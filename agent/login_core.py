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


def _wait_out_challenge(page, timeout=45):
    """如果遇到整页 CF 挑战，点复选框并等它过（家宽 IP 通常几秒就过）。"""
    if not _page_is_challenge(page):
        return True
    _log("检测到 Cloudflare 整页挑战，等待自动通过…")
    deadline = time.time() + timeout
    last_click = 0
    stable = 0
    while time.time() < deadline:
        page.wait_for_timeout(1500)
        if _challenge_cleared(page):
            stable += 1
            if stable >= 2:
                _log("整页挑战已通过")
                return True
        else:
            stable = 0
            if not _page_is_challenge(page):
                # 既不是挑战页、也没加载出正式页面，多半在跳转中
                page.wait_for_timeout(1000)
        if time.time() - last_click > 6:
            _click_turnstile_checkbox(page)
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

    实测：直接 frame.locator('input[type=checkbox]').click() 点到的是隐藏元素，
    会触发组件的 onError（页面弹“人机验证服务加载失败”）。
    所以优先用真实鼠标点在组件左侧复选框的像素位置上。
    """
    box = _widget_box(page)
    if box:
        try:
            x = box["x"] + 26
            y = box["y"] + box["height"] / 2
            page.mouse.move(box["x"] + 150, y - 8)
            page.wait_for_timeout(random.randint(200, 500))
            page.mouse.move(x, y, steps=10)
            page.wait_for_timeout(random.randint(120, 300))
            page.mouse.click(x, y)
            _log("已点击 Turnstile 复选框（坐标）")
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


def _wait_turnstile(page, timeout=30):
    """等 Turnstile 出 token。登录页的组件是交互式的，必须先点复选框。

    每 4 秒重点一次，直到出 token 或超时。
    """
    deadline = time.time() + timeout
    last_click = 0
    while time.time() < deadline:
        tok = _turnstile_token(page)
        if tok:
            _log(f"Turnstile token 已获取（{len(tok)} 字符）")
            return tok
        if "人机验证服务加载失败" in _body_text(page):
            raise LoginError(
                "TURNSTILE_NETWORK",
                "Turnstile 组件加载失败：这台机器访问不到 challenges.cloudflare.com，"
                "请检查出口网络/DNS（家宽直连一般没问题，机房或代理可能被墙）",
            )
        if _turnstile_widget_ok(page):
            # 组件显示“验证成功”即说明 token 已交给页面（很多实现不写 hidden input）
            _log("Turnstile 组件显示已验证，直接提交")
            return "widget-ok"
        if time.time() - last_click > 4:
            _click_turnstile_checkbox(page)
            last_click = time.time()
        page.wait_for_timeout(800)
    _log(f"Turnstile {timeout} 秒内未出 token，尝试直接提交")
    return None


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


def _body_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""


def _turnstile_widget_ok(page) -> bool:
    """Turnstile 组件里是否已经显示“验证成功”。"""
    for fr in page.frames:
        if "challenges.cloudflare.com" not in (fr.url or ""):
            continue
        try:
            text = (fr.locator("body").inner_text() or "").strip().lower()
        except Exception:
            continue
        if any(k in text for k in ("成功", "success", "verified", "验证通过")):
            return True
    return False


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

        _wait_turnstile(page, timeout=30)

        submit = _first_visible(page, SUBMIT_SELECTORS, timeout=5000)
        if submit is None:
            raise LoginError("PAGE_ERROR", "找不到登录按钮")

        try:
            with page.expect_response(
                lambda r: SIGNIN_API_MARK in r.url, timeout=30000
            ) as resp_info:
                submit.click(timeout=8000)
            resp = resp_info.value
        except Exception as e:
            last_msg = f"提交后未拿到接口响应（{type(e).__name__}）"
            _log(last_msg)
            # 可能是 Turnstile 还在算，或页面弹了验证框
            _click_turnstile_checkbox(page)
            page.wait_for_timeout(3000)
            continue

        try:
            data = resp.json()
        except Exception:
            body = (resp.text() or "")[:300]
            last_msg = f"登录接口返回非 JSON（HTTP {resp.status}）：{body}"
            _log(last_msg)
            continue

        if data.get("success"):
            cookie = cookies_to_string(page.context.cookies())
            if "sessionid=" not in cookie:
                raise LoginError("NO_COOKIE", f"接口返回成功但没拿到 sessionid：{cookie[:200]}")
            _log("登录成功")
            return {"cookie": cookie, "cookies": page.context.cookies(), "username": username}

        # 2FA
        if data.get("need2FA"):
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

        last_msg = data.get("message") or str(data)[:200]
        _log(f"登录失败：{last_msg}")

        low = last_msg.lower()
        cred_hints = ("密码错误", "账号或密码", "用户名或密码", "密码不正确",
                      "password incorrect", "invalid credentials", "user not found", "用户不存在")
        if any(k in low for k in cred_hints):
            raise LoginError("BAD_CREDENTIALS", last_msg)
        if any(k in last_msg for k in ["验证", "人机", "captcha", "稍后", "频繁", "重试"]):
            page.wait_for_timeout(random.randint(3000, 5000))
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
