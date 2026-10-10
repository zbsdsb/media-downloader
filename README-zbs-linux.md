# media-downloader @ zbs-linux（2026-10-10 迁入）

抖音/Instagram 无水印解析 + 苹果实况照片打包服务。**解析走真实浏览器通道**（Playwright
Chromium），因为 2026-10 起抖音 detail 接口对匿名裸 HTTP 请求一律返回
`403 Blocked by ArgusSecurityPlugin Uifid Not Found`。

## 拓扑

```
iPhone「下载实况照片」脚本（URL 不变）
  └─ https://media.zbsnb.dpdns.org
       └─ Cloudflare 边缘 → CF Tunnel → 京东云 JD
            └─ 1Panel OpenResty 站点 media-downloader-zbsnb
                 proxy_pass http://100.90.29.96:8088   ← 2026-10-10 由 127.0.0.1:8088 改为本机
                    └─ zbs-linux（本机，家宽住宅 IP 111.196.228.46 北京联通）
                         ├─ 常驻 headless Chromium 解析 aweme_detail
                         ├─ 下载实况 MOV / 原图（住宅 IP 拉抖音 CDN）
                         └─ ffmpeg + live_mov_builder 注入 Apple 实况元数据 → ZIP
```

JD 与原容器 `media-downloader` 均保留未删除，回滚只需把那一行改回 `127.0.0.1:8088` 并 reload。

## 目录

| 路径 | 说明 |
|---|---|
| `~/media-service/app.py` | FastAPI 主程序（入口） |
| `~/media-service/douyin_browser.py` | 常驻 Chromium 会话，捕获 detail 响应 |
| `~/media-service/live_mov_builder.py` | QuickTime 盒子注入（Apple 实况所需） |
| `~/media-service/static/` | 前端页面与图标 |
| `~/media-service/chrome_profile/` | 浏览器 profile（UIFID/ttwid/odin_tt 等凭据落在这里） |
| `~/media-service/venv/` | Python 3.14 虚拟环境 |
| `~/media-service/tools/` | 诊断脚本 |
| `~/.cache/ms-playwright/` | Chromium 153 headless shell + ffmpeg |

## 运维命令

```bash
systemctl status media-downloader        # 状态
sudo systemctl restart media-downloader  # 重启（改完 app.py 后执行）
sudo journalctl -u media-downloader -f   # 实时日志
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/openapi.json   # 健康检查
```

## 接口

| 接口 | 说明 |
|---|---|
| `POST /api/parse` | `{"url":"分享链接"}` → 完整媒体列表 |
| `POST /api/shortcut` | `{"text":"整段分享文案"}` → 脚本用（含 `live_photos`/`medias`/`raw_urls`） |
| `GET /api/live_photo_bundle?url=&index=N` | 生成带 Apple 实况元数据的 ZIP（`live.jpg` + `live.mov`） |
| `GET /api/download?media_url=&filename=` | 带 Referer 的中转下载 |
| `GET /api/stream?media_url=` | 支持 Range 的流媒体代理 |
| `GET /api/download_zip?item_id=&filename=` | 图集打包 |

## 关键设计取舍

- **解析缓存 `DOUYIN_DETAIL_TTL`（默认 900s）**：手机脚本逐张保存实况时会用同一链接
  反复调 `/api/live_photo_bundle?index=N`，缓存后第 2..N 张只剩下载+打包（约 1.9s/张，
  否则每次都要重新解析 3s+）。
- **浏览器会话回收**：每 150 次解析或存活 6 小时自动重建，避免 Chromium 内存长期膨胀。
- **并发**：解析用 `asyncio.Lock` 串行——单页复用最稳，个人服务够用。
- **失败用 4xx 而不是 5xx**：Cloudflare 会把 origin 的 5xx 正文重写成 `error code: 502`，
  手机端就读不到原因；4xx 正文原样透传。
- **合盖不休眠**：`/etc/systemd/logind.conf.d/lid.conf` 已是 `HandleLidSwitch=ignore`
  （已通过 `busctl` 确认生效），合盖不会休眠。

## 故障处理

| 现象 | 处理 |
|---|---|
| 解析返回 424「上游解析失败」 | 多为抖音风控拦截。先看 `journalctl -u media-downloader`；尝试 `rm -rf chrome_profile && sudo systemctl restart media-downloader` 重建凭据 |
| 浏览器起不来 | `systemctl status` 看报错；`./venv/bin/playwright install --with-deps chromium` 重装 |
| 全站 502 | 检查 Tailscale：`tailscale status`、`ping 100.98.218.97`；再检查 JD 的 openresty 反代是否仍指向 `100.90.29.96:8088` |
| 想回滚到 JD | JD 上 `/opt/1panel/www/sites/media-downloader-zbsnb/proxy/root.conf` 改回 `127.0.0.1:8088`，`nginx -s reload`（备份在 `root.conf.bak-20261010`） |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DOUYIN_PROFILE_DIR` | `./chrome_profile` | 浏览器 profile 目录 |
| `DOUYIN_HEADLESS` | `1` | 设 0 则开有头窗口（调试用，需要图形环境） |
| `DOUYIN_DETAIL_TTL` | `900` | 解析结果缓存秒数 |
| `PROXY_URL` | 空 | 仅 Instagram 走 yt-dlp 时使用 |

## 实测性能（2026-10-10）

- 解析一个作品：3.5~3.7s（含页面加载）；缓存命中后 bundle 约 1.9s/张
- chrome-headless 峰值 RSS：**687MB**；整机内存占用 2.4G / 5.2G
- 家宽上行：13.7 Mbps（Tailscale 直连 8.8ms）；端到端（CF 链路）约 3.4 Mbps
- 瓶颈在 JD → Cloudflare 那一段，与迁移前相同
