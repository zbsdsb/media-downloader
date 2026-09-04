import os
import re
import io
import time
import zipfile
import random
import string
import urllib.parse
from typing import Optional, List
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="Pro Media Downloader API", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROXY_URL = os.getenv("PROXY_URL") or None

def random_str(length=16):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

async def fetch_douyin_data(item_id: str, max_retries: int = 3):
    """带自动注册 ttwid 与动态凭据的抖音详情抓取器（强制直连 trust_env=False）"""
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=True, timeout=10.0) as client:
                reg_resp = await client.post('https://ttwid.bytedance.com/ttwid/union/register/', json={
                    'region': 'cn', 'aid': 1768, 'needFid': 'false', 'service': 'www.ixigua.com',
                    'migrate_info': {'ticket': '', 'source': 'node'}, 'cbUrlProtocol': 'https', 'union': 'true'
                })
                ttwid = reg_resp.cookies.get('ttwid', '')

                headers = {
                    'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(120, 128)}.0.0.0 Safari/537.36',
                    'Referer': f'https://www.douyin.com/video/{item_id}',
                    'Cookie': f'ttwid={ttwid}; s_v_web_id=verify_{random_str(8)}; passport_csrf_token={random_str(32)};',
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'Sec-Ch-Ua': '\"Chromium\";v=\"128\", \"Not;A=Brand\";v=\"24\", \"Google Chrome\";v=\"128\"',
                    'Sec-Ch-Ua-Mobile': '?0',
                    'Sec-Ch-Ua-Platform': '\"Windows\"',
                    'Sec-Fetch-Dest': 'empty',
                    'Sec-Fetch-Mode': 'cors',
                    'Sec-Fetch-Site': 'same-origin',
                }

                url = f'https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={item_id}&aid=6383&device_platform=webapp'
                resp = await client.get(url, headers=headers)
                
                if resp.status_code == 200 and resp.text.startswith('{'):
                    data = resp.json()
                    if data.get('aweme_detail'):
                        return data.get('aweme_detail')
        except Exception:
            pass
        time.sleep(0.3)
    return None

def get_clean_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    if "zbsnb.dpdns.org" in host or proto == "https":
        proto = "https"
    return f"{proto}://{host}/"
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

        item_id_match = re.search(r'/(?:video|note)/(\d+)', final_url)
        if not item_id_match:
            item_id_match = re.search(r'modal_id=(\d+)', final_url)

        if not item_id_match:
            raise HTTPException(status_code=400, detail="无法提取抖音作品 ID，请检查链接是否有效")

        item_id = item_id_match.group(1)

        aweme_detail = await fetch_douyin_data(item_id)
        if not aweme_detail:
            raise HTTPException(status_code=502, detail="获取抖音作品详情失败，请稍后重试")

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
                "download_url": music_dl
            }

        # 判断是否为图文/图集
        images = aweme_detail.get("images")
        if images:
            medias = []
            for idx, img in enumerate(images):
                url_list = img.get("url_list", [])
                if url_list:
                    raw_img_url = url_list[0]
                    proxy_dl = f"{base_url}api/download?media_url={urllib.parse.quote(raw_img_url)}&filename={urllib.parse.quote(clean_title + f'_图{idx+1}')}"
                    proxy_stream = f"{base_url}api/stream?media_url={urllib.parse.quote(raw_img_url)}"
                    medias.append({
                        "index": idx + 1,
                        "type": "image",
                        "raw_url": raw_img_url,
                        "stream_url": proxy_stream,
                        "download_url": proxy_dl,
                        "quality": "原始无损图"
                    })

            encoded_title = urllib.parse.quote(clean_title)
            zip_url = f"{base_url}api/download_zip?item_id={item_id}&filename={encoded_title}"

            return {
                "platform": "抖音图文",
                "type": "image",
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

    download_urls = [m["download_url"] for m in res.get("medias", [])]
    raw_urls = [m["raw_url"] for m in res.get("medias", [])]

    return {
        "success": True,
        "platform": res.get("platform"),
        "type": res.get("type"),
        "title": res.get("title"),
        "author": res.get("author"),
        "cover": res.get("cover"),
        "count": res.get("count"),
        "download_urls": download_urls,
        "raw_urls": raw_urls
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
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 128):
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
    aweme_detail = await fetch_douyin_data(item_id)
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
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 128):
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

