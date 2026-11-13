# -*- coding: utf-8 -*-
"""DeepFlood 自动登录。

DeepFlood(DF) 是 NodeSeek(NS) 的附属站，**同一套程序**（同样的 Vue SSR、
同样的 msc-script、同一个 fog 指纹服务），所以登录接口和 NS 一致。

DF 有三条登录路：
  1. NodeSeek 一键登录（NodeSeek Connect / OAuth）  ← 首选
  2. 用户名 + 密码 + Turnstile
  3. 邮箱动态验证（需要收邮件，cron 里不可行）

--------------------------------------------------------------------------
为什么「一键登录」必须复用 NS 的浏览器 profile
--------------------------------------------------------------------------
这不是实现偏好，是协议本身决定的。从两个站点自己的前端 bundle 里读出来的：

    DF  nsSignIn.html  点 button.bottone5
      → window.open("https://www.nodeseek.com/connect?target=DeepFlood")

    NS  connect 页     点 <a id="back" class="btn">Connect</a>
      → GET /api/cAuth?target=DeepFlood      ← **用 NS 自己的登录态换身份**
      → window.opener.postMessage({source:"ns-connect", data, wtf, sign}, ...)
      → window.close()

    DF  收到 message   → POST /api/account/nodeseek-signIn  {data, wtf, sign}
      → Set-Cookie（登录完成）

第 3 步的 /api/cAuth 只认 NS 的 cookie：未登录返回
{"message":"USER NOT FOUND","status":404,"success":false}。
所以浏览器里没有 NS 登录态时，弹窗只会显示 NS 的登录页 ——
这正解释了「换一个浏览器/换一个 profile 就不能一键登录」。

结论：调用方传进来的 user_data_dir 必须是 **该 NS 账号的 profile**
（即 cookie/profiles/<NS用户名>），不能另起一个干净 profile。

--------------------------------------------------------------------------
多账号
--------------------------------------------------------------------------
每个 NS 账号有自己的 profile 目录，里面存着各自的 NS 登录态。
DF 第 N 个账号用第 N 个 NS 账号的 profile，就各自授权到各自绑定的 DF 账号。
"""

from __future__ import annotations

import os
import time
from typing import Optional

import login_core
from login_core import (
    LoginError,
    _dismiss_dialog,
    _first_visible,
    _wait_out_challenge,
    cookie_expiry_info,
    cookies_to_string,
    describe_cookie_life,
)

DF_ORIGIN = "https://www.deepflood.com"
DF_NS_SIGNIN_URL = DF_ORIGIN + "/nsSignIn.html"
DF_SIGNIN_URL = DF_ORIGIN + "/signIn.html"
NS_CONNECT_URL = "https://www.nodeseek.com/connect?target=DeepFlood"
DF_DOMAIN = "deepflood.com"

# 登录态探测：和 NS 同一个接口。未登录返回 {"success":false,...}
DF_AUTH_PROBE = "/api/account/credit/page-1"

# DF 的「NodeSeek一键登录」按钮（nsSignIn 组件里是 class="bottone5"）
NS_LOGIN_BTN_SELECTORS = [
    "button.bottone5",
    "button:has-text('NodeSeek一键登录')",
    "button:has-text('一键登录')",
]

# NS connect 弹窗里的「Connect」链接
NS_CONNECT_BTN_SELECTORS = [
    "a#back",
    "#back",
    "a.btn:has-text('Connect')",
    "a:has-text('Connect')",
]

# DF 密码登录表单（signIn 组件：input#stacked-email / input#stacked-password）
DF_USER_SELECTORS = [
    "#stacked-email",
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[type="text"]',
]
DF_PWD_SELECTORS = [
    "#stacked-password",
    'input[type="password"]',
]
DF_SUBMIT_SELECTORS = [
    "form button[type='submit']",
    "button:has-text('登录')",
    "button:has-text('登 录')",
]


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[deepflood] {msg}")


def session_ok(page) -> bool:
    """当前浏览器上下文里 DF 是不是已登录。

    用 credit 接口而不是看 cookie 名：登录成功后 DF 的 cookie 名
    可能随版本变化，而接口是否返回 success 是权威行为。
    """
    try:
        return bool(page.evaluate(
            """async (url) => {
                try {
                    const r = await fetch(url, {credentials: 'include'});
                    const j = await r.json();
                    return !!(j && j.success);
                } catch (e) { return false; }
            }""",
            DF_AUTH_PROBE,
        ))
    except Exception:
        return False


