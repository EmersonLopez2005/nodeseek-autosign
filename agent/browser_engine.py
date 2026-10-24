# -*- coding: utf-8 -*-
"""
浏览器引擎适配层。

NodeSeek 现在的登录接口需要三样东西（详见 README 的“现状分析”）：
  1. x-captcha-token   —— Cloudflare Turnstile token
  2. x-integrity-token —— FingerprintJS 算出来的 visitorId（浏览器指纹）
  3. x-security-token  —— 页面里 localStorage 的 security_token

后两样都必须在真实浏览器里由站点自己的 JS 计算/读取，
所以“纯 HTTP + 打码平台”这条路已经走不通了，必须让家宽机器上跑真浏览器。

本模块把三种引擎统一成同一个接口：

    with browser_page(engine="auto", user_data_dir=..., proxy=...) as page:
        page.goto(...)

引擎优先级：camoufox > patchright > playwright
  - camoufox   : Firefox 定制版，过 Cloudflare 最稳（推荐）
  - patchright : Playwright 的反检测补丁版，Chromium
  - playwright : 官方版，最后的兜底
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from typing import Optional
from urllib.parse import urlparse

DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
DEFAULT_LOCALE = "zh-CN"
DEFAULT_TZ = "Asia/Shanghai"


# --------------------------------------------------------------------------
# 虚拟显示（Linux 有头模式必需）
# --------------------------------------------------------------------------
def _ensure_display() -> Optional[subprocess.Popen]:
    """Linux 上没有 DISPLAY 时自动拉起 Xvfb，返回进程句柄（用完要 terminate）。

    有头浏览器是过 Cloudflare 的前提，但 VPS / Docker 里默认没有 X server。
    以前要用户自己用 xvfb-run 包一层，挂到青龙上就做不到了，
    所以这里直接在代码里兜住。
    """
    if os.name != "posix":
        return None
    if os.getenv("DISPLAY"):
        return None
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        raise RuntimeError(
            "有头模式需要 X 服务，但这台机器既没有 DISPLAY 也没有 Xvfb。\n"
            "  Debian/Ubuntu: sudo apt install -y xvfb\n"
            "  Alpine:        apk add xvfb\n"
            "  或者显式设 LOGIN_LOCAL_HEADLESS=true 走无头（大概率过不了 Cloudflare）"
        )
    for disp in range(99, 110):
        lock = f"/tmp/.X{disp}-lock"
        if os.path.exists(f"/tmp/.X11-unix/X{disp}") or os.path.exists(lock):
            continue
        proc = subprocess.Popen(
            [xvfb, f":{disp}", "-screen", "0", "1440x900x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if os.path.exists(f"/tmp/.X11-unix/X{disp}"):
                os.environ["DISPLAY"] = f":{disp}"
                print(f"[engine] 已自动启动 Xvfb（DISPLAY=:{disp}）")
                return proc
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        try:
            proc.terminate()
        except Exception:
            pass
    raise RuntimeError("Xvfb 启动失败（试了 :99~:109 都起不来）")


# --------------------------------------------------------------------------
# 引擎探测
# --------------------------------------------------------------------------
def _has_camoufox() -> bool:
    try:
        import camoufox  # noqa: F401
        return True
    except Exception:
        return False


def _has_patchright() -> bool:
    try:
        import patchright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def _has_playwright() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def resolve_engine(preferred: Optional[str] = None) -> str:
    """返回实际可用的引擎名。preferred 为 auto/None 时按优先级自动挑。"""
    preferred = (preferred or "auto").strip().lower()
    if preferred in ("camoufox", "patchright", "playwright"):
        if preferred == "camoufox" and _has_camoufox():
            return "camoufox"
        if preferred == "patchright" and _has_patchright():
            return "patchright"
        if preferred == "playwright" and _has_playwright():
            return "playwright"
        # 指定的引擎没装，继续往下自动探测，但要打日志
        print(f"[engine] 指定的引擎 {preferred} 不可用，自动降级")
    for name, check in (
        ("camoufox", _has_camoufox),
        ("patchright", _has_patchright),
        ("playwright", _has_playwright),
    ):
        if check():
            return name
    raise RuntimeError(
        "没有可用的浏览器引擎。请安装其中之一：\n"
        "  pip install camoufox[geoip] && python -m camoufox fetch\n"
        "  pip install patchright && patchright install chromium\n"
        "  pip install playwright && playwright install chromium"
    )


def parse_proxy(proxy: Optional[str]) -> Optional[dict]:
    """把 http://user:pass@host:port 解析成 playwright/camoufox 的 proxy 字典。"""
    if not proxy:
        return None
    if isinstance(proxy, dict):
        return proxy
    proxy = proxy.strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    u = urlparse(proxy)
    if not u.hostname:
        return None
    server = f"{u.scheme}://{u.hostname}"
    if u.port:
        server += f":{u.port}"
    cfg = {"server": server}
    if u.username:
        cfg["username"] = u.username
    if u.password:
        cfg["password"] = u.password
    return cfg


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------
@contextmanager
def browser_page(
    engine: str = "auto",
    headless: bool = True,
    user_data_dir: Optional[str] = None,
    proxy: Optional[str] = None,
    locale: str = DEFAULT_LOCALE,
    timezone: str = DEFAULT_TZ,
    slow_mo: int = 0,
    humanize: bool = True,
):
    """
    打开一个浏览器页面，yield 出 playwright 的 Page 对象。

    使用 user_data_dir 时是持久化 profile（会保留 cookie / localStorage / 指纹），
    这对 NodeSeek 很重要：同一个账号的 visitorId 和 cf_clearance 保持稳定，
    就不会每次登录都重新过一遍 CF 挑战。
    """
    name = resolve_engine(engine)
    proxy_cfg = parse_proxy(proxy)

    if user_data_dir:
        os.makedirs(user_data_dir, exist_ok=True)

    xvfb_proc = None
    if not headless:
        xvfb_proc = _ensure_display()

    try:
        if name == "camoufox":
            with _camoufox_page(headless, user_data_dir, proxy_cfg, locale, humanize) as page:
                yield page
        else:
            with _playwright_page(
                name, headless, user_data_dir, proxy_cfg, locale, timezone, slow_mo
            ) as page:
                yield page
    finally:
        if xvfb_proc is not None:
            try:
                xvfb_proc.terminate()
            except Exception:
                pass
            os.environ.pop("DISPLAY", None)


