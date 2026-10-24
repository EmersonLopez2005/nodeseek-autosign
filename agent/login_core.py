# -*- coding: utf-8 -*-
"""
NodeSeek 浏览器登录 / 签到核心逻辑。

为什么必须在浏览器里做（2026-10 实测前端代码）：

  POST /api/account/signIn
    headers:
      content-type: application/json
      x-captcha-token:  <Cloudflare Turnstile token>
      x-captcha-source: turnstile
      x-security-token: <localStorage.security_token>
      x-integrity-token:<FingerprintJS visitorId>
    body: {username, password}

  - Turnstile token 只有真浏览器 + 干净 IP 才能稳定拿到；
  - visitorId 由 FingerprintJS 在前端采集 canvas/webgl/audio/fonts 等几十项特征算出，
    HTTP 客户端没法伪造；
  - security_token 存在页面 localStorage 里，同样是浏览器环境产物。

所以：登录放在家宽机器的真浏览器里跑，拿到 sessionid/cf_clearance 之后，
再交给青龙里的 curl_cffi 去签到（签到接口只认 cookie）。
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import Optional

BOARD_URL = "https://www.nodeseek.com/board"
LOGIN_URL = "https://www.nodeseek.com/signIn.html"
ORIGIN = "https://www.nodeseek.com"
SIGNIN_API_MARK = "/api/account/signIn"

USERNAME_SELECTORS = [
    "#stacked-email",
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[type="text"]',
]
PASSWORD_SELECTORS = [
    "#stacked-password",
    'input[type="password"]',
]
SUBMIT_SELECTORS = [
    'form button[type="submit"]',
    "button:has-text('登录')",
    "button:has-text('登 录')",
]


class LoginError(Exception):
    """带错误码的登录异常，方便上游区分是“要重试”还是“要人工介入”。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _probe_webgl(page):
    """把 WebGL 渲染器打进日志 —— 一锤定音的指纹证据。

    Xvfb 下没装 Mesa 时是 "Google SwiftShader"（CF 已知 bot 特征，
    会被 Turnstile onError 直接拒绝）；
    装了 libgl1-mesa-dri 后是 "llvmpipe"，是正常桌面会有的值。
    连 WebGL 都没有（NO_WEBGL）说明 GPU 进程没起来，往往缺 libegl1/libgles2。
    """
    try:
        r = page.evaluate(
            """() => {
                try {
                    const c = document.createElement('canvas');
                    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
                    if (!gl) return 'NO_WEBGL';
                    const ext = gl.getExtension('WEBGL_debug_renderer_info');
                    if (!ext) return 'NO_EXT';
                    return String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL));
                } catch (e) { return 'ERR_' + (e.message || e); }
            }"""
        )
        r = (r or "").strip()
        if "swiftshader" in r.lower():
            _log(f"WebGL 渲染器：{r}  ← CF 已知 bot 特征，执行：apt install -y libgl1 libgl1-mesa-dri")
        elif r in ("NO_WEBGL", "NO_EXT", "ERR", "") or r.startswith("ERR_"):
            _log(f"WebGL 渲染器：{r}  ← 浏览器完全没有 WebGL！GPU 进程没起来。"
                 f"先试：apt install -y libegl1 libgles2；仍无效就换 AGENT_ENGINE=camoufox")
        else:
            _log(f"WebGL 渲染器：{r}")
    except Exception as e:
        _log(f"WebGL 探测异常：{type(e).__name__}")


def _dump_gpu_info(page):
    """更深的 GPU 诊断：Chromium 内部看到的 GL 实现与 GPU 特性。"""
    try:
        info = page.evaluate(
            """() => {
                const out = {};
                try {
                    const c = document.createElement('canvas');
                    const gl = c.getContext('webgl');
                    if (gl) {
                        const ext = gl.getExtension('WEBGL_debug_renderer_info');
                        out.renderer = ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) : 'NO_EXT';
                        out.vendor = ext ? String(gl.getParameter(ext.UNMASKED_VENDOR_WEBGL)) : '?';
                        out.version = String(gl.getParameter(gl.VERSION));
                        out.glsl = String(gl.getParameter(gl.SHADING_LANGUAGE_VERSION));
                    } else {
                        out.renderer = 'NO_WEBGL';
                    }
                } catch (e) { out.err = String(e.message || e); }
                return out;
            }"""
        )
        _log(f"GPU 详情：{info}")
    except Exception as e:
        _log(f"GPU 详情获取失败：{type(e).__name__}")