def _wait_session(page, timeout: int = 60) -> bool:
    """轮询等登录态建立。DF 收到 postMessage 后要异步 POST 一次才种 cookie。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if session_ok(page):
            return True
        page.wait_for_timeout(800)
    return False


def _safe_goto(page, url: str, timeout: int = 60000) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout)


def inject_ns_cookie(page, cookie_str: str, domain: str = ".nodeseek.com") -> int:
    """把已保存的 NS cookie 注入当前浏览器上下文，返回注入条数。

    为什么必须做这一步（而不是指望 profile 里还留着登录态）：
      1. NS 的 profile 目录名由 _safe(用户名) 生成，而 _safe 只保留
         [0-9A-Za-z._-] —— **纯中文账号会被压成 "account"**，
         多个中文账号直接撞同一个目录；
      2. 登录 cookie 约 30 天到期，profile 里那份很可能早就没了；
      3. 用 profile 换账号还要重新走一遍 NS 登录（可能要邮箱验证码）。

    直接把签到用的那份 cookie 塞进浏览器最省事，而且用的是同一个账号的
    cookie —— 一键登录授权的还是这个账号绑定的 DF 号，不会串号。
    """
    if not cookie_str:
        return 0
    items = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or not value:
            continue
        items.append({"name": name, "value": value, "domain": domain, "path": "/"})
    if not items:
        return 0
    try:
        page.context.add_cookies(items)
        _log(f"已注入 {len(items)} 条 NodeSeek cookie 到浏览器")
        return len(items)
    except Exception as e:
        _log(f"注入 NS cookie 失败：{type(e).__name__}: {e}")
        return 0


def ns_session_ok(page, timeout: int = 30000) -> bool:
    """浏览器里（含刚注入的 cookie）NS 是否处于登录态。

    先导航到 NS 域名再用同源相对路径 fetch —— 在 deepflood 页面上跨域
    请求 nodeseek 会被 CORS 拦掉，判断不出真实状态。
    """
    try:
        page.goto("https://www.nodeseek.com/", wait_until="domcontentloaded", timeout=timeout)
    except Exception:
        return False
    try:
        return bool(page.evaluate(
            """async () => {
                try {
                    const r = await fetch('/api/account/credit/page-1',
                                          {credentials: 'include'});
                    const j = await r.json();
                    return !!(j && j.success);
                } catch (e) { return false; }
            }"""
        ))
    except Exception:
        return False


def _pass_challenge(page, where: str = "") -> bool:
    """过 Cloudflare 整页挑战。profile 里有 cf_clearance 时通常直接过。"""
    try:
        return _wait_out_challenge(page)
    except Exception as e:
        _log(f"{where} 过 Cloudflare 挑战异常：{type(e).__name__}: {e}")
        return False


def _looks_like_ns_login(popup) -> bool:
    """弹窗是不是落在了 NS 的登录页（= profile 里 NS 登录态没了）。"""
    try:
        url = (popup.url or "").lower()
    except Exception:
        url = ""
    if "signin" in url or "emailsignin" in url or "login" in url:
        return True
    try:
        if popup.locator('input[type="password"]').count() > 0:
            return True
    except Exception:
        pass
    return False


def _result_ok(page, username: str = "") -> dict:
    """从浏览器上下文取 cookie，组装成和 login_core 一致的成功结构。"""
    try:
        cookies = page.context.cookies()
    except Exception:
        cookies = []
    cookie = cookies_to_string(cookies, domain_hint=DF_DOMAIN)
    if not cookie:
        return {"error": "NO_COOKIE", "message": f"登录后没拿到 {DF_DOMAIN} 的 cookie"}
    return {
        "cookie": cookie,
        "cookies": cookies,
        "username": username,
        "expiry": cookie_expiry_info(cookies),
        "cookie_life": describe_cookie_life(cookies),
    }


# --------------------------------------------------------------------------
# 方式 1：NodeSeek 一键登录
# --------------------------------------------------------------------------
def login_via_ns_connect(page, timeout: int = 180) -> dict:
    """走 DF 的 nsSignIn 页 → NS Connect 弹窗 → postMessage 回传。

    前提：这个浏览器 profile 里有**该账号的 NS 登录态**。
    没登录态时返回 {"error": "NS_NOT_LOGGED_IN"}，让上层去先跑 NS 登录。
    """
    _log(f"打开 {DF_NS_SIGNIN_URL}")
    try:
        _safe_goto(page, DF_NS_SIGNIN_URL)
    except Exception as e:
        return {"error": "GOTO_FAILED", "message": f"打不开 DF 登录页：{e}"}

    if not _pass_challenge(page, "DF 登录页"):
        return {"error": "WAF_BLOCKED", "message": "DF 登录页的 Cloudflare 挑战没通过"}

    btn = _first_visible(page, NS_LOGIN_BTN_SELECTORS, timeout=30000)
    if btn is None:
        return {
            "error": "NO_LOGIN_BUTTON",
            "message": "nsSignIn 页找不到「NodeSeek一键登录」按钮（站点可能改版）",
        }

    _log("点击「NodeSeek一键登录」，等待 NS 授权弹窗…")
    popup = None
    try:
        with page.expect_popup(timeout=30000) as pinfo:
            btn.click()
        popup = pinfo.value
    except Exception as e:
        return {
            "error": "NO_POPUP",
            "message": f"点按钮后没弹出 NS 授权窗口：{type(e).__name__}: {e}",
        }

    try:
        popup.wait_for_load_state("domcontentloaded", timeout=45000)
    except Exception:
        pass

    # 弹窗落在 NS 登录页 = profile 里 NS 登录态失效
    if _looks_like_ns_login(popup):
        try:
            popup.close()
        except Exception:
            pass
        return {
            "error": "NS_NOT_LOGGED_IN",
            "message": (
                "这个 profile 里 NodeSeek 没登录（或已过期），"
                "一键登录无法授权。先让同一账号跑一次 NS 登录即可。"
            ),
        }

    if not _pass_challenge(popup, "NS 授权页"):
        return {"error": "WAF_BLOCKED", "message": "NS 授权页的 Cloudflare 挑战没通过"}

    back = _first_visible(popup, NS_CONNECT_BTN_SELECTORS, timeout=45000)
    if back is None:
        return {
            "error": "NO_CONNECT_BUTTON",
            "message": "NS 授权页找不到 Connect 按钮（站点可能改版）",
        }

    _log("点 NS 授权页的 Connect…")
    try:
        back.click()
    except Exception as e:
        return {"error": "CONNECT_CLICK_FAILED", "message": f"点 Connect 失败：{e}"}

    # 正常流程：NS 调 cAuth → postMessage → 自己关窗
    try:
        popup.wait_for_event("close", timeout=30000)
        _log("NS 授权弹窗已关闭")
    except Exception:
        # 没关窗不代表失败（postMessage 可能已经发出），继续看 DF 的会话
        _log("授权弹窗未自动关闭，继续检查 DF 登录态")
        try:
            popup.close()
        except Exception:
            pass

    _log("等待 DF 建立登录态…")
    if _wait_session(page, timeout=60):
        return _result_ok(page)
    return {
        "error": "SESSION_NOT_ESTABLISHED",
        "message": "授权走完了，但 DF 没有建立登录态（可能该 NS 账号没绑定 DF）",
    }


# --------------------------------------------------------------------------
# 方式 2：用户名密码
# --------------------------------------------------------------------------
def login_via_password(page, user: str, pwd: str, max_attempts: int = 2) -> dict:
    """DF 的密码登录。

    和 NS 的区别：DF 这个页面是**纯密码表单**（input#stacked-email +
    input#stacked-password + Turnstile），没有 NS 那套邮箱验证码切换。
    所以不需要走 login_core 的完整流程，只借用它的 Turnstile 处理。
    """
    if not user or not pwd:
        return {"error": "MISSING_CREDENTIALS", "message": "没配 DF_USER / DF_PASS"}

    _log(f"打开 {DF_SIGNIN_URL}")
    try:
        _safe_goto(page, DF_SIGNIN_URL)
    except Exception as e:
        return {"error": "GOTO_FAILED", "message": f"打不开 DF 登录页：{e}"}

    if not _pass_challenge(page, "DF 登录页"):
        return {"error": "WAF_BLOCKED", "message": "DF 登录页的 Cloudflare 挑战没通过"}

    page.wait_for_timeout(2500)

    last_msg = ""
    for attempt in range(1, max_attempts + 1):
        _log(f"密码登录 第 {attempt}/{max_attempts} 次：{user}")
        try:
            _dismiss_dialog(page)
        except Exception:
            pass

        u = _first_visible(page, DF_USER_SELECTORS, timeout=15000)
        p = _first_visible(page, DF_PWD_SELECTORS, timeout=8000)
        if u is None or p is None:
            return {
                "error": "PAGE_ERROR",
                "message": "DF 登录页找不到用户名/密码输入框（站点可能改版）",
            }

        try:
            u.click(timeout=3000)
            u.fill("")
            u.type(user, delay=60)
            p.click(timeout=3000)
            p.fill("")
            p.type(pwd, delay=60)
        except Exception as e:
            return {"error": "FILL_FAILED", "message": f"填表失败：{e}"}

        page.wait_for_timeout(800)

        # Turnstile：复用 NS 那套（同一个 sitekey、同一套 shadow DOM 处理）
        try:
            login_core._wait_turnstile(page, timeout=45)
        except LoginError as e:
            last_msg = f"人机验证未通过 [{e.code}] {getattr(e, 'message', e)}"
            _log(last_msg)
            if attempt < max_attempts:
                page.wait_for_timeout(2000)
                try:
                    _safe_goto(page, DF_SIGNIN_URL)
                    page.wait_for_timeout(2500)
                except Exception:
                    pass
                continue
            return {"error": "TURNSTILE_FAILED", "message": last_msg}
        except Exception as e:
            last_msg = f"人机验证异常：{type(e).__name__}: {e}"
            _log(last_msg)
            return {"error": "TURNSTILE_FAILED", "message": last_msg}

        submit = _first_visible(page, DF_SUBMIT_SELECTORS, timeout=8000)
        if submit is None:
            return {"error": "PAGE_ERROR", "message": "找不到登录按钮"}

        submit.click()
        _log("已提交登录表单，等待结果…")

        if _wait_session(page, timeout=45):
            return _result_ok(page, user)

        # 没登录成功：把页面上的提示抓出来（mscAlert 的 .msc-confirm）
        try:
            alert = login_core._ns_alert_text(page)
        except Exception:
            alert = ""
        if alert:
            last_msg = alert
            _log(f"登录失败提示：{alert}")
        if "验证码" in (alert or "") or "2FA" in (alert or ""):
            return {
                "error": "NEEDS_2FA_OR_EMAIL",
                "message": f"DF 要求二次验证：{alert}（cron 里做不了，请手动登录一次）",
            }
        page.wait_for_timeout(1500)

    return {"error": "LOGIN_FAILED", "message": last_msg or "登录未成功"}


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------
def login(
    page,
    user: str = "",
    pwd: str = "",
    *,
    try_ns_connect: bool = True,
    allow_password: bool = True,
    ns_cookie: str = "",
    timeout: int = 180,
) -> dict:
    """在已打开的 page 上登录 DF，返回 {cookie, cookie_life, ...} 或 {error, message}。

    参数 ns_cookie：该 NS 账号已保存的 cookie 字符串。传进来会被注入浏览器，
    一键登录就用这份身份去授权 —— 不依赖 profile 里是否还留着 NS 登录态。

    顺序：
      0. 这个 profile 里如果已经登录过 DF（cookie 还在），直接用
      1. NodeSeek 一键登录（注入 ns_cookie 后走 NS Connect）
      2. 用户名密码（需要 DF_USER / DF_PASS）
    """
    # 0. 已有登录态
    try:
        _safe_goto(page, DF_ORIGIN + "/")
    except Exception:
        pass
    if session_ok(page):
        _log("这个 profile 里 DF 已是登录状态，直接复用")
        return _result_ok(page, user)

    errors = []

    # 1. 一键登录
    if try_ns_connect:
        ns_ready = True
        if ns_cookie:
            # 一键登录的前提是浏览器里有 NS 登录态。先把签到用的那份 NS cookie
            # 注入进去 —— 比指望 profile 里还留着登录态可靠得多。
            inject_ns_cookie(page, ns_cookie)
            ns_ready = ns_session_ok(page)
            if not ns_ready:
                _log("注入的 NS cookie 已失效 —— 浏览器里 NS 仍未登录，一键登录无法授权")
        else:
            _log("没传入 NS cookie，只能依赖 profile 里现存的登录态")

        if ns_ready:
            r = login_via_ns_connect(page, timeout=timeout)
            if r.get("cookie"):
                _log("一键登录成功")
                return r
            errors.append(f"ns_connect: {r.get('error')} {r.get('message', '')}")
            _log(f"一键登录未成功：{r.get('error')} {r.get('message', '')}")
        else:
            errors.append(
                "ns_connect: NS_NOT_LOGGED_IN 这个账号的 NodeSeek cookie 已过期，"
                "一键登录需要 NS 处于登录态"
            )

    # 2. 密码
    if allow_password:
        r = login_via_password(page, user, pwd)
        if r.get("cookie"):
            _log("密码登录成功")
            return r
        errors.append(f"password: {r.get('error')} {r.get('message', '')}")
        _log(f"密码登录未成功：{r.get('error')} {r.get('message', '')}")

    return {"error": "LOGIN_FAILED", "message": " | ".join(errors) or "所有登录方式都失败"}
