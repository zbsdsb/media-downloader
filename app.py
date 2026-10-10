import os
import re
import io
import time
import asyncio
import zipfile
import random
import string
import urllib.parse
from contextlib import asynccontextmanager
from collections import OrderedDict
from typing import Optional, List
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import douyin_browser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时预拉常驻 Chromium，避免第一个解析请求承担冷启动开销"""
    try:
        browser = await douyin_browser.get_browser()
        await browser.warmup()
    except Exception as exc:
        print(f"[startup] 浏览器预热失败（首个解析请求会重试）: {type(exc).__name__}: {exc}", flush=True)
    yield
    try:
        await douyin_browser.close_browser()
    except Exception:
        pass


app = FastAPI(title="Pro Media Downloader API", version="1.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROXY_URL = os.getenv("PROXY_URL") or None

# 实况照片静帧的 JPEG 编码参数（画质优先，体积不管）
#   源图是抖音的 ~tplv-dy-aweme-images:q75.webp（有损，2160x2880，已是可拿到的最高分辨率；
#   去掉 tplv 变换求原图会 403，换 q100/origin/heic 等规格也 403，签名覆盖了变换段），
#   所以重编码阶段的目标是「不再额外丢东西」：
#     - quality=95 → 98：相对源图的重编码失真 PSNR 47.6dB → 50.2dB（实测）
#     - subsampling=0（4:4:4）：默认 4:2:0 会把色度分辨率减半，开启后色度不丢
#   实注：Pillow 的 subsampling=0 必须显式传，否则 quality>=95 仍可能走 4:2:0。
#   想压体积可设 LIVE_JPEG_QUALITY=85（约省一半，画质肉眼看不出差别）。
LIVE_JPEG_QUALITY = int(os.getenv("LIVE_JPEG_QUALITY", "98"))

# 实况声音：抖音不再提供实况片段原声（实测 5 个作品的实况段音轨全为零值，
# 唯一声源是尾卡广告），因此默认把**帖子 BGM** 混入实况片段，避免默片感。
#   bgm（默认）取 aweme_detail.music.play_url 的前 N 秒作音轨，视频流 -c:v copy 不重编码
#   off 保留抖音原样（静音）
LIVE_AUDIO_MODE = (os.getenv("LIVE_AUDIO_MODE", "bgm") or "bgm").strip().lower()
# BGM 音量比例（1.0 = 原音量）
LIVE_BGM_VOLUME = float(os.getenv("LIVE_BGM_VOLUME", "1.0"))

def random_str(length=16):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

# ----------------------------------------------------------------------------
# 抖音详情抓取
#
# 2026-10-10 起，抖音 web 详情接口对匿名裸 HTTP 请求一律返回
#   403 Blocked by ArgusSecurityPlugin Uifid Not Found
# ttwid 已不再被认可（注册接口仍返回 200，但 detail 接口照样拒绝）。
# 因此改为通过常驻 headless Chromium 打开作品页、直接取页面自身发出的 detail
# 响应 —— 只有真实浏览器会话携带的 secsdk 凭据（UIFID/UIFID_TEMP、odin_tt、
# __ac_signature 等）能通过校验。实现见 douyin_browser.py。
# ----------------------------------------------------------------------------

# 解析结果短缓存：手机脚本逐张保存实况时会用同一个分享链接反复调用
# /api/live_photo_bundle?url=...&index=N（每次都要重新解析作品，3s+）。
# 缓存后第 2..N 张只剩下载 + 打包的开销。作品内容不会变，15 分钟 TTL 足够安全。
_DETAIL_TTL = float(os.getenv("DOUYIN_DETAIL_TTL", "900"))
_DETAIL_CACHE_MAX = 128
_DETAIL_CACHE: "OrderedDict[str, tuple]" = OrderedDict()


def _detail_cache_get(item_id: str):
    ent = _DETAIL_CACHE.get(item_id)
    if not ent:
        return None
    ts, detail = ent
    if time.time() - ts > _DETAIL_TTL:
        _DETAIL_CACHE.pop(item_id, None)
        return None
    _DETAIL_CACHE.move_to_end(item_id)
    return detail


def _detail_cache_put(item_id: str, detail: dict) -> None:
    _DETAIL_CACHE[item_id] = (time.time(), detail)
    _DETAIL_CACHE.move_to_end(item_id)
    while len(_DETAIL_CACHE) > _DETAIL_CACHE_MAX:
        _DETAIL_CACHE.popitem(last=False)


async def fetch_douyin_data(item_id: str, is_note: bool = False, max_retries: int = 3):
    """通过常驻浏览器会话取 aweme_detail（含实况 MOV 与多档 bit_rate）"""
    cached = _detail_cache_get(item_id)
    if cached:
        return cached

    browser = await douyin_browser.get_browser()
    last_err = None
    for attempt in range(max_retries):
        try:
            detail = await browser.get_detail(item_id)
            if detail:
                _detail_cache_put(item_id, detail)
                return detail
            last_err = "浏览器返回空详情"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(0.5 * (attempt + 1))
    print(f"[douyin] fetch_douyin_data({item_id}) 失败: {last_err}", flush=True)
    return None

def get_clean_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    if "zbsnb.dpdns.org" in host or proto == "https":
        proto = "https"
    return f"{proto}://{host}/"


def media_duration(ffmpeg_exe: str, path: str) -> float:
    """用 ffmpeg 读容器时长（imageio-ffmpeg 只带 ffmpeg，不带 ffprobe）。

    不能信 aweme_detail 里声明的 duration：实测声明 1500ms 而实际视频流 1400ms，
    按声明的切 BGM 会留下 0.1s 悬空音轨。
    """
    try:
        p = subprocess.run([ffmpeg_exe, "-hide_banner", "-i", path],
                           capture_output=True, text=True, timeout=30)
        for line in p.stderr.splitlines():
            if "Duration:" in line:
                t = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = t.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        pass
    return 0.0
class ParseRequest(BaseModel):
    url: str

class ShortcutRequest(BaseModel):
    text: Optional[str] = None
    url: Optional[str] = None

# 1. 抖音最高画质解析（视频 / 图文图集）
async def extract_douyin(raw_input: str, base_url: str):
    url_match = re.search(r'https?://[^\s]+', raw_input)
    if not url_match:
        raise HTTPException(status_code=400, detail="未在输入内容中找到有效链接")
    
    share_url = url_match.group(0)

    async with httpx.AsyncClient(trust_env=False, follow_redirects=True, timeout=12.0) as client:
        resp = await client.get(share_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        final_url = str(resp.url)

        # 支持多种链接格式: /video/xxx /note/xxx /share/slides/xxx /share/video/xxx modal_id=xxx
        item_id_match = re.search(r'/(?:video|note|slides)/(\d+)', final_url)
        if not item_id_match:
            item_id_match = re.search(r'modal_id=(\d+)', final_url)
        if not item_id_match:
            # 兜底: 从原始分享链接提取（部分短链不重定向）
            item_id_match = re.search(r'/(?:video|note|slides)/(\d+)', share_url)
        if not item_id_match:
            # 最终兜底: 提取任意 19 位数字 ID
            item_id_match = re.search(r'(\d{19})', final_url) or re.search(r'(\d{19})', share_url)

        if not item_id_match:
            raise HTTPException(status_code=400, detail="无法提取抖音作品 ID，请检查链接是否有效")

        item_id = item_id_match.group(1)
        # slides 一定是图文/实况，note 是图文
        is_note = ("/note/" in final_url) or ("/slides/" in final_url) or ("/note/" in share_url) or ("/slides/" in share_url)

        aweme_detail = await fetch_douyin_data(item_id, is_note=is_note)
        if not aweme_detail:
            # 注意：状态码必须落在 4xx。Cloudflare 边缘会把 origin 的 5xx 正文重写成
            # 纯文本 "error code: 502"，手机端就只能看到一句无信息量的错误；
            # 4xx 会原样透传，detail 里的话才能读得到。
            raise HTTPException(status_code=424, detail="上游解析失败：抖音详情接口被风控拦截或超时，请稍后重试（浏览器通道）")

        raw_title = aweme_detail.get("desc", f"douyin_{item_id}").strip() or f"douyin_{item_id}"
        clean_title = re.sub(r'[\\/*?:"<>|\r\n\t]', '_', raw_title)[:60]
        
        cover = aweme_detail.get("video", {}).get("cover", {}).get("url_list", [""])[0]
        author = aweme_detail.get("author", {}).get("nickname", "未知作者")
        avatar = aweme_detail.get("author", {}).get("avatar_thumb", {}).get("url_list", [""])[0]

        # 提取背景音乐 BGM
        music_info = aweme_detail.get("music", {})
        music_urls = music_info.get("play_url", {}).get("url_list", [])
        music_title = music_info.get("title", "bgm")
        music_item = None
        if music_urls:
            music_dl = f"{base_url}api/download?media_url={urllib.parse.quote(music_urls[0])}&filename={urllib.parse.quote(clean_title + '_BGM')}"
            music_item = {
                "title": music_title,
                "author": music_info.get("author", ""),
                "raw_url": music_urls[0],
                "url": music_urls[0],
                "duration_ms": music_info.get("duration") or music_info.get("audition_duration"),
                "download_url": music_dl
            }

        # 判断是否为图文/图集/实况图文 (Live Photo)
        images = aweme_detail.get("images")
        is_live_work = aweme_detail.get("is_live_photo", False) or aweme_detail.get("media_type") == 42
        if images:
            medias = []
            has_live_photo = False
            for idx, img in enumerate(images):
                url_list = img.get("url_list", [])
                raw_img_url = url_list[0] if url_list else ""
                
                # 判定实况照片 (live_photo_type == 1 且包含配套短视频 video)
                iv = img.get("video") or {}
                is_live_img = (img.get("live_photo_type") == 1 and bool(iv)) or (is_live_work and bool(iv))
                
                raw_video_url = None
                proxy_video_dl = None
                if is_live_img:
                    has_live_photo = True
                    v_urls = (iv.get("play_addr") or {}).get("url_list") or (iv.get("download_addr") or {}).get("url_list") or []
                    if v_urls:
                        raw_video_url = v_urls[0].replace("playwm", "play")
                        proxy_video_dl = f"{base_url}api/download?media_url={urllib.parse.quote(raw_video_url)}&filename={urllib.parse.quote(clean_title + f'_实况{idx+1}')}"

                proxy_dl = f"{base_url}api/download?media_url={urllib.parse.quote(raw_img_url)}&filename={urllib.parse.quote(clean_title + f'_图{idx+1}')}"
                proxy_stream = f"{base_url}api/stream?media_url={urllib.parse.quote(raw_img_url)}"
                
                media_item = {
                    "index": idx + 1,
                    "type": "live_photo" if is_live_img else "image",
                    "is_live": is_live_img,
                    "raw_url": raw_img_url,
                    "stream_url": proxy_stream,
                    "download_url": proxy_dl,
                    "duration_ms": (iv.get("duration") or ((iv.get("play_addr") or {}).get("duration"))) if is_live_img else None,
                    "quality": "实况照片(图片+视频)" if is_live_img else "原始无损图"
                }
                if is_live_img and raw_video_url:
                    media_item["raw_video_url"] = raw_video_url
                    media_item["download_video_url"] = proxy_video_dl

                medias.append(media_item)

            encoded_title = urllib.parse.quote(clean_title)
            zip_url = f"{base_url}api/download_zip?item_id={item_id}&filename={encoded_title}"

            platform_title = "抖音实况图文" if has_live_photo else "抖音图文"
            return {
                "platform": platform_title,
                "type": "live_photo" if has_live_photo else "image",
                "has_live_photo": has_live_photo,
                "item_id": item_id,
                "title": clean_title,
                "author": author,
                "avatar": avatar,
                "cover": cover or (medias[0]["raw_url"] if medias else ""),
                "count": len(medias),
                "zip_download_url": zip_url,
                "music": music_item,
                "medias": medias
            }
        else:
            # 视频模式：查找最高码率 (bit_rate)
            video_info = aweme_detail.get("video", {})
            bit_rate_list = video_info.get("bit_rate", [])

            best_video_url = None
            quality_label = "1080P/超清最高码率"

            if bit_rate_list:
                sorted_rates = sorted(bit_rate_list, key=lambda x: x.get("bit_rate", 0), reverse=True)
                highest = sorted_rates[0]
                url_list = highest.get("play_addr", {}).get("url_list", [])
                if url_list:
                    best_video_url = url_list[0].replace("playwm", "play")
                    gear_name = highest.get("gear_name", "")
                    rate_kbps = highest.get("bit_rate", 0) // 1000
                    fps = highest.get("FPS", "")
                    quality_label = f"原画最高码率 ({gear_name} {rate_kbps}kbps {fps}FPS)"

            if not best_video_url:
                fallback_list = video_info.get("play_addr", {}).get("url_list", [])
                if fallback_list:
                    best_video_url = fallback_list[0].replace("playwm", "play")

            if not best_video_url:
                raise HTTPException(status_code=400, detail="未找到无水印视频直链")

            proxy_dl = f"{base_url}api/download?media_url={urllib.parse.quote(best_video_url)}&filename={urllib.parse.quote(clean_title)}"
            proxy_stream = f"{base_url}api/stream?media_url={urllib.parse.quote(best_video_url)}"

            return {
                "platform": "抖音视频",
                "type": "video",
                "item_id": item_id,
                "title": clean_title,
                "author": author,
                "avatar": avatar,
                "cover": cover,
                "count": 1,
                "music": music_item,
                "medias": [{
                    "index": 1,
                    "type": "video",
                    "raw_url": best_video_url,
                    "stream_url": proxy_stream,
                    "download_url": proxy_dl,
                    "quality": quality_label
                }]
            }

# 2. Instagram 最高画质解析
def extract_instagram(raw_input: str, base_url: str):
    url_match = re.search(r'https?://[^\s]+', raw_input)
    if not url_match:
        raise HTTPException(status_code=400, detail="未找到有效 Instagram 链接")
    
    url = url_match.group(0)

    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'extract_flat': False,
    }
    if PROXY_URL:
        ydl_opts['proxy'] = PROXY_URL

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            raw_title = info.get('title') or info.get('description', 'instagram_post')[:50]
            clean_title = re.sub(r'[\\/*?:"<>|\r\n\t]', '_', raw_title).strip() or "instagram_media"
            author = info.get('uploader') or info.get('channel') or "Instagram User"
            cover = info.get('thumbnail', '')

            if 'entries' in info and info['entries']:
                medias = []
                for idx, entry in enumerate(info['entries']):
                    m_url = entry.get('url')
                    if not m_url and entry.get('formats'):
                        m_url = entry['formats'][-1].get('url')
                    
                    if m_url:
                        is_vid = entry.get('ext') == 'mp4' or 'video' in entry.get('extractor_key', '').lower()
                        m_type = "video" if is_vid else "image"
                        proxy_dl = f"{base_url}api/download?media_url={urllib.parse.quote(m_url)}&filename={urllib.parse.quote(clean_title + f'_item{idx+1}')}"
                        proxy_stream = f"{base_url}api/stream?media_url={urllib.parse.quote(m_url)}"
                        medias.append({
                            "index": idx + 1,
                            "type": m_type,
                            "raw_url": m_url,
                            "stream_url": proxy_stream,
                            "download_url": proxy_dl,
                            "quality": "原片最高画质"
                        })
                return {
                    "platform": "Instagram",
                    "type": "carousel",
                    "item_id": info.get('id', ''),
                    "title": clean_title,
                    "author": author,
                    "avatar": "",
                    "cover": cover,
                    "count": len(medias),
                    "medias": medias
                }
            
            video_url = info.get('url')
            if not video_url and info.get('formats'):
                video_url = info['formats'][-1].get('url')
            
            if not video_url:
                raise HTTPException(status_code=400, detail="未能提取到 Instagram 媒体直链")

            is_video = info.get('ext') == 'mp4' or 'video' in info.get('extractor_key', '').lower() or info.get('vcodec') != 'none'
            proxy_dl = f"{base_url}api/download?media_url={urllib.parse.quote(video_url)}&filename={urllib.parse.quote(clean_title)}"
            proxy_stream = f"{base_url}api/stream?media_url={urllib.parse.quote(video_url)}"

            return {
                "platform": "Instagram",
                "type": "video" if is_video else "image",
                "item_id": info.get('id', ''),
                "title": clean_title,
                "author": author,
                "avatar": "",
                "cover": cover,
                "count": 1,
                "medias": [{
                    "index": 1,
                    "type": "video" if is_video else "image",
                    "raw_url": video_url,
                    "stream_url": proxy_stream,
                    "download_url": proxy_dl,
                    "quality": "原画最高分辨率"
                }]
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Instagram 解析出错: {str(e)}")

# ================= 路由端点 =================

@app.post("/api/parse")
async def api_parse(req: ParseRequest, request: Request):
    raw_url = req.url.strip()
    base_url = get_clean_base_url(request)
    if "douyin.com" in raw_url or "v.douyin.com" in raw_url:
        return await extract_douyin(raw_url, base_url)
    elif "instagram.com" in raw_url:
        return extract_instagram(raw_url, base_url)
    else:
        raise HTTPException(status_code=400, detail="暂不支持该链接，目前支持 抖音 与 Instagram")

@app.post("/api/shortcut")
async def api_shortcut(req: ShortcutRequest, request: Request):
    raw_text = (req.text or req.url or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="请求内容为空")

    base_url = get_clean_base_url(request)
    if "douyin.com" in raw_text or "v.douyin.com" in raw_text:
        res = await extract_douyin(raw_text, base_url)
    elif "instagram.com" in raw_text:
        res = extract_instagram(raw_text, base_url)
    else:
        raise HTTPException(status_code=400, detail="不支持的链接格式")

    raw_urls = []
    download_urls = []
    live_photos = []

    for m in res.get("medias", []):
        raw_urls.append(m["raw_url"])
        download_urls.append(m["download_url"])
        if m.get("is_live") and m.get("raw_video_url"):
            live_photos.append({
                "index": m["index"],
                "image_url": m["raw_url"],
                "video_url": m["raw_video_url"],
                "download_image_url": m["download_url"],
                "download_video_url": m["download_video_url"]
            })
            # 将实况附带的动态视频也作为媒体文件加入，确保普通快捷指令也能存入动起来的实况片段
            raw_urls.append(m["raw_video_url"])
            download_urls.append(m["download_video_url"])

    return {
        "success": True,
        "platform": res.get("platform"),
        "type": res.get("type"),
        "has_live_photo": res.get("has_live_photo", False),
        "title": res.get("title"),
        "author": res.get("author"),
        "cover": res.get("cover"),
        "count": res.get("count"),
        "download_urls": download_urls,
        "raw_urls": raw_urls,
        "live_photos": live_photos,
        "medias": res.get("medias", []),
        "headers": {
            "Referer": "https://www.douyin.com/",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
        }
    }

# 高性能防盗链视频/图片流媒体代理（支持 Range 拖拽进度条）
@app.get("/api/stream")
async def api_stream(request: Request, media_url: str = Query(...)):
    is_douyin = "douyin" in media_url or "byte" in media_url
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": "https://www.douyin.com/" if is_douyin else "https://www.instagram.com/"
    }
    
    range_header = request.headers.get("range")
    if range_header:
        req_headers["Range"] = range_header

    client = httpx.AsyncClient(trust_env=False, timeout=30.0, follow_redirects=True)
    req = client.build_request("GET", media_url, headers=req_headers)
    resp = await client.send(req, stream=True)

    if resp.status_code not in (200, 206):
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=resp.status_code, detail="无法读取远程视频流")

    async def stream_content():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 512):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    res_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": resp.headers.get("content-type", "video/mp4"),
        "Cache-Control": "public, max-age=3600",
        "Access-Control-Allow-Origin": "*"
    }
    if "content-range" in resp.headers:
        res_headers["Content-Range"] = resp.headers["content-range"]
    if "content-length" in resp.headers:
        res_headers["Content-Length"] = resp.headers["content-length"]

    return StreamingResponse(
        stream_content(),
        status_code=resp.status_code,
        headers=res_headers
    )

# 图文一键打包 ZIP 下载接口
@app.get("/api/download_zip")
async def api_download_zip(item_id: str = Query(...), filename: str = Query("images")):
    aweme_detail = await fetch_douyin_data(item_id, is_note=True)
    if not aweme_detail or not aweme_detail.get("images"):
        raise HTTPException(status_code=400, detail="未找到对应的图集内容")

    images = aweme_detail.get("images", [])
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": "https://www.douyin.com/"
    }

    zip_buffer = io.BytesIO()
    async with httpx.AsyncClient(trust_env=False, headers=headers, timeout=30.0, follow_redirects=True) as client:
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for idx, img in enumerate(images):
                url_list = img.get("url_list", [])
                if url_list:
                    img_resp = await client.get(url_list[0])
                    if img_resp.status_code == 200:
                        zip_file.writestr(f"image_{idx+1}.jpg", img_resp.content)

    zip_buffer.seek(0)
    clean_fn = re.sub(r'[\\/*?:"<>|\r\n\t]', '_', filename).strip()[:50]
    encoded_fn = urllib.parse.quote(f"{clean_fn}_图集.zip")

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}"}
    )

# 流式防盗链中转下载
@app.get("/api/download")
async def api_download(media_url: str = Query(...), filename: str = Query("media")):
    is_douyin = "douyin" in media_url or "byte" in media_url
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": "https://www.douyin.com/" if is_douyin else "https://www.instagram.com/"
    }

    client = httpx.AsyncClient(trust_env=False, timeout=60.0, follow_redirects=True)
    req = client.build_request("GET", media_url, headers=headers)
    resp = await client.send(req, stream=True)

    if resp.status_code not in (200, 206):
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=resp.status_code, detail="远程资源抓取失败")

    async def stream_content():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 512):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    content_type = resp.headers.get("content-type", "application/octet-stream")
    if "audio" in content_type or ".mp3" in media_url:
        ext = ".mp3"
    elif "video" in content_type or ".mp4" in media_url:
        ext = ".mp4"
    else:
        ext = ".jpg"
    
    clean_fn = re.sub(r'[\\/*?:"<>|\r\n\t]', '_', filename).strip()[:50]
    encoded_fn = urllib.parse.quote(f"{clean_fn}{ext}")

    return StreamingResponse(
        stream_content(),
        media_type=content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}",
            "Cache-Control": "public, max-age=3600"
        }
    )

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get('/favicon.ico', include_in_schema=False)
async def favicon():
    fav_p = os.path.join(STATIC_DIR, 'favicon.png')
    if os.path.exists(fav_p):
        return FileResponse(fav_p, media_type='image/png')
    raise HTTPException(status_code=404)

@app.get('/apple-touch-icon.png', include_in_schema=False)
async def apple_touch_icon():
    icon_p = os.path.join(STATIC_DIR, 'apple-touch-icon.png')
    if os.path.exists(icon_p):
        return FileResponse(icon_p, media_type='image/png')
    raise HTTPException(status_code=404)

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>Media Downloader API is Running</h1>")



import uuid
import tempfile
import shutil
import zipfile
import subprocess
from PIL import Image
import imageio_ffmpeg
from live_mov_builder import inject_live_photo_metadata

def build_apple_live_photo_exif_app1(content_id: str) -> bytes:
    """构建符合 Apple 官方规范的完整 EXIF APP1（IFD0 -> ExifIFD (0x8769) -> MakerNote (0x927C) -> Apple iOS 0x0011 ContentIdentifier）"""
    # 1. 构造 Apple MakerNote (70 字节)
    cid_bytes = (content_id + "\0").encode("ascii")  # 37 字节
    pad = len(cid_bytes) % 2
    mn_data_offset = 14 + 2 + 12 + 4  # 32 字节

    mn = bytearray()
    mn.extend(b"Apple iOS\x00\x00\x01MM")  # 14 字节头部
    mn.extend((1).to_bytes(2, "big"))       # 1 个条目
    mn.extend((0x0011).to_bytes(2, "big"))  # 0x0011 ContentIdentifier
    mn.extend((2).to_bytes(2, "big"))       # type ASCII
    mn.extend(len(cid_bytes).to_bytes(4, "big"))  # count 37
    mn.extend(mn_data_offset.to_bytes(4, "big"))  # offset 32
    mn.extend((0).to_bytes(4, "big"))       # next IFD = 0
    mn.extend(cid_bytes)
    if pad:
        mn.append(0)

    # 2. 构造规范的 TIFF 目录树：IFD0(0x8769 指向 ExifIFD) -> ExifIFD(0x927C 指向 MakerNote)
    exif_ifd_offset = 26
    maker_note_offset = 44

    tiff = bytearray()
    tiff.extend(b"MM")  # 大端序
    tiff.extend((42).to_bytes(2, "big"))
    tiff.extend((8).to_bytes(4, "big"))  # 指向 IFD0 偏移 8

    # IFD0: 1 个条目 ExifOffset (0x8769)
    tiff.extend((1).to_bytes(2, "big"))
    tiff.extend((0x8769).to_bytes(2, "big"))  # tag 0x8769
    tiff.extend((4).to_bytes(2, "big"))       # type LONG
    tiff.extend((1).to_bytes(4, "big"))       # count 1
    tiff.extend(exif_ifd_offset.to_bytes(4, "big"))  # 偏移 26
    tiff.extend((0).to_bytes(4, "big"))       # next IFD = 0

    # ExifIFD: 1 个条目 MakerNote (0x927C)
    tiff.extend((1).to_bytes(2, "big"))
    tiff.extend((0x927C).to_bytes(2, "big"))  # tag 0x927C
    tiff.extend((7).to_bytes(2, "big"))       # type UNDEFINED
    tiff.extend(len(mn).to_bytes(4, "big"))   # 70 字节
    tiff.extend(maker_note_offset.to_bytes(4, "big"))  # 偏移 44
    tiff.extend((0).to_bytes(4, "big"))       # next IFD = 0

    # 放入 MakerNote 数据
    tiff.extend(mn)

    # 3. 封装为 JPEG APP1 (0xFFE1)
    payload = b"Exif\x00\x00" + tiff
    return b"\xFF\xE1" + (len(payload) + 2).to_bytes(2, "big") + payload

@app.get("/api/live_photo_bundle")
async def api_live_photo_bundle(url: str = Query(...), index: int = Query(1), request: Request = None):
    """生成并打包带 Apple 官方实况元数据 (MakerNote 17 + QuickTime Identifier) 的 Live Photo ZIP 包"""
    base_url = get_clean_base_url(request)
    res = await extract_douyin(url, base_url)
    medias = res.get("medias", [])
    if not medias:
        raise HTTPException(status_code=400, detail="未找到媒体内容")

    idx = max(0, min(index - 1, len(medias) - 1))
    target = medias[idx]

    if not target.get("is_live") or not target.get("raw_video_url"):
        raise HTTPException(status_code=400, detail="所选项目不是实况照片")

    img_url = target["raw_url"]
    vid_url = target["raw_video_url"]
    asset_id = str(uuid.uuid4()).upper()

    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": "https://www.douyin.com/"
    }

    async with httpx.AsyncClient(trust_env=False, timeout=30.0, follow_redirects=True) as client:
        import asyncio as _aio
        img_resp, vid_resp = await _aio.gather(
            client.get(img_url, headers=headers),
            client.get(vid_url, headers=headers),
        )

    if img_resp.status_code != 200 or vid_resp.status_code != 200:
        raise HTTPException(status_code=424, detail="实况源文件下载失败（抖音 CDN 拒绝或过期），请重新解析")

    temp_dir = tempfile.mkdtemp(prefix="lp_")
    try:
        raw_vid_path = os.path.join(temp_dir, "raw.mp4")
        out_mov_path = os.path.join(temp_dir, "live.mov")

        with open(raw_vid_path, "wb") as f:
            f.write(vid_resp.content)

        # 1. ffmpeg 无损转封装为 QuickTime MOV（moov 放文件末尾），并可选混入帖子 BGM。
        #    Apple 实况必需的 mdta content.identifier 与 mebx timed metadata 轨道
        #    由 inject_live_photo_metadata 直接写入，不用 ffmpeg -metadata。
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        remux_path = os.path.join(temp_dir, "remux.mov")
        # 注意：不要在这里写 -c copy，它会被当成下一个输入的输入选项（Unknown decoder 'copy'）
        cmd = [ffmpeg_exe, "-y", "-i", raw_vid_path]

        # 实况原声缺失（抖音把实况段音轨置全零）→ 用帖子 BGM 补上；视频流仍 copy，不重编码
        bgm_path = None
        if LIVE_AUDIO_MODE == "bgm":
            bgm_url = (((res.get("music") or {}).get("url")) or "").strip()
            if bgm_url:
                try:
                    async with httpx.AsyncClient(trust_env=False, timeout=30.0, follow_redirects=True) as mc:
                        bgm_resp = await mc.get(bgm_url, headers=headers)
                    if bgm_resp.status_code == 200 and bgm_resp.content:
                        bgm_path = os.path.join(temp_dir, "bgm.src")
                        with open(bgm_path, "wb") as bf:
                            bf.write(bgm_resp.content)
                except Exception as exc:
                    print(f"[live_photo] BGM 下载失败，回退静音: {type(exc).__name__}: {exc}", flush=True)
                    bgm_path = None

        if bgm_path:
            # 切 BGM 前 clip 秒作音轨；必须写 -t，否则视频结束后 BGM 会无限流下去
            clip = media_duration(ffmpeg_exe, raw_vid_path)
            if clip <= 0:
                clip = float(target.get("duration_ms") or 0) / 1000.0 or 3.0
            fade_st = max(0.0, clip - 0.12)
            cmd += [
                "-i", bgm_path,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100",
                "-af", (f"volume={LIVE_BGM_VOLUME},afade=t=in:st=0:d=0.08,"
                        f"afade=t=out:st={fade_st:.3f}:d=0.12"),
                "-t", f"{clip:.3f}",
                "-shortest",
            ]
        else:
            cmd += ["-c", "copy", "-map", "0"]

        cmd += ["-map_metadata", "-1", "-f", "mov", "-brand", "qt", remux_path]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        patched_mov = inject_live_photo_metadata(open(remux_path, "rb").read(), asset_id)
        with open(out_mov_path, "wb") as mf:
            mf.write(patched_mov)

        # 2. 将静态图片转为标准 JPEG 并注入 Apple MakerNote 0x0011 (asset_id)
        #    编码参数见 LIVE_JPEG_QUALITY：quality=98 + 4:4:4，重编码失真已趋近可忽略
        im = Image.open(io.BytesIO(img_resp.content))
        jpg_buf = io.BytesIO()
        im.convert("RGB").save(
            jpg_buf, format="JPEG",
            quality=LIVE_JPEG_QUALITY, subsampling=0,
            optimize=True, progressive=True,
        )
        raw_jpg = jpg_buf.getvalue()

        app1 = build_apple_live_photo_exif_app1(asset_id)
        final_jpg = raw_jpg[:2] + app1 + raw_jpg[2:]

        # 3. 打包成轻量 ZIP
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_STORED) as z:
            z.writestr("live.jpg", final_jpg)
            with open(out_mov_path, "rb") as mf:
                z.writestr("live.mov", mf.read())

        zip_buf.seek(0)
        clean_fn = re.sub(r'[\\/*?:"<>|\r\n\t]', "_", res.get("title", "live_photo")).strip()[:50]
        encoded_fn = urllib.parse.quote(f"{clean_fn}_实况{index}.zip")

        return StreamingResponse(
            zip_buf,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}"}
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