def _first_visible(page, selectors, timeout=8000):
    """依次尝试选择器，返回第一个可见元素。"""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        page.wait_for_timeout(300)
    return None


def _page_is_challenge(page) -> bool:
    """判断当前是不是 Cloudflare 的整页挑战。

    注意：不能用“页面源码里出现 cf-chl”来判断——正常页面也会引用相关脚本，
    实测会误判。这里以标题 + 挑战容器为准。
    """
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    hints = ("just a moment", "请稍候", "checking your browser",
             "attention required", "正在验证您是否是真人", "正在进行安全验证")
    if any(h in title for h in hints):
        return True
    try:
        if page.locator("#challenge-stage, #cf-challenge-running, #challenge-form").count():
            return True
    except Exception:
        pass
    return False


def _challenge_cleared(page) -> bool:
    """挑战是否真的过去了。

    不能只看“标题不是挑战页”——切换瞬间标题可能是空的，会误判通过。
    这里要求落到 NodeSeek 的正式页面（标题带 NodeSeek 或登录表单已渲染）。
    """
    if _page_is_challenge(page):
        return False
    try:
        if page.locator("#stacked-email, #stacked-password").count():
            return True
    except Exception:
        pass
    try:
        return "nodeseek" in (page.title() or "").lower()
    except Exception:
        return False


def _wait_out_challenge(page, timeout=50):
    """遇到整页 CF 挑战时等它自己过。

    注意：整页挑战的 Turnstile 同样是「点一次 → 转圈计算 → 放行」。
    计算期间重复点击会取消当前验证重新开始，等于永远过不去，
    所以这里最多点 2 次，间隔 20 秒。
    """
    if not _page_is_challenge(page):
        return True
    _log("检测到 Cloudflare 整页挑战，等待自动通过…")
    deadline = time.time() + timeout
    clicked = 0
    last_click = 0.0
    stable = 0
    while time.time() < deadline:
        page.wait_for_timeout(1500)
        if _challenge_cleared(page):
            stable += 1
            if stable >= 2:
                _log("整页挑战已通过")
                return True
            continue
        stable = 0
        if not _page_is_challenge(page):
            # 既不是挑战页、也没加载出正式页面，多半在跳转中
            page.wait_for_timeout(1000)
            continue
        # 只在前 20 秒内补点，避免打断正在进行的验证
        if clicked < 2 and time.time() - last_click > 20:
            _click_turnstile_checkbox(page)
            clicked += 1
            last_click = time.time()
    _log(f"整页挑战 {timeout} 秒未通过")
    return False


def _turnstile_token(page) -> Optional[str]:
    """读取 Turnstile hidden input 里的 token。"""
    try:
        return page.evaluate(
            """() => {
                const el = document.querySelector('input[name="cf-turnstile-response"]');
                if (el && el.value) return el.value;
                const tas = document.querySelectorAll('textarea[name="cf-turnstile-response"]');
                for (const t of tas) { if (t.value) return t.value; }
                return null;
            }"""
        )
    except Exception:
        return None


def _turnstile_token_detail(page) -> Optional[str]:
    """读取 token 并带上长度/前缀，用于判断 token 是否合法（正常 Turnstile token 以 0. 或 1. 开头）。"""
    tok = _turnstile_token(page)
    if tok:
        return f"{len(tok)}字符/前缀{tok[:8]!r}"
    return None


def _widget_box(page):
    """拿到 Turnstile 组件 iframe 的位置，用于真实鼠标点击。"""
    try:
        for el in page.query_selector_all('iframe[src*="challenges.cloudflare.com"]'):
            box = el.bounding_box()
            if box and box.get("width", 0) > 100:
                return box
    except Exception:
        pass
    return None


