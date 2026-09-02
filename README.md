# 🎬 Pro Media Downloader (抖音 & Instagram 高画质无水印下载服务)

专为**抖音 (Douyin)**与 **Instagram** 量身打造的高画质无水印解析服务，带现代响应式 Web 界面与 iOS 快捷指令专属接口。

---

## 🌟 核心特性
- **最高画质无水印**：抖音自动提取最高码率（1080P/原画），Ins 提取最高分辨率 MP4 / 原始大图。
- **全格式支持**：抖音单视频/图集、Instagram Reels/Post/多图轮播（Carousel）。
- **防盗链中转**：内置 /api/download 流式中转引擎，彻底解决跨域及 403 Forbidden 问题。
- **现代 Web UI**：玻璃拟态暗黑风格，自适应 iPhone/iPad/PC，支持一键剪贴板读取与文案复制。
- **iOS 快捷指令无缝配合**：提供专用的 /api/shortcut 极简接口，iPhone 分享面板一键直存系统相册。

---

## 🚀 本地启动方式

`ash
# 双击 start_local.bat 或执行：
cd E:\Paseo\media-downloader
.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8080 --reload
`
浏览器打开：http://localhost:8080（局域网内手机可通过电脑 IP 访问）。

---

## 🐳 云服务器 / Docker 部署

`ash
# 1. 构建并启动容器
docker compose up -d

# 2. 查看日志
docker compose logs -f
`
配合 Nginx / 1Panel 反向代理绑定域名并开启 HTTPS 即可全网使用。
