# 🎬 Pro Media Downloader

<p align="center">
  <strong>专为抖音 (Douyin) 与 Instagram 量身打造的高画质无水印媒体解析与下载服务</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python Version" />
  <img src="https://img.shields.io/badge/FastAPI-0.110+-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License" />
</p>

---

## 📖 项目简介

**Pro Media Downloader** 是一个高性能、轻量级的媒体解析与下载站。专为解决短视频及社交媒体去水印难、画质压缩、防盗链 403 拦截等痛点打造。

自带现代化**暗黑玻璃拟态 Web 界面**，支持手机端与桌面端自适应；同时原生提供 **iOS 快捷指令专用接口**，可在 iPhone 分享面板一键直存系统相册。

---

## 🌟 核心特性

- 🎯 **原画级无水印提取**
  - **抖音 (Douyin)**：智能提取官方最高码率视频流（1080P/原画），支持单个视频与高清图文图集。
  - **Instagram**：精准提取 Reels、Post 帖子、多图轮播（Carousel）的高清 MP4 视频与原始高分辨率大图。
- 📦 **图集批量打包 & 原声提取**
  - 针对图集内容支持一键**批量打包下载为 ZIP 压缩包**。
  - 自动分离并支持**提取背景原声 (BGM/音频)** 下载。
- 🛡️ **流式防盗链与跨域中转**
  - 内置高性能 `/api/stream` 与 `/api/download` 流式中转代理。
  - 完美支持 HTTP `Range` 头，支持视频播放进度条任意拖拽跳转。
  - 彻底解决浏览器直接访问时的跨域与 `403 Forbidden` 防盗链拦截问题。
- 📱 **iOS 快捷指令深度集成**
  - 提供轻量专用的 `/api/shortcut` 接口。
  - 自动识别并提取粘贴文本中的短链接/口令，手机端分享面板一键直存无水印原片至系统相册。
- 🎨 **现代化响应式 Web UI**
  - 暗黑玻璃拟态（Glassmorphism）视觉风格。
  - 完美自适应 iPhone / iPad / Android / PC 各类屏幕。
  - 支持一键读取剪贴板智能粘贴、一键复制文案与话题标签。
- ⚡ **高性能异步架构**
  - 基于 **FastAPI + HTTPX** 纯异步高并发架构，毫秒级响应，低内存占用。

---

## 🚀 快速开始

### 方式一：Docker 部署（推荐）

通过 Docker Compose 可一键快速构建并启动服务：

```bash
# 1. 克隆代码仓库
git clone https://github.com/zbsdsb/media-downloader.git
cd media-downloader

# 2. 启动容器
docker compose up -d

# 3. 查看实时运行日志
docker compose logs -f
```

容器启动后，浏览器访问 `http://localhost:8080` 即可开始使用。

#### 环境变量配置 (`docker-compose.yml`)

| 环境变量 | 必填 | 默认值 | 说明 |
|---|:---:|:---:|---|
| `PROXY_URL` | 否 | 空 | HTTP/SOCKS 代理（若部署在大陆服务器且需解析海外 Instagram 时配置，如 `http://192.168.1.100:7890`；海外 VPS 留空即可） |

---

### 方式二：本地运行

#### 1. 环境准备
确保本机已安装 Python 3.10 或更高版本。

```bash
# 克隆仓库并进入目录
git clone https://github.com/zbsdsb/media-downloader.git
cd media-downloader
```

#### 2. 创建并激活虚拟环境

- **Linux / macOS:**
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  ```

- **Windows (PowerShell / CMD):**
  ```powershell
  python -m venv .venv
  .venv\Scripts\activate
  ```

#### 3. 安装依赖
```bash
pip install -r requirements.txt
```

#### 4. 启动服务
```bash
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

> **Windows 便捷启动**：Windows 用户配置好 `.venv` 虚拟环境后，亦可直接双击根目录下的 `start_local.bat` 一键运行。