def _click_turnstile_checkbox(page) -> bool:
    """点 Turnstile 的复选框。

    实测（2026-10，AT&T 住宅 IP）：两件事都会触发 Turnstile onError
    （页面弹「人机验证服务加载失败」）：
      1. frame.locator('input[type=checkbox]').click() —— 点到隐藏元素；
      2. page.mouse.click(x, y) —— mousedown/mouseup 间隔约等于 0，
         人类按住时长是 60~150ms，hold≈0 是经典 bot 信号。

    所以这里：从随机起点多步移过去（自然轨迹）→ 悬停一瞬 →
    按下 → 按住 60~140ms → 松开。
    """
    box = _widget_box(page)
    if box:
        try:
            x = box["x"] + 26
            y = box["y"] + box["height"] / 2
            # 从页面上的随机起点出发，而不是凭空出现在组件上
            sx = max(0, box["x"] + random.uniform(-400, -120))
            sy = max(0, y + random.uniform(-200, 200))
            try:
                page.mouse.move(sx, sy)
                page.wait_for_timeout(random.randint(150, 400))
            except Exception:
                pass
            page.mouse.move(x, y, steps=random.randint(15, 28))
            # 悬停，Turnstile 会记录 hover 时长
            page.wait_for_timeout(random.randint(150, 400))
            # 关键：按下和松开之间要有真实的按住时长
            page.mouse.down()
            page.wait_for_timeout(random.randint(60, 140))
            page.mouse.up()
            _log("已点击 Turnstile 复选框（按住时长已模拟）")
            return True
        except Exception as e:
            _log(f"坐标点击失败：{type(e).__name__}")

    # 兜底：在 iframe 内点 label
    try:
        for fr in page.frames:
            if "challenges.cloudflare.com" not in (fr.url or ""):
                continue
            loc = fr.locator("label").first
            if loc.count() > 0:
                loc.click(timeout=2500)
                _log("已点击 Turnstile label")
                return True
    except Exception:
        pass
    return False


def _wait_turnstile(page, timeout=40):
    """等 Turnstile 完成验证。

    实测教训（2026-10，AT&T 住宅 IP，Xvfb 有头 patchright）：
      1) 点完复选框后组件转圈 3~15 秒，期间**再点会取消并重新开始**，
         所以绝对不能每 4 秒点一次（旧版就是因此永远过不去）；
      2) 但也**不能不点**——托管模式并不总是自动放行，该点还得点。
         上一次改过头，状态误判成 verifying 后一次都没点，白等 40 秒。

    所以策略是：先给 8 秒自动通过的机会 → 没动静就点一次 → 之后只等不点
    （只有明确 error 才允许再点一次）。

    返回 token / "widget-ok"；拿不到明确信号返回 None（交给提交结果判断）。
    """
    start = time.time()
    deadline = start + timeout
    clicked = 0
    last_click = 0.0
    last_state = None
    last_tok = None
    tok_stable = 0

    # 组件是异步渲染的，太早点会点到空白处；
    # 顺便像人一样晃一下鼠标——整个会话期间鼠标从没动过，本身就很可疑
    try:
        page.mouse.move(
            random.uniform(300, 1100), random.uniform(200, 700),
            steps=random.randint(10, 25),
        )
    except Exception:
        pass
    page.wait_for_timeout(random.randint(1500, 2500))

    while time.time() < deadline:
        state = turnstile_state(page)
        tok = _turnstile_token(page)

        # 组件明确 success 是最强信号——此时 token 一定是新鲜有效的。
        # 反过来「有 token 但组件还没 success」很可能是 hidden input 里的过期/残留 token，
        # 拿去提交必然被判「验证码无效」。
        if state == "success" and tok:
            _log(f"Turnstile 验证通过（{len(tok)} 字符）")
            return tok
        if state == "success":
            _log(f"Turnstile 组件已通过（{time.time() - start:.0f}s），但没读到 token，继续等")
        elif tok:
            # 有 token 但组件状态不是 success：可能是状态误判。
            # 若同一个 token 连续多轮稳定存在，说明验证其实已经完成、token 新鲜，
            # 允许提交（兜底，避免因状态误判卡死）。
            if tok == last_tok:
                tok_stable += 1
                if tok_stable >= 3 and time.time() - start > 6:
                    _log(f"token 已稳定存在 {tok_stable} 轮（组件状态 {state} 可能误判），按验证通过处理")
                    return tok
            else:
                tok_stable = 1
                last_tok = tok
            if state != last_state:
                _log(f"已有 token（{_turnstile_token_detail(page)}）但组件状态为 {state}，等待组件确认…")
            last_state = state
        elif state != last_state:
            _log(f"Turnstile 组件状态：{state}")
            last_state = state

        if "人机验证服务加载失败" in _body_text(page):
            # 这不是网络问题（网络通的时候照样弹）——是 Turnstile onError：
            # 点击交互或浏览器指纹被判 bot。重载页面换一个新的 widget 重来，
            # 只有连续多次都这样才彻底放弃。
            raise LoginError(
                "TURNSTILE_INTERACT",
                "Turnstile 拒绝了本次交互（onError）：点击特征或指纹被判定为机器人，"
                "重载页面重试中",
            )

        elapsed = time.time() - start
        # 8 秒后还没过就点一次。不看 state：
        #   pending   → 还没开始，需要点
        #   verifying → 可能真是转圈，也可能是状态误判；点一下最多重来一次，
        #               总比一直干等强
        if clicked == 0 and elapsed > 8 and time.time() - last_click > 3:
            ok = _click_turnstile_checkbox(page)
            last_click = time.time()
            if ok:
                clicked = 1
                page.wait_for_timeout(600)
                _log(f"点击后状态：{turnstile_state(page)}")
            else:
                _log("没定位到 Turnstile 组件，3 秒后重试点击")
        elif state == "error" and clicked < 2 and time.time() - last_click > 10:
            _log("Turnstile 报错，重新点一次")
            _click_turnstile_checkbox(page)
            clicked += 1
            last_click = time.time()

        page.wait_for_timeout(800)

    # 超时了。若此时 hidden input 里还有个 token，拿去做最后一次尝试
    # （总比空手提交强）；否则返回 None 交给提交结果判断。
    tok = _turnstile_token(page)
    if tok:
        _log(f"Turnstile {timeout} 秒内状态未确认，但拿到 token（{len(tok)} 字符），提交试试")
        return tok
    _log(f"Turnstile {timeout} 秒内没拿到明确信号，直接提交看接口怎么说")
    return None