@contextmanager
def _camoufox_page(headless, user_data_dir, proxy_cfg, locale, humanize):
    from camoufox.sync_api import Camoufox

    kwargs = dict(
        headless=headless,
        humanize=humanize,
        locale=[locale, "zh"],
        user_data_dir=user_data_dir,
        proxy=proxy_cfg,
        os=["windows"],
        # 关掉一些非必要的数据上报，Docker 里更稳
        geoip=False,
    )
    try:
        cm = Camoufox(**kwargs)
    except TypeError:
        # 老版本 camoufox 没有某些参数，退到最小集合
        cm = Camoufox(headless=headless, user_data_dir=user_data_dir, proxy=proxy_cfg)

    with cm as browser:
        page = browser.new_page()
        try:
            yield page
        finally:
            try:
                page.close()
            except Exception:
                pass


@contextmanager
def _playwright_page(name, headless, user_data_dir, proxy_cfg, locale, timezone, slow_mo):
    if name == "patchright":
        from patchright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright

    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1440,900",
        # 以下都是纯“减负”参数，不碰 canvas/webgl/字体，指纹不受影响
        "--disable-background-networking",
        "--disable-backgrounding-occluded-windows",
        "--disable-breakpad",
        "--disable-client-side-phishing-detection",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-domain-reliability",
        "--disable-extensions",
        "--disable-features=Translate,MediaRouter,OptimizationHints,InterestFeedContentSuggestions",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-default-browser-check",
        "--no-first-run",
        "--renderer-process-limit=2",
        "--js-flags=--max-old-space-size=384",
    ]
    ctx_kwargs = dict(
        locale=locale,
        timezone_id=timezone,
        viewport=DEFAULT_VIEWPORT,
        ignore_https_errors=False,
    )

    p = sync_playwright().start()
    try:
        if user_data_dir:
            context = p.chromium.launch_persistent_context(
                user_data_dir,
                headless=headless,
                args=args,
                proxy=proxy_cfg,
                slow_mo=slow_mo,
                **ctx_kwargs,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                yield page
            finally:
                try:
                    context.close()
                except Exception:
                    pass
        else:
            browser = p.chromium.launch(
                headless=headless, args=args, proxy=proxy_cfg, slow_mo=slow_mo
            )
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            try:
                yield page
            finally:
                for closer in (context.close, browser.close):
                    try:
                        closer()
                    except Exception:
                        pass
    finally:
        try:
            p.stop()
        except Exception:
            pass