服务启动后，使用浏览器打开：`http://localhost:8080`（局域网内的手机或平板可通过 `http://<本机局域网IP>:8080` 访问）。

---

## 📱 iOS 快捷指令配置指引

你可以配合 iOS「快捷指令」App 打造在手机分享面板一键直存的自动化流：

```
[iPhone 分享面板] ➔ [快捷指令] ➔ [POST /api/shortcut] ➔ [自动存入系统相册]
```

### 配置步骤简述：
1. 打开 iPhone「快捷指令」App，新建一条快捷指令；
2. 开启 **「在共享表单中显示」**，接收类型选择 **文本** 与 **URL**；
3. 添加 **「获取 URL 的内容」** 操作：
   - **URL**：`https://你的部署域名/api/shortcut`
   - **方法**：`POST`
   - **请求头**：`Content-Type: application/json`
   - **请求体**：选择 `JSON`，添加文本键值对 `"text": 快捷指令输入`
4. 添加 **「从输入中获取字典值」**，获取键为 `download_urls` 的列表；
5. 添加 **「重复每个项目」** 循环，并在循环内添加 **「存储到相簿」** 操作；
6. 保存后，在抖音或 Instagram 点击「分享」选择该快捷指令，即可自动无水印下载并存入相册！

---

## 🔌 API 接口文档

### 1. 媒体解析接口
- **路径**：`POST /api/parse`
- **说明**：前端 Web 交互主接口，返回详细媒体信息及防盗链流媒体直链。
- **请求体**：
  ```json
  {
    "url": "https://v.douyin.com/xxxxxx/"
  }
  ```
- **主要返回字段**：
  - `title`: 作品文案/标题
  - `author`: 发布者昵称与头像
  - `cover`: 视频/图集封面
  - `medias`: 解析出的媒体列表（含 `stream_url`、`download_url`、`raw_url`）
  - `music`: 背景音乐信息（含 `download_url`）
  - `zip_download_url`: 图集一键打包下载地址

### 2. iOS 快捷指令极简接口
- **路径**：`POST /api/shortcut`
- **说明**：专为自动化设计的轻量接口，自动从附带文字的分享口令中提取链接。
- **请求体**：
  ```json
  {
    "text": "7.12 复制打开抖音，看看【xxx的作品】 https://v.douyin.com/xxxxxx/"
  }
  ```
- **返回示例**：
  ```json
  {
    "success": true,
    "platform": "douyin",
    "type": "video",
    "title": "作品标题",
    "author": "作者昵称",
    "download_urls": [
      "https://your-domain.com/api/download?media_url=..."
    ],
    "raw_urls": [...]
  }
  ```

### 3. 流媒体与下载代理
- `GET /api/stream?media_url=<URL>`：视频在线流式中转代理，支持 `Range` 请求播放。
- `GET /api/download?media_url=<URL>&filename=<NAME>`：强制触发浏览器附件下载。
- `GET /api/download_zip?item_id=<ID>&filename=<NAME>`：将图集动态打包成 `.zip` 下载。

---

## 🌐 生产环境部署建议

建议使用 Nginx、1Panel、Caddy 或 Cloudflare Tunnel 进行公网反向代理并配置 SSL 证书：

```nginx
server {
    listen 80;
    server_name downloader.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name downloader.yourdomain.com;

    # SSL 证书配置 ...

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 支持大图集打包与流式中转下载
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

---

## ⚠️ 免责声明 (Disclaimer)

1. 本项目仅供编程技术学习、网络协议研究与交流使用，禁止用于任何商业用途。
2. 解析的所有音视频、图片及文字内容的知识产权与版权均归原平台（抖音、Instagram 等）及原创作者所有。
3. 请遵守相关平台的用户服务协议，使用本工具下载内容所产生的一切法律责任由使用者自行承担。

---

## 📄 开源协议

本项目遵循 [MIT License](https://opensource.org/licenses/MIT) 开源协议。