def _reload_login_page(page):
    """重新加载登录页，拿到一个新的用户名输入框。

    重试时必须走“整页刷新”而不是原地再点：Turnstile 的验证态绑在页面实例上，
    原地重试会沿用已经失效的组件，表现为反复转圈。
    """
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    _wait_out_challenge(page)
    page.wait_for_timeout(1500)
    loc = _first_visible(page, USERNAME_SELECTORS, timeout=8000)
    if loc is None:
        raise LoginError("PAGE_ERROR", "重新加载后登录表单没出现（站点改版或被风控）")
    return loc


def _dismiss_dialog(page):
    """关掉站点 mscAlert 之类的弹窗，否则它会挡住输入框。"""
    for txt in ["OK", "确定", "知道了", "关闭"]:
        try:
            loc = page.get_by_role("button", name=txt).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=1500)
                page.wait_for_timeout(300)
                return
        except Exception:
            continue


def _install_signin_interceptor(page):
    """用 page.route 拦截 signIn 请求，去掉 Accept-Encoding: br，让响应体可读。

    根因（2026-10 实测）：NodeSeek 的 /api/account/signIn 响应是
    `content-encoding: br`（Brotli 压缩），而 Camoufox（Firefox 内核）下
    Playwright 读 body 会报 NS_ERROR_INVALID_CONTENT_ENCODING / resp.body() 直接 Error，
    导致拿不到响应原文，只能看到「HTTP 200」。

    这里用 page.route 在请求发出前把 `Accept-Encoding` 改成 `gzip`（去掉 br），
    服务器就会回 gzip 或明文，响应体就能正常读了。

    同时保留 JS 层的 fetch/XHR hook 作为双保险（有些场景 route 不触发）。
    """
    try:
        page.route(
            lambda r: SIGNIN_API_MARK in (r.url or ""),
            lambda route: route.continue_(
                headers={**route.request.headers, "Accept-Encoding": "gzip, deflate"}
            ),
        )
    except Exception as e:
        _log(f"page.route 拦截安装失败：{type(e).__name__}: {e}")

    page.evaluate(
        """() => {
            if (window.__ns_signin_hooked) return;
            window.__ns_signin_hooked = true;
            window.__ns_signin_debug = null;

            const isSignInUrl = (u) => u && u.indexOf('/api/account/signIn') !== -1;

            // ---- 1) 拦截 fetch ----
            try {
                const origFetch = window.fetch.bind(window);
                window.fetch = async function(input, init) {
                    const url = typeof input === 'string' ? input : (input && input.url) || '';
                    if (isSignInUrl(url)) {
                        try {
                            const resp = await origFetch(input, init);
                            const clone = resp.clone();
                            let bodyText = '';
                            try { bodyText = await clone.text(); } catch (e) { bodyText = 'READ_ERR:' + e.message; }
                            window.__ns_signin_debug = {
                                via: 'fetch', url: url, status: resp.status,
                                respBody: bodyText.slice(0, 3000)
                            };
                            return resp;
                        } catch (e) {
                            window.__ns_signin_debug = { via: 'fetch', url: url, fetchErr: String(e && e.message || e) };
                            throw e;
                        }
                    }
                    return origFetch(input, init);
                };
            } catch (e) {}

            // ---- 2) 拦截 XMLHttpRequest ----
            try {
                const origOpen = XMLHttpRequest.prototype.open;
                const origSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url) {
                    this.__ns_url = url;
                    return origOpen.apply(this, arguments);
                };
                XMLHttpRequest.prototype.send = function() {
                    const xhr = this;
                    if (isSignInUrl(xhr.__ns_url)) {
                        xhr.addEventListener('load', function() {
                            let bodyText = '';
                            try { bodyText = xhr.responseText; } catch (e) { bodyText = 'READ_ERR:' + e.message; }
                            window.__ns_signin_debug = {
                                via: 'xhr', url: xhr.__ns_url, status: xhr.status,
                                respBody: (bodyText || '').slice(0, 3000)
                            };
                        });
                        xhr.addEventListener('error', function() {
                            window.__ns_signin_debug = { via: 'xhr', url: xhr.__ns_url, xhrErr: 'network error' };
                        });
                    }
                    return origSend.apply(this, arguments);
                };
            } catch (e) {}
        }"""
    )


