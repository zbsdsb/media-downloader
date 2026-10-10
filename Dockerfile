FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 抖音解析走真实浏览器通道（douyin_browser.py）：
# 2026-10 起 detail 接口对匿名裸 HTTP 请求返回 403 Uifid Not Found，只有浏览器会话能过，
# 因此镜像内必须装 Chromium 及其系统依赖。
RUN python -m playwright install --with-deps chromium

COPY . .

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
