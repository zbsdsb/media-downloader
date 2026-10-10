"""抖音作品详情抓取器 —— 真实浏览器（Playwright Chromium）通道

背景（2026-10-10 起）：
抖音 web 详情接口 /aweme/v1/web/aweme/detail/ 对匿名裸 HTTP 请求一律返回
    403 Blocked by ArgusSecurityPlugin Uifid Not Found
ttwid 已不再被认可为有效凭据（ttwid 注册接口仍返回 200，但 detail 接口照样拒绝），
换域（www.douyin.com / www.iesdouyin.com）、换 aid、伪造 uifid/msToken 全部无效。
实测只有携带浏览器 secsdk 生成的 UIFID_TEMP/UIFID、odin_tt、__ac_signature 等
凭据的**真实浏览器会话**才能拿到数据。

因此这里改为常驻一个 headless Chromium：打开作品页 → 直接取页面自身发出的
detail 响应（页面自己会带上全部签名与凭据）→ 返回完整的 aweme_detail。
这样既能拿到实况照片的 MOV 分量（images[].video.play_addr），也能拿到
视频的多档 bit_rate（最高码率）。失败时退化为在页面上下文里主动 fetch。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional

DETAIL_MARK = "/aweme/v1/web/aweme/detail/"

# 页面上下文内的兜底请求（携带浏览器 cookie）
_FETCH_JS = """
async (itemId) => {
    const u = `https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=${itemId}&aid=6383&device_platform=webapp`;
    try {
        const resp = await fetch(u, { credentials: 'include' });
        const text = await resp.text();
        let data = null;
        try { data = JSON.parse(text); } catch (e) { data = null; }
        return { status: resp.status, data: data, head: text.slice(0, 220) };
    } catch (e) {
        return { error: String(e) };
    }
}
"""

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--mute-audio",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
]


def log(msg: str) -> None:
    print(f"[douyin-browser] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


class DouyinBrowser:
    """常驻 Chromium 会话，串行解析抖音作品详情。

    复用同一个 persistent context：首次访问时页面 secsdk 会自行注册
    UIFID/ttwid/odin_tt 等凭据并落到 profile 目录，后续请求直接复用，
    既省去每次冷启动 Chromium 的开销，也避免频繁触发风控。
    """

    def __init__(
        self,
        profile_dir: str,
        headless: bool = True,
        recycle_uses: int = 150,
        recycle_seconds: int = 6 * 3600,
    ) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.recycle_uses = recycle_uses
        self.recycle_seconds = recycle_seconds

        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = asyncio.Lock()
        self._uses = 0
        self._born = 0.0
        self._warmed = False

    # ------------------------------------------------------------------ 生命周期
    async def _launch_locked(self) -> None:
        """必须在持有 self._lock 的情况下调用"""
        from playwright.async_api import async_playwright

        await self._close_locked()

        if self._pw is None:
            self._pw = await async_playwright().start()

        os.makedirs(self.profile_dir, exist_ok=True)
        self._ctx = await self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=_LAUNCH_ARGS,
        )
        self._ctx.set_default_timeout(45000)
        self._page = await self._ctx.new_page()
        self._uses = 0
        self._born = time.time()
        log(f"Chromium 会话已启动 (headless={self.headless}, profile={self.profile_dir})")

    async def _close_locked(self) -> None:
        for obj, closer in ((self._page, "close"), (self._ctx, "close")):
            if obj is not None:
                try:
                    await getattr(obj, closer)()
                except Exception:
                    pass
        self._page = None
        self._ctx = None

    async def _ensure_locked(self) -> None:
        dead = self._ctx is None or self._page is None or self._page.is_closed()
        aged = (time.time() - self._born) > self.recycle_seconds or self._uses >= self.recycle_uses
        if dead or aged:
            if aged and not dead:
                log(f"回收会话（已用 {self._uses} 次 / 存活 {int(time.time() - self._born)}s）")
            await self._launch_locked()

    async def _warmup_locked(self) -> None:
        """首次访问 douyin.com，让页面 secsdk 注册 UIFID 等凭据"""
        if self._warmed:
            return
        try:
            await self._page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
            await self._page.wait_for_timeout(1500)
            self._warmed = True
            log("预热完成（凭据已注册到 profile）")
        except Exception as exc:
            log(f"预热失败（不影响后续，首次解析会自动重试）: {type(exc).__name__}: {exc}")

    async def warmup(self) -> None:
        """启动时预热：拉起浏览器并注册凭据"""
        async with self._lock:
            await self._ensure_locked()
            await self._warmup_locked()

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None
            self._warmed = False

    # ------------------------------------------------------------------ 取详情
    async def _fetch_in_page_locked(self, item_id: str) -> Optional[Dict[str, Any]]:
        res = await self._page.evaluate(_FETCH_JS, item_id)
        if isinstance(res, dict):
            data = res.get("data") or {}
            detail = data.get("aweme_detail")
            if detail:
                return detail
            log(f"页面内 fetch 未拿到 detail: status={res.get('status')} head={str(res.get('head'))[:120]!r}")
        return None

    async def get_detail(self, item_id: str, timeout_ms: int = 30000) -> Optional[Dict[str, Any]]:
        """返回 aweme_detail（完整作品数据）或 None"""
        async with self._lock:
            await self._ensure_locked()
            await self._warmup_locked()

            page = self._page
            url = f"https://www.douyin.com/video/{item_id}"
            matcher = lambda r: DETAIL_MARK in r.url and f"aweme_id={item_id}" in r.url  # noqa: E731

            # 首选：直接捕获页面自身发出的 detail 请求（带全套签名）
            try:
                async with page.expect_response(matcher, timeout=timeout_ms) as waiter:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                resp = await waiter.value
                data = await resp.json()
                detail = (data or {}).get("aweme_detail")
                if detail:
                    self._uses += 1
                    return detail
                log(f"{item_id}: 页面请求返回但无 aweme_detail（status={resp.status}）")
            except Exception as exc:
                log(f"{item_id}: 捕获页面 detail 请求失败 {type(exc).__name__}: {str(exc)[:160]}")
                try:
                    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    await self._launch_locked()

            # 兜底：在页面上下文里主动 fetch
            try:
                detail = await self._fetch_in_page_locked(item_id)
                if detail:
                    self._uses += 1
                    return detail
            except Exception as exc:
                log(f"{item_id}: 页面内 fetch 异常 {type(exc).__name__}: {str(exc)[:160]}")

            self._uses += 1
            return None


_browser: Optional[DouyinBrowser] = None


def profile_dir() -> str:
    return os.getenv("DOUYIN_PROFILE_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "chrome_profile"
    )


def headless() -> bool:
    return (os.getenv("DOUYIN_HEADLESS", "1") or "1").strip() not in ("0", "false", "False")


async def get_browser() -> DouyinBrowser:
    global _browser
    if _browser is None:
        _browser = DouyinBrowser(profile_dir(), headless=headless())
    return _browser


async def close_browser() -> None:
    global _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