def _read_signin_debug(page):
    """读回 signIn 的响应调试信息，供失败判定用。"""
    try:
        return page.evaluate("() => window.__ns_signin_debug")
    except Exception:
        return None


def _read_login_error(page) -> Optional[str]:
    """登录失败后，从页面 DOM 里读错误提示文字（不依赖读响应体）。

    NodeSeek 登录失败时会在页面上渲染提示（密码错误 / 验证码错误 / 账号不存在…），
    这些文字就在 DOM 里，比读 br 压缩的响应体可靠得多。
    同时检查当前 URL 是否已跳转到 /board（登录成功的信号）。
    """
    url = ""
    try:
        url = page.url or ""
    except Exception:
        pass
    if "/board" in url or "/home" in url:
        _log(f"页面已跳转到 {url}（可能登录成功）")
    try:
        err = page.evaluate(
            """() => {
                const sels = [
                    '.msc-alert', '.alert', '.error', '.invalid-feedback',
                    '[class*="error"]', '[class*="alert"]', '[class*="message"]',
                    '.text-red-500', '.text-red-600', '[role="alert"]'
                ];
                const out = [];
                for (const s of sels) {
                    for (const el of document.querySelectorAll(s)) {
                        const t = (el.innerText || el.textContent || '').trim();
                        if (t && t.length < 200) out.push(t);
                    }
                }
                return Array.from(new Set(out)).slice(0, 5);
            }"""
        )
        if err and isinstance(err, list) and err:
            return " | ".join(err)
    except Exception:
        pass
    return None


def _body_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""


def turnstile_state(page) -> str:
    """Turnstile 组件当前状态：success / verifying / pending / error / unknown。

    为什么要判状态而不是判 token：
      NodeSeek 走的是 Turnstile 的 callback 模式，token 只存在页面 JS 变量里，
      不会写进 input[name="cf-turnstile-response"]。
      所以「读不到 token」是正常的，不代表验证失败。

    verifying 表示正在转圈计算，此时**绝对不能再点**，点了会重新开始。
    """
    found = False
    for fr in page.frames:
        if "challenges.cloudflare.com" not in (fr.url or ""):
            continue
        found = True
        try:
            probe = fr.evaluate(
                """() => {
                    const q = (s) => document.querySelector(s);
                    const text = (document.body && document.body.innerText || '').trim();
                    const cb = q('input[type="checkbox"]');
                    // 注意：不能用 svg circle 判“转圈”——组件本身那个圆形外框就是 circle，
                    // 会导致初始状态被误判成 verifying，从而永远不点击。
                    const hasSpinner = !!(q('#spinner') || q('.spinner') || q('[class*="spinner"]'));
                    const hasSuccess = !!(q('#success') || q('.success'));
                    return {
                        checked: cb ? !!cb.checked : null,
                        hasCb: !!cb,
                        hasSuccess,
                        hasSpinner,
                        text,
                    };
                }"""
            )
        except Exception:
            continue

        if probe.get("checked") or probe.get("hasSuccess"):
            return "success"
        text = (probe.get("text") or "")
        low = text.lower()
        if any(k in low for k in ("error", "failed", "无法", "失败", "重试", "retry")):
            return "error"
        if any(k in low for k in ("成功", "success", "verified", "验证通过")):
            return "success"
        # 只有在明确的 spinner 元素存在时才算“计算中”
        if probe.get("hasSpinner"):
            return "verifying"
    return "pending" if found else "unknown"


def _turnstile_widget_ok(page) -> bool:
    """Turnstile 组件里是否已经显示“验证成功”。"""
    return turnstile_state(page) == "success"


def cookies_to_string(cookies, domain_hint="nodeseek.com") -> str:
    picked = {}
    for c in cookies or []:
        dom = (c.get("domain") or "").lstrip(".")
        if domain_hint and domain_hint not in dom:
            continue
        picked[c["name"]] = c["value"]
    # sessionid 放最前面，方便人眼核对
    ordered = []
    if "sessionid" in picked:
        ordered.append(f"sessionid={picked.pop('sessionid')}")
    ordered += [f"{k}={v}" for k, v in picked.items()]
    return "; ".join(ordered)


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------
def login(page, username: str, password: str, totp_secret: Optional[str] = None,
          max_attempts: int = 4) -> dict:
    """
    在已打开的 page 上完成登录，返回 cookie 字符串。

    成功返回：{"cookie": "...", "cookies": [...], "username": "..."}
    失败抛 LoginError。
    """
    _log(f"打开登录页 {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    _probe_webgl(page)
    _dump_gpu_info(page)
    if not _wait_out_challenge(page):
        _debug_dump(page, "challenge_stuck")
        raise LoginError(
            "WAF_BLOCKED",
            "Cloudflare 整页挑战没通过。常见原因：①无头浏览器（必须用有头+Xvfb）；"
            "②出口 IP 被标记；③访问不到 challenges.cloudflare.com",
        )
    page.wait_for_timeout(1500)

    # 有些版本的登录页默认是“邮箱验证码”模式，需要切到密码模式
    user_input = _first_visible(page, USERNAME_SELECTORS, timeout=6000)
    if user_input is None:
        for txt in ["密码登录", "密码", "账号密码"]:
            try:
                loc = page.get_by_text(txt, exact=True).first
                if loc.count() > 0:
                    loc.click(timeout=2000)
                    page.wait_for_timeout(800)
                    break
            except Exception:
                continue
        user_input = _first_visible(page, USERNAME_SELECTORS, timeout=6000)

    if user_input is None:
        _debug_dump(page, "login_page")
        raise LoginError("PAGE_ERROR", "登录页找不到用户名输入框（站点可能改版），已截图 data/debug_login_page.png")

    last_msg = ""
    for attempt in range(1, max_attempts + 1):
        _log(f"第 {attempt}/{max_attempts} 次尝试登录：{username}")
        _dismiss_dialog(page)

        pwd_input = _first_visible(page, PASSWORD_SELECTORS, timeout=5000)
        if pwd_input is None:
            raise LoginError("PAGE_ERROR", "登录页找不到密码输入框")

        # 清空后重新填，避免上次残留
        for loc, value in ((user_input, username), (pwd_input, password)):
            try:
                loc.click(timeout=3000)
                loc.fill("")
                loc.type(value, delay=random.randint(40, 90))
            except Exception:
                loc.fill(value)
            page.wait_for_timeout(random.randint(200, 500))

        # 填完表先像人一样顿一下，再去看验证组件
        page.wait_for_timeout(random.randint(600, 1500))
        try:
            _wait_turnstile(page, timeout=40)
        except LoginError as e:
            if e.code == "TURNSTILE_INTERACT" and attempt < max_attempts:
                _dismiss_dialog(page)
                page.wait_for_timeout(random.randint(1000, 2000))
                _log("关闭报错弹窗，重载页面后重试")
                user_input = _reload_login_page(page)
                continue
            raise

        submit = _first_visible(page, SUBMIT_SELECTORS, timeout=5000)
        if submit is None:
            raise LoginError("PAGE_ERROR", "找不到登录按钮")

        # 提交前装好 fetch 拦截器：登录是前端 JS 自己 fetch 的，
        # 只有这样才能在 JS 层拿到接口真实响应体（token 无效/密码错/2FA）。
        _install_signin_interceptor(page)

        resp = None
        try:
            with page.expect_response(
                lambda r: SIGNIN_API_MARK in r.url, timeout=30000
            ) as resp_info:
                submit.click(timeout=8000)
            resp = resp_info.value
        except Exception as e:
            last_msg = f"提交后未拿到接口响应（{type(e).__name__}）"
            _log(last_msg)
            if attempt < max_attempts:
                _log("重新加载登录页后重试")
                user_input = _reload_login_page(page)
            continue

        # 登录接口成功与否，不靠读响应体判断——登录成功后页面会立刻跳转，
        # 响应体随即被浏览器丢弃（Camoufox/Firefox 下读 body 会报
        # NS_ERROR_INVALID_CONTENT_ENCODING）。真正的判据是 cookie 里有没有 sessionid。
        page.wait_for_timeout(2500)
        all_cookies = page.context.cookies()
        cookie = cookies_to_string(all_cookies)
        _log(f"提交后 cookie 一览：{[c['name'] for c in all_cookies]}")
        if "sessionid=" in cookie:
            _log("登录成功（已拿到 sessionid）")
            return {"cookie": cookie, "cookies": all_cookies, "username": username}

        # 没有 sessionid：先读 fetch/XHR 拦截器捕获的接口原文，再用网络层响应兜底
        debug = _read_signin_debug(page)
        status = None
        data = None
        if debug and isinstance(debug, dict):
            status = debug.get("status")
            body_text = debug.get("respBody") or ""
            if body_text:
                try:
                    data = json.loads(body_text)
                except Exception:
                    data = body_text
            if debug.get("fetchErr") or debug.get("xhrErr"):
                _log(f"signIn 请求报错：{debug.get('fetchErr') or debug.get('xhrErr')}")

        # 兜底：拦截器没抓到（或抓到的是非 JSON）时，直接从 Playwright 响应对象读原始字节。
        # Camoufox/Firefox 下 resp.json()/resp.text() 会报 NS_ERROR_INVALID_CONTENT_ENCODING，
        # 但 resp.body() 拿的是原始字节，可以自己解码。
        if data is None and resp is not None:
            status = resp.status
            raw = None
            try:
                raw = resp.body()
            except Exception as e:
                _log(f"resp.body() 读取失败：{type(e).__name__}")
            if raw:
                for enc in ("utf-8", "gbk", "latin-1"):
                    try:
                        txt = raw.decode(enc)
                        data = json.loads(txt)
                        _log(f"响应体（{enc} 解码成功）：{txt[:500]}")
                        break
                    except Exception:
                        continue
                else:
                    # 都不是合法 JSON，当作纯文本保留前 300 字节
                    data = raw.decode("utf-8", errors="replace")[:300]
                    _log(f"响应体非 JSON：{data}")
            # 响应头也打出来，帮助判断 Content-Encoding / Set-Cookie 问题
            try:
                _log(f"响应头：content-type={resp.headers.get('content-type')} "
                     f"content-encoding={resp.headers.get('content-encoding')} "
                     f"set-cookie={resp.headers.get('set-cookie')}")
            except Exception:
                pass

        # 2FA
        if data and isinstance(data, dict) and data.get("need2FA"):
            otp_session = data.get("otpSession", "")
            if not totp_secret:
                raise LoginError(
                    "NEED_2FA",
                    "账号开启了两步验证，请配置 TOTP 密钥（NS_TOTP_SECRET / 请求体 totp_secret）",
                )
            code = _totp(totp_secret)
            _log("账号需要 2FA，已用 TOTP 密钥生成验证码")
            otp_input = _first_visible(page, ["#2fa", 'input[placeholder*="6"]'], timeout=6000)
            if otp_input is None:
                raise LoginError("NEED_2FA", "需要 2FA 验证码，但页面上找不到输入框")
            otp_input.fill(code)
            page.wait_for_timeout(random.randint(300, 700))
            btn = _first_visible(page, SUBMIT_SELECTORS, timeout=5000)
            try:
                if btn:
                    with page.expect_response(
                        lambda r: SIGNIN_API_MARK in r.url, timeout=30000
                    ):
                        btn.click(timeout=8000)
                    page.wait_for_timeout(2500)
            except Exception:
                pass
            cookie = cookies_to_string(page.context.cookies())
            if "sessionid=" in cookie:
                _log("2FA 登录成功")
                return {"cookie": cookie, "cookies": page.context.cookies(), "username": username}
            raise LoginError("NEED_2FA", "2FA 验证未通过，请检查 TOTP 密钥是否正确")

        if isinstance(data, dict):
            last_msg = data.get("message") or data.get("msg") or data.get("error") or json.dumps(data, ensure_ascii=False)[:200]
        elif data:
            last_msg = str(data)[:200]
        else:
            last_msg = f"HTTP {status}"

        # 读不到响应体时，从页面 DOM 里抓错误提示（最可靠的兜底）
        if not last_msg or last_msg == f"HTTP {status}":
            page_err = _read_login_error(page)
            if page_err:
                last_msg = f"页面提示：{page_err}"

        if not last_msg:
            last_msg = f"HTTP {status}（响应体已被浏览器丢弃，但也没拿到 sessionid）"
        _log(f"登录失败：{last_msg}")
        _log(f"  ↳ 接口原文：{data if isinstance(data, (str, dict)) else str(data)[:300]}")
        _log(f"  ↳ 当前 URL：{page.url if hasattr(page, 'url') else '?'}")

        low = last_msg.lower()
        cred_hints = ("密码错误", "账号或密码", "用户名或密码", "密码不正确",
                      "password incorrect", "invalid credentials", "user not found", "用户不存在")
        if any(k in low for k in cred_hints):
            raise LoginError("BAD_CREDENTIALS", last_msg)
        if any(k in last_msg for k in ["验证", "人机", "captcha", "稍后", "频繁", "重试"]):
            page.wait_for_timeout(random.randint(3000, 5000))
            if attempt < max_attempts:
                _log("验证类失败，重新加载登录页重试")
                user_input = _reload_login_page(page)
            continue
        raise LoginError("LOGIN_FAILED", last_msg)

    _debug_dump(page, "login_failed")
    raise LoginError("LOGIN_FAILED", last_msg or "多次尝试仍未登录成功")


def _debug_dump(page, tag: str):
    """失败时留一份截图 + HTML，方便判断是改版还是被风控。"""
    try:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(base, exist_ok=True)
        page.screenshot(path=os.path.join(base, f"debug_{tag}.png"), full_page=True)
        with open(os.path.join(base, f"debug_{tag}.html"), "w", encoding="utf-8") as f:
            f.write(page.content() or "")
        _log(f"已保存排查文件：data/debug_{tag}.png / .html")
    except Exception:
        pass


def _totp(secret: str) -> str:
    try:
        import pyotp
    except ImportError:
        raise LoginError("NEED_2FA", "需要 2FA，但未安装 pyotp（pip install pyotp）")
    return pyotp.TOTP(secret.strip().replace(" ", "")).now()


# --------------------------------------------------------------------------
# 在浏览器里直接签到（青龙 IP 被 CF 拦时用）
# --------------------------------------------------------------------------
def sign_in_page(page, cookie: Optional[str] = None) -> dict:
    """带着 cookie 打开 /board 并调用签到接口，返回接口 JSON。"""
    if cookie:
        cookies = []
        for part in cookie.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            cookies.append({
                "name": k.strip(),
                "value": v.strip(),
                "domain": ".nodeseek.com",
                "path": "/",
            })
        try:
            page.context.add_cookies(cookies)
        except Exception as e:
            _log(f"写入 cookie 失败：{e}")

    page.goto(BOARD_URL, wait_until="domcontentloaded", timeout=60000)
    _wait_out_challenge(page)
    page.wait_for_timeout(random.randint(1500, 2500))

    res = page.evaluate(
        """async () => {
            const r = await fetch('/api/attendance?random=true', {
                method: 'POST',
                headers: {'content-type': 'application/json', 'x-csrf-challenge': 'simple-token'},
                credentials: 'include'
            });
            const text = await r.text();
            let data = null;
            try { data = JSON.parse(text); } catch (e) {}
            return {status: r.status, text: text.slice(0, 500), data};
        }"""
    )
    return res


def open_and_login(engine="auto", headless=True, profile_dir=None, proxy=None,
                   username="", password="", totp_secret=None) -> dict:
    """一站式：开浏览器 → 登录 → 关浏览器。给 agent 和命令行共用。"""
    from browser_engine import browser_page

    with browser_page(
        engine=engine, headless=headless, user_data_dir=profile_dir, proxy=proxy
    ) as page:
        result = login(page, username, password, totp_secret=totp_secret)
        # 顺手记一下指纹，方便排查“同一 profile 被风控”
        try:
            fp = page.evaluate("() => navigator.userAgent")
            result["user_agent"] = fp
        except Exception:
            pass
        return result
