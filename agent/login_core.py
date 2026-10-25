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
import re
import time
from typing import Optional
from urllib.parse import unquote

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

# NodeSeek 真实的登录态 cookie 名（2026-10 实测登录成功后浏览器里拿到的是
# pjwt / session / smac，而不是早期文档里写的 sessionid）。
#
# 这一条踩过坑：邮箱验证码流程其实**已经登录成功**（cookie 里有 session+pjwt，
# 页面也跳回了首页），但代码只认 "sessionid="，于是把成功误判成失败，
# 白白重跑一遍邮箱验证。
AUTH_COOKIE_NAMES = ("pjwt", "session", "smac")

# 站内「必须登录才能访问」的接口，用它来确认登录态（比看 cookie 名可靠）
AUTH_PROBE_API = "/api/account/credit/page-1"


class LoginError(Exception):
    """带错误码的登录异常，方便上游区分是“要重试”还是“要人工介入”。"""

    def __init__(self, code: str, message: str, device_memory_expired: bool = False):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        # True = 这台设备之前登录过，这次仍被要求邮箱验证
        #       → NodeSeek 的「记住设备」可能已失效，需要告警而不是静默重试
        self.device_memory_expired = device_memory_expired


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# 调试开关：NS_DEBUG=1 时才打印「排查用」的详细信息。
# 默认关闭——日常签到日志要一眼看得出「成功没成功」，不该被指纹/表单 dump 淹没。
# 需要排查站点改版时：NS_DEBUG=1 python3 multi_site_sign_cookie_only.py
DEBUG = os.getenv("NS_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _dbg(msg: str):
    """仅调试模式打印。"""
    if DEBUG:
        print(f"[{time.strftime('%H:%M:%S')}] [debug] {msg}", flush=True)


def _cookie_names(cookies) -> str:
    """把 cookie 列表压成一行短文本，只留关键字段。"""
    names = [c.get("name") for c in cookies or []]
    keys = [n for n in names if n in AUTH_COOKIE_NAMES or n == "cf_clearance"]
    others = [n for n in names if n not in keys]
    tail = f" +{len(others)}个" if others else ""
    return (",".join(keys) + tail) if keys else ",".join(names)


def _probe_webgl(page):
    """把 WebGL 渲染器打进日志 —— 一锤定音的指纹证据。

    Xvfb 下没装 Mesa 时是 "Google SwiftShader"（CF 已知 bot 特征，
    会被 Turnstile onError 直接拒绝）；
    装了 libgl1-mesa-dri 后是 "llvmpipe"，是正常桌面会有的值。
    连 WebGL 都没有（NO_WEBGL）说明 GPU 进程没起来，往往缺 libegl1/libgles2。

    注意：正常值只在调试模式打印；只有「异常值」才值得占用日志行。
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
            _log(f"⚠️ WebGL 渲染器：{r}  ← CF 已知 bot 特征，执行：apt install -y libgl1 libgl1-mesa-dri")
        elif r in ("NO_WEBGL", "NO_EXT", "ERR", "") or r.startswith("ERR_"):
            _log(f"⚠️ WebGL 渲染器：{r}  ← 浏览器完全没有 WebGL！GPU 进程没起来。"
                 f"先试：apt install -y libegl1 libgles2；仍无效就换 AGENT_ENGINE=camoufox")
        else:
            _dbg(f"WebGL 渲染器：{r}")
    except Exception as e:
        _dbg(f"WebGL 探测异常：{type(e).__name__}")


def _dump_gpu_info(page):
    """更深的 GPU 诊断（仅调试模式）：Chromium 内部看到的 GL 实现与 GPU 特性。"""
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
        _dbg(f"GPU 详情：{info}")
    except Exception as e:
        _dbg(f"GPU 详情获取失败：{type(e).__name__}")


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
    """拿到 Turnstile 组件 iframe 的位置，用于真实鼠标点击。

    **关键坑（2026-10-25 真机实测定位）**：
    Turnstile 的 iframe 被包在 **shadow DOM** 里，
    `page.query_selector_all('iframe[src*="challenges.cloudflare.com"]')`
    在 light DOM 里**一个都查不到**（实测命中 0 个），
    所以老实现永远返回 None → 「没定位到 Turnstile 组件」→
    **复选框从来没被点过**。

    正确做法：走 `page.frames` 找到 challenges.cloudflare.com 的 frame，
    再用 `frame.frame_element()` 拿它——这是 Playwright 层面枚举的，
    能穿 shadow root，实测拿到 bounding_box {width:300, height:65}，位置可用。

    退路：万一 frame_element() 失败，再试 light DOM 查询。
    """
    # 1) 首选：page.frames + frame_element()（能穿 shadow DOM）
    try:
        for fr in page.frames:
            if "challenges.cloudflare.com" not in (fr.url or ""):
                continue
            try:
                el = fr.frame_element()
                box = el.bounding_box()
            except Exception:
                continue
            if box and box.get("width", 0) > 100:
                return box
    except Exception:
        pass

    # 2) 退路：light DOM 查询（有些版本 iframe 不在 shadow 里）
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

    **2026-10-25 新增（真实根因）**：
    Turnstile iframe 被包在 **shadow DOM** 里，
    `query_selector_all('iframe[src*=...])` 在 light DOM 查不到（实测命中 0 个），
    所以老代码的 `_widget_box` 永远返回 None → **复选框从来没被点过**。
    现在 `_widget_box` 改走 `page.frames` + `frame_element()`（能穿 shadow）。

    iframe 内部是 274KB 混淆脚本，**没有**可被 Playwright 定位的
    input/label（实测 hasCheckbox=false、hasLabel=false），
    所以只能靠坐标点：复选框在 iframe 左侧约 30px、垂直居中。
    """
    box = _widget_box(page)
    if not box:
        return False

    def _click_at(x, y):
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

    try:
        y = box["y"] + box["height"] / 2
        # 30 是实测值：Turnstile 复选框圆心约在 iframe 左边缘 +30px
        _click_at(box["x"] + 30, y)
        _log("已点击 Turnstile 复选框（按住时长已模拟）")

        # 微调兜底：点完若组件毫无反应（状态还是 pending 且没出 token），
        # 说明没点准。Turnstile 复选框的实际圆心在 28~34px 之间浮动，
        # 再补一枪横向微调的位置。
        page.wait_for_timeout(1200)
        try:
            still_pending = (turnstile_state(page) == "pending"
                             and not _acquire_ts_token(page).get("token"))
        except Exception:
            still_pending = False
        if still_pending:
            page.wait_for_timeout(random.randint(400, 900))
            _click_at(box["x"] + 32, y)
            _log("Turnstile 组件无响应，已横向微调补点一次")
        return True
    except Exception as e:
        _log(f"坐标点击失败：{type(e).__name__}")
        return False


def _wait_turnstile(page, timeout=40, widget_only_ok=False):
    """等 Turnstile 完成验证。

    实测教训（2026-10，AT&T 住宅 IP，Xvfb 有头 patchright）：
      1) 点完复选框后组件转圈 3~15 秒，期间**再点会取消并重新开始**，
         所以绝对不能每 4 秒点一次（旧版就是因此永远过不去）；
      2) 但也**不能不点**——托管模式并不总是自动放行，该点还得点。
         上一次改过头，状态误判成 verifying 后一次都没点，白等 40 秒。

    所以策略是：先给 8 秒自动通过的机会 → 没动静就点一次 → 之后只等不点
    （只有明确 error 才允许再点一次）。

    **什么才算「通过了」（2026-10 真机实测后改）**：
      唯一权威信号是**真的拿到 token**（组件 expose 的 getResponse()，
      或者 hidden input 里的 cf-turnstile-response）。
      组件显示「验证成功」**不算数**——那是读 iframe 里的文字，
      而页面上**别的** Cloudflare frame（整页挑战页残留、其它组件）
      同样会带「成功/success」字样。曾因此把「根本没通过」误判成通过，
      拿着空 token 去发码，被站点以「请先通过人机验证」挡回来，
      而日志里只留下一句「20 秒内没看到任何接口响应」。
      所以：**state 只用来决定「要不要点」，token 才是通行证**。

    返回 token / "widget-ok"；拿不到明确信号返回 None（交给提交结果判断）。

    widget_only_ok=True 时（邮箱验证页用）：
      组件明确显示通过、且持续 10 秒以上、但就是取不到 token（极少见），
      按「已验证但无 token」处理返回 "widget-ok"，让调用方走
      「点按钮 + 读站点真实提示」的兜底，而不是干等满超时。
    """
    start = time.time()
    deadline = start + timeout
    clicked = 0
    last_click = 0.0
    last_state = None
    success_at = None

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

        # 权威信号：真的拿到 token（getResponse 优先，hidden input 兜底）
        tok = _acquire_ts_token(page)
        if tok.get("token"):
            _log(f"Turnstile 验证通过（{len(tok['token'])} 字符，"
                 f"来源：{tok.get('via')}）")
            return tok["token"]

        if state != last_state:
            _dbg(f"Turnstile 组件状态：{state}")
            last_state = state
        if state == "success":
            # 组件说通过了但拿不到 token —— 不可信（多半是别的 frame 带的字样），
            # 继续等；持续 10 秒还没有才按 callback 模式处理。
            if success_at is None:
                success_at = time.time()
            elif time.time() - success_at > 10:
                if widget_only_ok:
                    _log("Turnstile 组件显示通过但取不到 token，按「已验证无 token」处理")
                    return "widget-ok"
                _dbg("组件显示通过但取不到 token，继续等")
        else:
            success_at = None

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
        # 8 秒后还没过就点。不看 state 是否 pending：
        #   pending   → 还没开始，需要点
        #   verifying → 可能真是转圈，也可能是状态误判；点一下最多重来一次，
        #               总比一直干等强
        #
        # **2026-10-25 修正**：老逻辑 clicked==0 时只点一次，点歪了就再也不点。
        # 但 Turnstile iframe 内部是混淆脚本、**无法用选择器定位**，
        # 只能靠坐标点，命中不可能 100%。所以只要状态还是 pending
        # （=没点中），就允许隔一段时间再点一次，最多 3 次。
        if elapsed > 8 and time.time() - last_click > 6 and clicked < 3:
            if state != "verifying":
                ok = _click_turnstile_checkbox(page)
                last_click = time.time()
                if ok:
                    clicked += 1
                    page.wait_for_timeout(600)
                    _dbg(f"点击后状态：{turnstile_state(page)}")
                else:
                    _dbg("没定位到 Turnstile 组件，6 秒后重试点击")
        elif state == "error" and clicked < 3 and time.time() - last_click > 10:
            _dbg("Turnstile 报错，重新点一次")
            _click_turnstile_checkbox(page)
            clicked += 1
            last_click = time.time()

        page.wait_for_timeout(800)

    # 超时了。最后再取一次 token（可能刚好在这时验证完）
    tok = _acquire_ts_token(page)
    if tok.get("token"):
        _log(f"Turnstile {timeout} 秒内状态未确认，但拿到 token"
             f"（{len(tok['token'])} 字符），提交试试")
        return tok["token"]
    if success_at is not None:
        _log("Turnstile 组件显示通过却取不到 token（多半是状态被别的 frame 误导），"
             "按未通过处理")
        return None
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
    """关掉站点 mscAlert 之类的模态弹窗，否则它会盖住输入框导致点击超时。

    实测（2026-10）：emailSignIn 页面发码后会弹一个带 '×' / 'OK' 按钮的模态框，
    它盖在验证码输入框上，locator.click() 直接 TimeoutError。

    站点用的是 msc.js：容器 class 是 **.msc-confirm**（不是 .msc-alert），
    结构是 .msc-confirm > .msc-overlay > button.msc-close（×）
    ＋ .msc-content > h3.msc-title / .msc-body / button.msc-ok（OK）。
    站点还导出了全局 mscClose()，直接调它最干净。
    """
    # 0) 最干净的做法：直接调站点自己的 mscClose()
    try:
        closed = page.evaluate(
            """() => {
                if (!document.querySelector('.msc-confirm')) return false;
                if (typeof window.mscClose === 'function') { window.mscClose(); return true; }
                return false;
            }"""
        )
        if closed:
            page.wait_for_timeout(300)
            _log("已关闭弹窗")
            return True
    except Exception:
        pass

    # 1) 先找带文字的确认按钮
    for txt in ["OK", "确定", "知道了", "关闭", "好"]:
        try:
            loc = page.get_by_role("button", name=txt, exact=True).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2500)
                page.wait_for_timeout(400)
                _log("已关闭弹窗")
                return True
        except Exception:
            continue

    # 2) 再找常见的关闭元素（mscAlert 的 × / .close / [aria-label=close]）
    for sel in [
        ".msc-confirm .msc-ok",
        ".msc-confirm .msc-close",
        ".msc-alert .close",
        ".modal .close",
        "button[aria-label='Close']",
        "button[aria-label='close']",
        "[data-dismiss='modal']",
        ".msc-alert button",
    ]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2500)
                page.wait_for_timeout(400)
                _log("已关闭弹窗")
                return True
        except Exception:
            continue

    # 2b) 弹窗按钮定位不到时，用坐标点击（Camoufox 下 locator 点击常点不到实位）
    try:
        clicked = page.evaluate(
            """() => {
                const cands = [];
                for (const el of document.querySelectorAll('button, .close, [aria-label*=close i], [aria-label*=Close]')) {
                    const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
                    if (t === 'OK' || t === '×' || t === 'x' || t === 'X' || t === '关闭' || t === '确定') {
                        const st = window.getComputedStyle(el);
                        if (st.display !== 'none' && st.visibility !== 'hidden') {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                cands.push({x: r.x + r.width/2, y: r.y + r.height/2, t});
                            }
                        }
                    }
                }
                return cands;
            }"""
        )
        for c in (clicked or [])[:3]:
            try:
                page.mouse.move(c["x"], c["y"], steps=8)
                page.wait_for_timeout(150)
                page.mouse.down()
                page.wait_for_timeout(80)
                page.mouse.up()
                page.wait_for_timeout(400)
                _log(f"已坐标点击弹窗按钮 {c.get('t')!r}")
                return True
            except Exception:
                continue
    except Exception:
        pass

    # 3) 兜底：JS 直接把可能的遮罩层移除
    try:
        removed = page.evaluate(
            """() => {
                let n = 0;
                for (const sel of ['.msc-alert', '.modal-backdrop', '.modal.show', '[role="dialog"]']) {
                    document.querySelectorAll(sel).forEach(el => {
                        const st = window.getComputedStyle(el);
                        if (st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0') {
                            el.remove(); n++;
                        }
                    });
                }
                return n;
            }"""
        )
        if removed:
            page.wait_for_timeout(300)
            _log(f"已移除 {removed} 个弹窗/遮罩元素")
            return True
    except Exception:
        pass
    return False


def _fill_input_safe(loc, value: str, label: str = "") -> bool:
    """填输入框，优先 fill()（不受遮挡影响），失败再退回 click+type。

    实测：locator.click() 在被模态框遮挡时会 TimeoutError，
    而 fill() 直接设值不依赖可点击性。
    """
    try:
        loc.fill(value)
        return True
    except Exception as e:
        _log(f"{label} fill() 失败（{type(e).__name__}），改用 click+type")
    try:
        loc.click(timeout=4000)
        loc.fill("")
        loc.type(value, delay=random.randint(50, 120))
        return True
    except Exception as e:
        _log(f"{label} 填值彻底失败：{type(e).__name__}")
        return False


def _signin_via_page_fetch(page, username: str, password: str):
    """在页面 JS 上下文里主动 fetch 登录，拿到真实响应体。

    为什么要这么做（2026-10 实测根因）：
      NodeSeek 登录响应是 content-encoding: br（Brotli），Camoufox/Firefox 内核下
      Playwright 的 resp.body()/resp.json()/resp.text() 全部报
      NS_ERROR_INVALID_CONTENT_ENCODING，导致只能看到「HTTP 200」而读不到失败原因。

      而在页面自己的 JS 环境里 await resp.text()，用的是浏览器内置解码器，
      能正确解 br。所以这里绕过 Playwright 的网络层，直接在页面里发请求、读 body。

      登录所需的 header 都从前端已有的值里取：
        - x-captcha-token   = Turnstile token（hidden input）
        - x-security-token  = localStorage.security_token
        - x-integrity-token = FingerprintJS visitorId（若前端暴露在 window 上）
    """
    js = """
    async (payload) => {
        const out = { status: null, body: '', error: null };

        const getToken = () => {
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            if (el && el.value) return el.value;
            const tas = document.querySelectorAll('textarea[name="cf-turnstile-response"]');
            for (const t of tas) { if (t.value) return t.value; }
            return '';
        };

        const captchaToken = getToken() || (payload.token || '');
        if (!captchaToken) {
            out.error = 'NO_CAPTCHA_TOKEN';
            return out;
        }

        let securityToken = '';
        try { securityToken = localStorage.getItem('security_token') || ''; } catch (e) {}

        // FingerprintJS visitorId：可能挂在 window 上，或需要现算（先探测常见位置）
        let integrityToken = '';
        try {
            integrityToken = window.__visitorId
                || window.visitorId
                || (window.fp && window.fp.visitorId)
                || '';
        } catch (e) {}

        const headers = {
            'content-type': 'application/json',
            'x-captcha-token': captchaToken,
            'x-captcha-source': 'turnstile',
        };
        if (securityToken) headers['x-security-token'] = securityToken;
        if (integrityToken) headers['x-integrity-token'] = integrityToken;

        try {
            const resp = await fetch('/api/account/signIn', {
                method: 'POST',
                headers: headers,
                body: JSON.stringify({ username: payload.username, password: payload.password }),
                credentials: 'include',
            });
            out.status = resp.status;
            try {
                out.body = await resp.text();
            } catch (e) {
                out.body = 'BODY_ERR:' + (e && e.message ? e.message : e);
            }
        } catch (e) {
            out.error = String(e && e.message ? e.message : e);
        }
        return out;
    }
    """
    try:
        # 登录页的 token 正常会写进 hidden input；但万一这页也是 explicit render
        # （token 只在组件里），就用 _acquire_ts_token 兜底取一个填进来。
        fallback_tok = _acquire_ts_token(page).get("token") or ""
        return page.evaluate(js, {"username": username, "password": password,
                                  "token": fallback_tok})
    except Exception as e:
        _log(f"主动 fetch 登录异常：{type(e).__name__}: {e}")
        return None


def _prior_login_traces(page) -> str:
    """检查浏览器 profile 里是否留有「之前登录过这台设备」的痕迹。

    用途：区分两种「需要邮箱验证码」：
      · 首次在新设备/新 profile 上登录 → 要码是正常的
      · 这台设备之前登录过（profile 里有痕迹），这次仍要码
        → 说明 NodeSeek 侧的「记住设备」状态已经失效了（需要告警）

    痕迹来源（都在同一 origin 的持久化存储里）：
      · localStorage 里的 security_token（登录页前端会生成）
      · profile 目录里存过的 NodeSeek cookie（登录态 cookie 已被清掉时只剩名字）
    返回一段描述文字，没有痕迹则返回空串。
    """
    found = []
    try:
        ls = page.evaluate(
            """() => {
                const out = [];
                try {
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        if (k) out.push(k);
                    }
                } catch (e) {}
                return out;
            }"""
        )
        if isinstance(ls, list) and ls:
            for k in ls:
                if k in ("security_token", "__ns_api_log"):
                    found.append(k)
    except Exception:
        pass

    try:
        names = [c.get("name") for c in (page.context.cookies() or [])]
        # session/pjwt/smac 已经不在了（否则这轮就登录成功了），
        # 但如果还留着 cf_clearance 之类的，说明这台设备确实来过。
        if any(n in ("cf_clearance", "fog", "hmti_") for n in names):
            found.append("cf_clearance/fog")
    except Exception:
        pass

    if not found:
        return ""
    return "、".join(dict.fromkeys(found))


def _obtain_email_code(timeout: int = 0) -> str:
    """拿到邮箱验证码。三级降级，任何一级成功就返回。

    级别 1（自动，推荐）：验证码落地文件
        在邮箱里设一条「转发规则」，把 NodeSeek 的邮件自动转发到一个
        「落地文件」服务（下面两种任选），脚本轮询该文件即可自动取码。
        完全不需要把邮箱密码交给脚本。

        · NS_CODE_FILE   —— 本机文件路径。转发到一个能写这个路径的
                           脚本/钩子即可（curl 到 webhook、mailhook、
                           或任意自建小服务）。
        · NS_CODE_URL    —— HTTP(S) 地址，返回纯文本验证码内容。

    级别 2：IMAP 自动收码（可选，需要你显式配置）
        NS_IMAP_HOST / NS_IMAP_PORT / NS_IMAP_USER / NS_IMAP_PASS
        仅在你愿意把「邮箱授权码」（不是登录密码）交给脚本时使用。

    级别 3：终端手动输入（兜底）
        前两级都没配时，退回交互式输入；cron 里没有终端则直接失败，
        并提示怎么配置自动收码。

    设计原则：默认什么都不配 → 行为与从前完全一致（手动输入）。
    只有你主动配置了自动收码，才会走无人值守路径。
    """
    timeout = timeout or int(os.getenv("NS_CODE_TIMEOUT", "180") or 180)

    # ---------- 级别 1：验证码落地文件 / HTTP ----------
    file_path = os.getenv("NS_CODE_FILE", "").strip()
    code_url = os.getenv("NS_CODE_URL", "").strip()
    if file_path or code_url:
        code = _poll_code_from_source(file_path, code_url, timeout)
        if code:
            _log(f"已自动获取验证码：{code[:4]}…{code[-4:]}（共{len(code)}位）")
            return code
        _log("自动收码超时/失败，尝试下一级")

    # ---------- 级别 2：IMAP ----------
    if os.getenv("NS_IMAP_HOST", "").strip() and os.getenv("NS_IMAP_USER", "").strip():
        code = _poll_code_imap(timeout)
        if code:
            _log(f"已通过 IMAP 自动获取验证码：{code[:4]}…{code[-4:]}（共{len(code)}位）")
            return code
        _log("IMAP 收码超时/失败，尝试下一级")

    # ---------- 级别 3：手动输入 ----------
    try:
        raw = input("请输入邮箱验证码：").strip()
        return raw
    except EOFError:
        _log("当前不是交互式终端（cron / 管道），无法手动输入验证码。")
        _log("要实现完全无人值守，配以下任一种即可（都不需要交出邮箱密码）：")
        _log("  · NS_CODE_FILE=/路径/验证码.txt   邮箱转发规则把邮件写入该文件")
        _log("  · NS_CODE_URL=https://…            返回纯文本验证码的 HTTP 地址")
        _log("  · NS_IMAP_HOST/NS_IMAP_USER/NS_IMAP_PASS  用邮箱授权码收信")
        _log("详见 部署指南_账号密码自动登录.md「让验证码也自动化」一节。")
        return ""
    except Exception:
        return ""


def _extract_code_from_text(text: str) -> str:
    """从邮件正文/文本里抠出验证码。

    NodeSeek 的验证码实测是 24 位十六进制串（如 8415f1a2b52331d5a2b9078e）。
    但不同批次可能是别的字母数字组合，所以：
      1) 优先匹配「验证码/validation code/code」附近的长串字母数字
      2) 兜底匹配整段文本里最长的 16~64 位字母数字串
    """
    if not text:
        return ""
    t = text.strip()

    # 去掉 HTML 标签
    plain = re.sub(r"<[^>]+>", " ", t)
    plain = re.sub(r"&nbsp;?", " ", plain)

    # 1) 关键词附近找
    for kw in ("验证码", "校验码", "动态码", "validation code", "verification code",
               "your code", "code"):
        for m in re.finditer(re.escape(kw), plain, re.IGNORECASE):
            tail = plain[m.end(): m.end() + 120]
            cand = re.search(r"[A-Za-z0-9]{16,64}", tail)
            if cand:
                return cand.group(0)

    # 2) 整段找最长的字母数字串（>=16 位）
    cands = re.findall(r"[A-Za-z0-9]{16,64}", plain)
    if cands:
        return max(cands, key=len)
    return ""


def _poll_code_from_source(file_path: str, code_url: str, timeout: int) -> str:
    """轮询验证码来源（文件或 HTTP），拿到就返回。

    文件方式会记住上次读到的内容，避免读到旧验证码重复提交。
    """
    deadline = time.time() + timeout
    last_seen = ""
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                last_seen = f.read()
        except Exception:
            pass

    while time.time() < deadline:
        raw = ""
        if file_path:
            try:
                if os.path.exists(file_path):
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        raw = f.read()
            except Exception:
                raw = ""
        if not raw and code_url:
            try:
                import urllib.request
                req = urllib.request.Request(code_url, headers={"User-Agent": "ns-code/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read().decode("utf-8", errors="ignore")
            except Exception as e:
                _dbg(f"取验证码 HTTP 失败：{type(e).__name__}")

        if raw and raw.strip() and raw.strip() != last_seen.strip():
            code = _extract_code_from_text(raw)
            if code:
                return code
        time.sleep(3)
    return ""


def _poll_code_imap(timeout: int) -> str:
    """IMAP 自动收码（可选功能，需要你显式配置邮箱授权码）。"""
    try:
        import imaplib
        import email as _email
        from email.header import decode_header
    except Exception:
        return ""

    host = os.getenv("NS_IMAP_HOST", "").strip()
    port = int(os.getenv("NS_IMAP_PORT", "993") or 993)
    user = os.getenv("NS_IMAP_USER", "").strip()
    pwd = os.getenv("NS_IMAP_PASS", "").strip()
    folder = os.getenv("NS_IMAP_FOLDER", "INBOX").strip() or "INBOX"
    sender_kw = os.getenv("NS_IMAP_SENDER", "nodeseek").strip().lower()

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(host, port)
            try:
                M.login(user, pwd)
                M.select(folder)
                # 只看最近 10 封未读
                typ, data = M.search(None, 'UNSEEN')
                if typ == "OK" and data and data[0]:
                    ids = data[0].split()[-10:]
                    for mid in reversed(ids):
                        typ, msg_data = M.fetch(mid, "(RFC822)")
                        if typ != "OK":
                            continue
                        msg = _email.message_from_bytes(msg_data[0][1])
                        subj = ""
                        for part in msg.walk():
                            if part.get_content_type() == "text/html":
                                try:
                                    subj = part.get_payload(decode=True).decode(
                                        part.get_content_charset() or "utf-8", errors="ignore")
                                except Exception:
                                    pass
                                break
                            if part.get_content_type() == "text/plain" and not subj:
                                try:
                                    subj = part.get_payload(decode=True).decode(
                                        part.get_content_charset() or "utf-8", errors="ignore")
                                except Exception:
                                    pass
                        blob = json.dumps(msg.items(), ensure_ascii=False) + " " + (subj or "")
                        if sender_kw and sender_kw not in blob.lower():
                            continue
                        code = _extract_code_from_text(subj or blob)
                        if code:
                            try:
                                M.store(mid, "+FLAGS", "\\Seen")
                            except Exception:
                                pass
                            return code
            finally:
                try:
                    M.logout()
                except Exception:
                    pass
        except Exception as e:
            _dbg(f"IMAP 收码失败：{type(e).__name__}: {e}")
        time.sleep(5)
    return ""


def _json_msg(body: str) -> str:
    """从 JSON 响应体里挖出错误/提示信息。"""
    try:
        d = json.loads(body)
    except Exception:
        return ""
    if isinstance(d, dict):
        for k in ("message", "msg", "error", "errMsg", "errmsg", "data"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _looks_like_error(text: str) -> bool:
    """一段文字看起来像失败提示吗？"""
    t = (text or "").lower()
    keys = ("失败", "错误", "频繁", "无效", "过期", "不允许", "被拒", "不存在",
            "error", "invalid", "too many", "rate limit", "denied", "expired")
    return any(k in t for k in keys)


def _looks_like_sent(text: str) -> bool:
    """一段文字看起来像「已发送」的成功提示吗？

    注意站点的原文是「验证码**已经**发送，如未收到请检查垃圾邮件」——
    「已发送」是「已经发送」的子串吗？不是！中间隔了个「经」。
    旧实现只匹配「已发送」，所以站点真成功时反而判不出来，这里补上「已经发送」。
    """
    t = (text or "").lower()
    keys = ("已经发送", "已发送", "发送成功", "已发", "sent successfully", "send success")
    return any(k in t for k in keys)


def _is_send_api(url: str) -> bool:
    """这个接口看起来像「发验证码」吗？（用来从一堆 API 响应里挑出对的那个）

    站点真实的发码接口是 POST /api/email（2026-10 逆向自前端 bundle），
    先精确匹配它，再退回粗筛。注意别把 /api/account/emailSignIn（提交验证码）
    也当成发码——那俩是不同的事。
    """
    u = (url or "").lower()
    if "/api/email" in u:
        return True
    return any(k in u for k in ("send", "code", "verify", "captcha"))


def _api_log_window(page) -> list:
    """读当前页面的 API 响应记录（只看 window 上的那份，避免和 localStorage 混淆）。"""
    try:
        log = page.evaluate("() => window.__ns_api_log || []")
        return log if isinstance(log, list) else []
    except Exception:
        return []


def _send_btn_text(page) -> str:
    """读「发送验证码」按钮当前的文字。"""
    try:
        return page.evaluate(
            """() => {
                for (const b of document.querySelectorAll('button')) {
                    const t = (b.innerText || '').trim();
                    if (t && /发送|重发|秒|重新/.test(t)) return t;
                }
                return '';
            }"""
        ) or ""
    except Exception:
        return ""


def _is_countdown(text: str) -> bool:
    """按钮文字是不是变成了倒计时（「60s 后重发」之类）——说明发码请求被接受了。"""
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"\d+\s*(?:s|秒)", t):
        return True
    return _looks_like_sent(t)


def _page_notice_text(page) -> str:
    """页面上冒出来的提示文字（发码成功/失败的消息通常在这里）。

    注意：站点用的是 msc.js，弹窗容器 class 是 **.msc-confirm**，
    旧代码只抓 .msc-alert / .modal.show，站点真实提示一条都读不到。
    """
    try:
        return (page.evaluate(
            """() => {
                for (const sel of ['.msc-confirm', '.msc-alert', '.modal.show',
                                   '[role="dialog"]',
                                   '.swal2-container', '.invalid-feedback',
                                   '[class*="toast"]', '[class*="Toast"]']) {
                    for (const el of document.querySelectorAll(sel)) {
                        const st = window.getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden' ||
                            st.opacity === '0') continue;
                        const t = (el.innerText || '').trim();
                        if (t) return t.slice(0, 300);
                    }
                }
                return '';
            }"""
        ) or "").strip()
    except Exception:
        return ""


def _judge_send_response(entries: list, strict: bool = False):
    """从发码接口的响应里判断成功/失败。

    返回 (ok, detail)；没有结论时返回 None。

    strict=True 表示这些条目已经确认是「发验证码」接口的响应，
    此时 HTTP 2xx 即视为发码成功；strict=False（来源不确定）时，
    光一个 2xx 不算数，返回 None 继续等更明确的信号（按钮倒计时/页面提示）。
    """
    for item in entries:
        body = (item.get("body") or "").strip()
        status = item.get("status")
        if not body:
            # 没有 body 但状态码是 4xx/5xx —— 也是失败
            if isinstance(status, int) and status >= 400:
                return False, f"接口返回 HTTP {status}"
            continue
        norm = body.replace(" ", "").lower()
        # 失败信号优先，免得把 {"success":false,...} 误判成成功
        if '"success":false' in norm:
            msg = _json_msg(body)
            return False, f"接口明确返回失败：{msg or body[:200]}"
        if '"success":true' in norm:
            msg = _json_msg(body)
            return True, f"接口返回成功：{msg or body[:120]}"
        if '"code":0' in norm or '"status":200' in norm:
            return True, f"接口返回成功：{body[:120]}"
        msg = _json_msg(body)
        if msg and _looks_like_error(msg):
            return False, f"接口返回：{msg}"
        if _looks_like_error(body):
            return False, f"接口返回：{body[:200]}"
        if strict and isinstance(status, int) and 200 <= status < 300:
            return True, f"接口 HTTP {status}"
    return None


def _fill_email_field(page, email_hint: str) -> tuple:
    """在邮箱验证页上把邮箱填好。

    **必须在点「发送验证码」之前调用。**
    这个页面的发码请求体里带邮箱地址，框是空的话请求根本发不出去
    （或者发到空地址），表现就是「日志说点了发送验证码，但邮箱里什么都没有」。
    这正是「新机器部署又没收到验证码」的根因：新 profile 上前端不会自动填邮箱。

    返回 (是否可用, 邮箱地址)。
    页面自己已经填好了就直接用页面上的值 —— redirect 里没带 email 参数时，
    这是唯一能拿到收件地址的机会。
    """
    email_input = _first_visible(
        page,
        ["input[placeholder='Email']", 'input[placeholder*="Email"]',
         'input[type="email"]', "input[name='email']"],
        timeout=5000,
    )
    if email_input is None:
        _log("⚠️ 找不到邮箱输入框")
        return False, ""
    cur = ""
    try:
        cur = (email_input.input_value() or "").strip()
    except Exception:
        pass
    if cur:
        _log(f"邮箱框已有值：{cur}")
        return True, cur
    if not email_hint:
        _log("⚠️ 拿不到绑定邮箱（redirect 里没解析出 email 参数，页面也没自动填）")
        return False, ""
    if not _fill_input_safe(email_input, email_hint, "邮箱框"):
        return False, ""
    page.wait_for_timeout(300)
    filled = ""
    try:
        filled = (email_input.input_value() or "").strip()
    except Exception:
        pass
    if filled != email_hint.strip():
        _log(f"⚠️ 邮箱框回读不一致：期望 {email_hint!r}，实际 {filled!r}")
        return False, ""
    _log(f"已填入邮箱：{filled}")
    return True, filled


# ---------------------------------------------------------------------------
# Turnstile token 与「发验证码」——完全按站点真实协议实现
#
# 下面是 2026-10 从站点前端 bundle（assets/index-Bz3_0Bch.js，emailSignIn 页）
# 逆向出来的发码逻辑原文，一字未改：
#
#   async function x(){
#     if(!w(c.email)){ m.value=!1; return }                       # 邮箱格式非法 → 静默 return
#     if(n.value!=="loading"){
#       if(!r.value){ mscAlert("请先通过人机验证"); return }          # 没 token → 弹提示，**一个请求都不发**
#       n.value="loading";
#       try{
#         const t=await fetch("/api/email",{method:"POST",
#           headers:{"content-type":"application/json"},
#           body:JSON.stringify({email:c.email,mode:c.mode,token:r.value,
#                                source:"turnstile",version:"v3"})}).then(e=>e.json());
#         if(t.success){ V(); mscAlert("验证码已经发送，如未收到请检查垃圾邮件"); return }
#         n.value="not-started"; mscAlert(t.message||"操作失败"); p.value.reset()
#       }catch{ n.value="not-started"; mscAlert("网络错误"); p.value.reset() }
#     }
#   }
#
# 由此确认了三个此前踩空的事实：
#   1) 发码接口是 POST /api/email，token 放在 **body** 里（不是 header）；
#      emailSignIn 页的 mode 固定 "totp"，另有 source="turnstile"、version="v3"；
#      该接口不需要 x-security-token / x-integrity-token（站点自己就没带）；
#   2) Turnstile 是 explicit render，token 只经 callback 存进 Vue 状态，
#      **不写 hidden input**，只能靠 Turnstile 封装组件 expose 出来的
#      getResponse() 取（它 expose 了 {reset, removeWidget, getResponse}）；
#   3) 没有 token 时站点直接 return，**不会发任何请求**——旧实现把这种情况
#      笼统报成「20 秒内没看到任何接口响应」，真实原因被吞掉了；
#      而 mscAlert 的弹窗 class 是 .msc-confirm（提示文字在 h3.msc-title），
#      旧代码抓的是 .msc-alert / .modal.show，同样抓不到。
# ---------------------------------------------------------------------------

# 取 token：遍历 Vue 组件树，找 Turnstile 封装组件 expose 出来的 getResponse()。
#
# ⚠️ 关键（2026-10 真机实测）：**不能**靠遍历 DOM 找 __vueParentComponent——
# Vue 生产构建默认不挂这个属性（__VUE_PROD_DEVTOOLS__ = false），实测页面上
# 带 __vueParentComponent 的元素数量是 0。
# 正确的入口是挂载容器的 _vnode.component：
#   1) 找到带 __vue_app__ 的宿主元素
#   2) host._vnode.component = 根组件实例（app._instance 不一定有）
#   3) 沿 inst.subTree 递归子组件
# 真机实测到的组件树：
#   index（emailSignIn 页）  refs: emailAuthRef -> 验证码组件
#   index（验证码组件）       refs: turnstileRef -> Turnstile 封装组件
#   index（Turnstile 封装）   exposed: ['reset','removeWidget','getResponse']
_TS_TOKEN_JS = r"""
() => {
    const res = {token: '', via: ''};

    const pick = (c) => {
        try {
            if (c && typeof c.getResponse === 'function') {
                const t = c.getResponse();
                if (typeof t === 'string' && t.length > 20) return t;
            }
        } catch (e) {}
        return '';
    };

    function findRoot() {
        for (const el of document.querySelectorAll('*')) {
            const app = el.__vue_app__;
            if (!app) continue;
            if (el._vnode && el._vnode.component) return el._vnode.component;
            if (app._instance) return app._instance;
            if (app._container && app._container._vnode && app._container._vnode.component) {
                return app._container._vnode.component;
            }
        }
        return null;
    }

    const seen = new Set();
    function visitInst(inst) {
        if (!inst || seen.has(inst)) return '';
        seen.add(inst);
        // 1) 自身 expose 的 getResponse（Turnstile 封装组件）
        let t = pick(inst.exposed);
        if (t) return t;
        // 2) 模板 ref 指向的子组件
        try {
            const refs = inst.refs || {};
            for (const k of Object.keys(refs)) {
                t = pick(refs[k]);
                if (t) return t;
            }
        } catch (e) {}
        // 3) 子组件
        try { return visitVNode(inst.subTree); } catch (e) { return ''; }
    }
    function visitVNode(v) {
        if (!v || typeof v !== 'object') return '';
        let t = '';
        if (v.component) { t = visitInst(v.component); if (t) return t; }
        if (Array.isArray(v.children)) {
            for (const c of v.children) { t = visitVNode(c); if (t) return t; }
        }
        if (v.suspense) {
            try { t = visitVNode(v.suspense.activeBranch); } catch (e) {}
            if (!t) { try { t = visitVNode(v.suspense.pendingBranch); } catch (e) {} }
        }
        return t;
    }

    const root = findRoot();
    if (root) {
        const token = visitInst(root);
        if (token) { res.token = token; res.via = 'vue-tree-getResponse'; return res; }
    }

    // 4) Turnstile 全局 API（不带 widget id 时取第一个组件）
    try {
        if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
            const t = window.turnstile.getResponse();
            if (typeof t === 'string' && t.length > 20) {
                res.token = t; res.via = 'turnstile-global'; return res;
            }
        }
    } catch (e) {}
    // 5) hidden input 兜底（Cloudflare 组件容器里确实会写这个 input）
    try {
        for (const el of document.querySelectorAll(
                'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]')) {
            if (el.value && el.value.length > 20) {
                res.token = el.value; res.via = 'hidden-input'; return res;
            }
        }
    } catch (e) {}
    return res;
}
"""

# 重置 Turnstile：站点在发码失败时也是这么干的（p.value.reset()），
# 目的是让组件出一个**新** token（旧的已被用过，是一次性的）。
_TS_RESET_JS = r"""
() => {
    const call = (c) => {
        try {
            if (c && typeof c.reset === 'function') { c.reset(); return true; }
        } catch (e) {}
        return false;
    };
    function findRoot() {
        for (const el of document.querySelectorAll('*')) {
            const app = el.__vue_app__;
            if (!app) continue;
            if (el._vnode && el._vnode.component) return el._vnode.component;
            if (app._instance) return app._instance;
            if (app._container && app._container._vnode && app._container._vnode.component) {
                return app._container._vnode.component;
            }
        }
        return null;
    }
    const seen = new Set();
    function visitInst(inst) {
        if (!inst || seen.has(inst)) return false;
        seen.add(inst);
        if (call(inst.exposed)) return true;
        try {
            const refs = inst.refs || {};
            for (const k of Object.keys(refs)) {
                if (call(refs[k])) return true;
            }
        } catch (e) {}
        try { return visitVNode(inst.subTree); } catch (e) { return false; }
    }
    function visitVNode(v) {
        if (!v || typeof v !== 'object') return false;
        if (v.component && visitInst(v.component)) return true;
        if (Array.isArray(v.children)) {
            for (const c of v.children) { if (visitVNode(c)) return true; }
        }
        if (v.suspense) {
            try { if (visitVNode(v.suspense.activeBranch)) return true; } catch (e) {}
            try { if (visitVNode(v.suspense.pendingBranch)) return true; } catch (e) {}
        }
        return false;
    }
    const root = findRoot();
    if (root) return visitInst(root);
    return false;
}
"""

# 按站点原样发码。放在页面上下文里发，cookie / 同源 / UA / 出口 IP 全部一致。
_SEND_EMAIL_JS = r"""
async (args) => {
    const {email, mode, token} = args;
    try {
        const resp = await fetch('/api/email', {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({
                email: email, mode: mode, token: token,
                source: 'turnstile', version: 'v3'
            })
        });
        let body = '';
        try { body = await resp.text(); } catch (e) { body = 'READ_ERR'; }
        let parsed = null;
        try { parsed = JSON.parse(body); } catch (e) {}
        const msg = (parsed && (parsed.message || parsed.msg || parsed.error || parsed.errMsg)) || '';
        return {
            status: resp.status,
            body: String(body).slice(0, 800),
            success: !!(parsed && parsed.success),
            message: String(msg)
        };
    } catch (e) {
        return {
            status: 0,
            body: 'EXC:' + (e && e.message ? e.message : e),
            success: false,
            message: '网络错误'
        };
    }
}
"""


def _acquire_ts_token(page) -> dict:
    """拿 Turnstile token。返回 {"token": str, "via": str}。"""
    try:
        res = page.evaluate(_TS_TOKEN_JS)
        return res if isinstance(res, dict) else {}
    except Exception as e:
        _dbg(f"读 Turnstile token 异常：{type(e).__name__}")
        return {}


def _reset_ts_widget(page) -> bool:
    """让 Turnstile 组件重出一个新 token（旧的已被用过，是一次性的）。"""
    try:
        ok = page.evaluate(_TS_RESET_JS)
        if ok:
            _log("已重置 Turnstile 组件，等它出新的 token")
        return bool(ok)
    except Exception:
        return False


def _post_send_email(page, email: str, mode: str, token: str) -> dict:
    """按站点真实协议调 POST /api/email 发码。"""
    try:
        res = page.evaluate(_SEND_EMAIL_JS,
                             {"email": email, "mode": mode, "token": token})
        return res if isinstance(res, dict) else {}
    except Exception as e:
        _dbg(f"发码请求异常：{type(e).__name__}")
        return {"status": 0, "body": f"EXC:{e}", "success": False, "message": ""}


def _judge_api_email(res: dict) -> tuple:
    """把 /api/email 的响应判成 (是否成功, 详情)。"""
    if not res:
        return False, "发码请求没有返回结果"
    status = res.get("status") or 0
    body = str(res.get("body") or "").strip()
    msg = str(res.get("message") or "").strip()
    if res.get("success"):
        return True, f"接口返回成功（HTTP {status}）" + (f"：{msg}" if msg else "")
    if status == 0:
        return False, f"发码请求没发出去：{body[:200] or '网络错误'}"
    if msg:
        return False, f"接口返回失败（HTTP {status}）：{msg}"
    return False, f"接口返回失败（HTTP {status}）：{body[:200]}"


def _looks_like_token_problem(text: str) -> bool:
    """失败原因是不是「token 不对/过期/用过了」这类——这类才值得重来一次。"""
    t = (text or "").lower()
    keys = ("token", "captcha", "人机", "turnstile", "expired", "invalid",
            "无效", "过期", "重复", "duplicate")
    return any(k in t for k in keys)


def _ns_alert_text(page) -> str:
    """读站点 mscAlert 弹窗里的提示文字。

    站点用的是 msc.js：容器 class 是 **.msc-confirm**（不是 .msc-alert），
    结构是 .msc-confirm > .msc-content > h3.msc-title（提示文字）
    ＋ .msc-body（正文），确认按钮是 button.msc-ok（文字 "OK"）。
    旧实现抓 .msc-alert / .modal.show，所以站点弹的「请先通过人机验证」、
    「Cloudflare人机验证服务加载失败」这些真实原因一条都读不到。
    """
    try:
        return (page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('.msc-confirm')) {
                    const st = window.getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden') continue;
                    const title = el.querySelector('h3.msc-title');
                    const body = el.querySelector('.msc-body');
                    let s = '';
                    if (title) s += (title.innerText || '').trim();
                    if (body) {
                        const bt = (body.innerText || '').trim();
                        if (bt && bt !== s) s += (s ? ' / ' : '') + bt;
                    }
                    if (!s) s = (el.innerText || '').trim();
                    if (s) return s.slice(0, 300);
                }
                return '';
            }"""
        ) or "").strip()
    except Exception:
        return ""


def _better_detail(new: str, old: str) -> str:
    """挑更有信息量的失败原因（别用含糊的覆盖具体的）。"""
    if not new:
        return old
    if not old:
        return new
    for g in ("没看到任何接口响应", "点了按钮，但"):
        if g in new:
            return old
    return new


def _click_send_and_watch(page, before: int = 0, wait: int = 20) -> tuple:
    """点站点自己的「发送验证码」按钮，并观察站点给出的结果。

    这是拿不到 token 时的兜底路径。返回 (ok, detail)；
    完全观察不出结果时 detail 是空串（表示「什么线索都没有」）。
    """
    # 上一次残留的弹窗会盖住按钮，先清掉
    try:
        _dismiss_dialog(page)
    except Exception:
        pass

    send_btn = None
    for sel in ["button:has-text('发送验证码')", "button:has-text('发送')",
                "button:has-text('获取')"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                send_btn = loc
                break
        except Exception:
            continue
    if send_btn is None:
        return False, "页面上没找到「发送验证码」按钮"

    # 坐标点击（locator.click() 在 Camoufox 下可能点到被遮挡/隐藏元素，
    # 表现为「已点击」但实际没触发发信）
    try:
        box = send_btn.bounding_box()
        if box:
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            page.mouse.move(x, y, steps=random.randint(5, 12))
            page.wait_for_timeout(random.randint(100, 300))
            page.mouse.down()
            page.wait_for_timeout(random.randint(60, 140))
            page.mouse.up()
            _log("已点击「发送验证码」（坐标点击）")
        else:
            send_btn.click(timeout=5000)
            _log("已点击「发送验证码」（locator 点击）")
    except Exception as e:
        return False, f"点击发送验证码失败：{type(e).__name__}"

    deadline = time.time() + wait
    while time.time() < deadline:
        page.wait_for_timeout(600)
        # 1) 站点弹窗里的真实提示（成功/失败原因都在这儿）
        alert = _ns_alert_text(page)
        if alert:
            if _looks_like_sent(alert):
                return True, f"页面提示：{alert}"
            return False, f"页面提示：{alert}"
        # 2) 接口响应
        new = _api_log_window(page)[before:]
        if new:
            send_entries = [e for e in new if _is_send_api(e.get("url") or "")]
            if send_entries:
                verdict = _judge_send_response(send_entries, strict=True)
                if verdict is not None:
                    return verdict
            for e in new[-3:]:
                _dbg(f"发码接口响应：{e.get('url')} status={e.get('status')} "
                     f"body={str(e.get('body'))[:200]}")
        # 3) 按钮进入倒计时
        btn_text = _send_btn_text(page)
        if _is_countdown(btn_text):
            return True, f"按钮已进入倒计时（{btn_text}）"
    return False, "点了按钮，但站点既没发请求也没弹提示"


def _send_email_code(page, email_hint: str, attempts: int = 3,
                     mode: str = "totp") -> tuple:
    """把验证码邮件真正发出去。

    返回 (是否发出, 详情说明, 实际使用的邮箱地址)。

    主路径（2026-10 按站点真实协议重写）：拿到 Turnstile token 后，直接照站点
    的样子调 POST /api/email，读接口的真实 JSON 判定成败——不再依赖
    「坐标点击 + 猜按钮文字」这种一戳就碎的链路。

    兜底：拿不到 token（或 token 类失败）时，先用站点自己的组件重置 Turnstile
    拿新 token；再不行才去点站点按钮，并读站点弹窗的真实提示。

    为什么不能「点了就当成功」：发码请求可能被服务器拒（Turnstile 没通过、
    频率限制、邮箱为空…），这时候邮箱里永远不会出现邮件。旧版点完就直接让
    用户去查收码，用户只能干等——这就是「又没收到验证码」的真正原因：
    请求压根没发出去，日志却什么都没说。
    """
    detail = ""
    best_detail = ""
    used_email = email_hint
    for attempt in range(1, attempts + 1):
        # 记录器要在发请求之前就装好，否则抓不到这次请求的响应
        _install_api_logger(page)
        # 邮箱必须先填好——发码请求要带收件地址
        filled_ok, actual_email = _fill_email_field(page, email_hint)
        if not filled_ok:
            return False, "邮箱输入框没能填上，发码请求没有收件地址", ""
        if actual_email and actual_email != email_hint:
            # 页面自己填的地址更可信（redirect 里可能没带 email 参数）
            email_hint = actual_email
        used_email = actual_email or email_hint
        if not used_email:
            return False, "拿不到收件邮箱，发码请求发不出去", ""

        # ---- A. 拿到 token → 按站点协议直接发码 ----
        tok = _acquire_ts_token(page)
        token = tok.get("token") or ""
        if token:
            _log(f"已取得 Turnstile token（{len(token)} 字符，来源：{tok.get('via')}）")
            res = _post_send_email(page, used_email, mode, token)
            ok, detail = _judge_api_email(res)
            if ok:
                _log(f"✓ 验证码已发出（{detail}）")
                return True, detail, used_email
            _log(f"✗ 发码接口返回失败：{detail}")
            best_detail = _better_detail(detail, best_detail)
            if _looks_like_token_problem(detail):
                # token 已被用过（或过期）：不能拿它去点按钮了，
                # 必须让组件出一个新的再来。
                _reset_ts_widget(page)
            elif "没发出去" in detail:
                # 网络异常，可以重试
                pass
            else:
                # 业务侧拒绝（频率限制、邮箱不存在等），再试几次也没用
                return False, detail, used_email
        else:
            # ---- B. 拿不到 token：点站点自己的按钮，看站点给的真实提示 ----
            # 站点没有 token 时会弹「请先通过人机验证」并且**一个请求都不发**，
            # 这条提示才是真实原因——旧代码抓不到 .msc-confirm，
            # 才会退化成一句没信息量的「20 秒内没看到任何接口响应」。
            _log("⚠️ 没取到 Turnstile token（组件还没出 token，或取 token 的路径没命中）")
            before = len(_api_log_window(page))
            b_ok, b_detail = _click_send_and_watch(page, before)
            if b_ok:
                _log(f"✓ 验证码已发出（{b_detail}）")
                return True, b_detail, used_email
            if b_detail:
                _log(f"✗ 第 {attempt}/{attempts} 次发码未成功：{b_detail}")
                best_detail = _better_detail(b_detail, best_detail)
            _reset_ts_widget(page)

        if attempt < attempts:
            _log("重试：等 Turnstile 出新 token 再试")
            try:
                _wait_turnstile(page, timeout=30, widget_only_ok=True)
            except Exception:
                pass
    return False, best_detail or detail or "发码失败", used_email


def _handle_email_verify(page, redirect: str, username: str):
    """处理密码登录后的邮箱验证码二次确认。

    NodeSeek 登录是两步：密码验证通过后，redirect 到 emailSignIn.html 要求
    邮箱验证码。这里跳转到该页 → 填邮箱 → 点「发送验证码」→ **确认邮件真的
    发出去了** → 停在命令行等用户把码输进来 → 填入 Validation Code → 点登录。

    为什么一定要确认发码成功（2026-10 踩坑）：
      旧版点完按钮就直接让用户去邮箱查收。但发码请求可能被服务器拒
      （Turnstile 没通过 / 频率限制 / 邮箱框是空的），这时候邮箱里根本不会有
      邮件，用户只能对着空邮箱干等，日志却什么都没说。
      现在改成：读接口的真实响应 + 按钮倒计时 + 页面提示，三者交叉判定；
      判定失败会重试，全失败就明确报出原因，不再假装成功。

    之所以「停在命令行等输入」，是因为邮箱验证码是「一次性」的：完成后
    浏览器 profile 会被 NodeSeek 记住，之后同一 profile 密码登录就免验证，
    所以只需人工介入这一次。后续若要全自动，可换成 IMAP 自动收码。
    """
    url = redirect
    if url.startswith("/"):
        url = ORIGIN + url
    _log(f"跳转到邮箱验证页：{url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        _log(f"跳转邮箱验证页失败：{type(e).__name__}: {e}")
        return None
    page.wait_for_timeout(2500)

    # 1) 等该页自己的 Turnstile 验证完成
    #    emailSignIn 页面自带一个 cf-turnstile-response 组件，「发送验证码」请求
    #    很可能要带上它的 token。之前不等待就点按钮 → 点了没反应（用户反馈）。
    try:
        has_ts = page.evaluate(
            """() => {
                if (document.querySelector('input[name="cf-turnstile-response"]')) return true;
                for (const fr of window.frames || []) {
                    if (String(fr.location && fr.location.href || '').indexOf('challenges.cloudflare.com') !== -1) return true;
                }
                return false;
            }"""
        )
    except Exception:
        has_ts = True
    if has_ts:
        try:
            _log("等待邮箱验证页的 Turnstile 组件…")
            # widget_only_ok=True：callback 模式下 token 不落 hidden input，
            # 组件显示通过就算通过，别再白等满 30 秒。
            ts = _wait_turnstile(page, timeout=30, widget_only_ok=True)
            # 老版本这里无条件打印「Turnstile 已就绪」——哪怕什么都没等到。
            # 那样看日志的人会以为验证过了，其实发码请求很可能就是被它拒的。
            if ts:
                if ts == "widget-ok":
                    _log("Turnstile 已就绪（组件显示通过；token 由组件内部持有，发码时现取）")
                else:
                    _log(f"Turnstile 已就绪（{len(ts)} 字符）")
            else:
                _log("⚠️ Turnstile 30 秒内没确认通过（发码请求可能被拒，会重试）")
        except Exception as e:
            _log(f"等待 Turnstile 出现异常（不阻塞，继续尝试发送）：{type(e).__name__}")
    else:
        _log("该页没有 Turnstile 组件，直接发码")

    # 2) 取绑定邮箱 + 真正把码发出去（并且确认发出去了）
    #    redirect 形如 /emailSignIn.html?email=xxx%40yyy.com
    email_hint = ""
    m = re.search(r"[?&]email=([^&]+)", redirect or "")
    if m:
        email_hint = unquote(m.group(1)).strip()
    if not email_hint:
        # 拿不到就用登录名兜底（NodeSeek 允许邮箱登录）
        email_hint = (username or "").strip()
        if "@" not in email_hint:
            email_hint = ""
    if email_hint:
        _log(f"绑定邮箱：{email_hint}")
    else:
        _log("⚠️ 没能从 redirect 里解析出绑定邮箱")

    sent, send_detail, used_email = _send_email_code(page, email_hint)
    if not sent:
        _log(f"❌ 验证码没发出去：{send_detail}")
        _log("   → 邮箱里不会有邮件，别去查收。")
        _log("   → 常见原因：Turnstile 没通过 / 触发频率限制 / 邮箱地址不对。")
        return None
    if used_email:
        email_hint = used_email

    # 3) 取验证码（自动收码已配置则自动取，否则手动输入）
    _log("")
    _log("=" * 50)
    _log("  需要邮箱验证码（24 位字母数字串，如 8415f1a2b52331d5a2b9078e）")
    if os.getenv("NS_CODE_FILE", "").strip() or os.getenv("NS_CODE_URL", "").strip() \
            or (os.getenv("NS_IMAP_HOST", "").strip() and os.getenv("NS_IMAP_USER", "").strip()):
        _log("  已配置自动收码，正在等待邮件…")
    else:
        _log(f"  验证码已发到 {email_hint}，请查收后整段复制过来即可")
    _log("=" * 50)
    code = _obtain_email_code()
    if not code:
        _log("未取到验证码，放弃邮箱验证")
        return None
    code = code.strip().replace(" ", "")
    # 实测 NodeSeek 邮箱验证码是 24 位十六进制字符串（8415f1a2b52331d5a2b9078e），
    # 所以这里只做「字母数字、无杂质」的宽松校验，不限制长度。
    if not re.fullmatch(r"[A-Za-z0-9]{4,64}", code):
        _log(f"验证码格式异常（{code!r}），请确认复制的是邮件正文里的那一串验证码")
        return None

    # 5) 先关掉可能弹出的模态框（它会盖住输入框，导致 click 超时）
    _dismiss_dialog(page)

    # 6) 邮箱在发码之前就填好了（见 _send_email_code）。
    #    这里只做一次兜底：万一中途被页面清空了，补填回去。
    if email_hint:
        try:
            email_input = _first_visible(
                page,
                ["input[placeholder='Email']", 'input[placeholder*="Email"]',
                 'input[type="email"]', "input[name='email']"],
                timeout=3000,
            )
            if email_input is not None:
                cur_val = ""
                try:
                    cur_val = email_input.input_value() or ""
                except Exception:
                    pass
                if not cur_val.strip():
                    if _fill_input_safe(email_input, email_hint, "邮箱框"):
                        _log(f"邮箱框被清空了，已补填：{email_hint}")
                else:
                    _log(f"邮箱框已有值：{cur_val}")
        except Exception as e:
            _log(f"检查邮箱框异常（不阻塞）：{type(e).__name__}")

    # 7) 填入 Validation Code 输入框
    code_input = _first_visible(
        page,
        ["input[placeholder='Validation Code']", 'input[placeholder*="Code"]',
         'input[placeholder*="验证码"]', 'input[autocomplete="one-time-code"]'],
        timeout=5000,
    )
    if code_input is None:
        _log("找不到验证码输入框")
        return None
    if not _fill_input_safe(code_input, code, "验证码框"):
        return None
    # 确认一下真的填进去了
    try:
        filled = code_input.input_value() or ""
        if filled.strip() != code:
            _log(f"验证码框实际值 {filled!r} 与输入不一致，再试一次 fill")
            code_input.fill(code)
            filled = code_input.input_value() or ""
        _log(f"验证码框已填入：{filled!r}")
    except Exception:
        pass
    page.wait_for_timeout(random.randint(300, 700))

    # 6) 点「登录」提交
    submit = None
    for sel in ["button:has-text('登录')", "button:has-text('登 录')", "form button[type=submit]"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                submit = loc
                break
        except Exception:
            continue
    if submit is None:
        _log("找不到登录按钮")
        return None

    # 坐标点击（和「发送验证码」同一个坑：locator.click() 可能点不到实位）
    try:
        box = submit.bounding_box()
        if box:
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            page.mouse.move(x, y, steps=random.randint(5, 12))
            page.wait_for_timeout(random.randint(100, 300))
            page.mouse.down()
            page.wait_for_timeout(random.randint(60, 140))
            page.mouse.up()
            _log("已点击「登录」按钮（坐标点击）")
        else:
            submit.click(timeout=8000)
            _log("已点击「登录」按钮")
    except Exception as e:
        _log(f"点击登录按钮失败：{type(e).__name__}")
        try:
            submit.click(timeout=8000)
        except Exception:
            pass

    # 7) 等页面跳转 + cookie 落库（最多 30 秒）
    #    判据：拿到登录态 cookie（pjwt / session+smac）。
    #    「跳回首页」只作为提示信号，最终仍以登录态 cookie + 站内接口为准。
    deadline = time.time() + 30
    all_cookies = []
    cookie = ""
    auth_ok = False
    left_email_page = False
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        all_cookies = page.context.cookies()
        cookie = cookies_to_string(all_cookies)
        url_now = ""
        try:
            url_now = page.url or ""
        except Exception:
            pass
        if _auth_cookie_present(all_cookies):
            auth_ok = True
            break
        if url_now and "emailSignIn" not in url_now:
            left_email_page = True

    _log(f"邮箱验证后 cookie：{_cookie_names(all_cookies)}")
    _log(f"当前 URL：{page.url if hasattr(page, 'url') else '?'}")
    if auth_ok:
        # 再用一次真实接口确认（cookie 名可能变，接口不会骗人）
        verified = _verify_authenticated(page)
        if verified is False:
            _log("⚠️ cookie 看起来像登录态，但站内接口返回未登录 → 本次验证没生效")
        elif verified is True:
            _log("站内接口已确认：登录成功")
        else:
            _log("站内接口没能确认登录态（可能网络/风控），按 cookie 判定成功")
        _log("邮箱验证登录成功")
        _log(f"cookie 有效期：{describe_cookie_life(all_cookies)}")
        return _login_result(cookie, all_cookies, username)

    if left_email_page:
        _log("页面已离开 emailSignIn，但没抓到登录态 cookie，再探一次接口…")
        verified = _verify_authenticated(page)
        if verified is True:
            all_cookies = page.context.cookies()
            cookie = cookies_to_string(all_cookies)
            _log(f"接口确认已登录，cookie：{_cookie_names(all_cookies)}")
            return _login_result(cookie, all_cookies, username)

    # 没拿到登录态：把服务器原文打出来，看它到底说了什么
    api_log = _read_api_log(page)
    if api_log:
        _log("邮箱验证相关的接口响应：")
        for item in api_log[-6:]:
            _log(f"  ↳ {item.get('url')} status={item.get('status')} body={item.get('body')}")
    err = _read_login_error(page)
    if err:
        _log(f"邮箱验证未通过：{err}")
    _debug_dump(page, "email_verify_failed")
    return None


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
        # 注意：page.route 的 URL 匹配函数收到的参数是「URL 字符串」，不是 Request 对象。
        # 之前写成 lambda r: ... r.url 会 AttributeError，且该错误在事件监听器里抛出，
        # 连带把后续 context.cookies() 也搞挂。这里用 **/api/account/signIn 通配字符串。
        page.route(
            "**/api/account/signIn",
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
        _dbg(f"页面已跳转到 {url}（可能登录成功）")
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
    # 登录态相关的排前面，方便人眼核对
    ordered = []
    for k in ("pjwt", "session", "smac", "cf_clearance", "sessionid"):
        if k in picked:
            ordered.append(f"{k}={picked.pop(k)}")
    ordered += [f"{k}={v}" for k, v in picked.items()]
    return "; ".join(ordered)


def cookie_expiry_info(cookies) -> dict:
    """读出登录态 cookie 的到期时间，算出「还剩几天」。

    为什么需要这个：NodeSeek 的 session cookie 约 30 天过期（论坛实测
    「按日期看登录后一个月就得失效了」）。等它掉了才发现要重新登录，
    很可能撞上「设备记忆失效 → 又要邮箱验证码」。所以要提前知道还剩多久。

    Playwright 的 cookie 字典里 expires 是 Unix 时间戳（秒），-1 表示会话 cookie
    （浏览器关掉就没了）。

    返回结构（fields 是扁平映射，方便直接落成 meta.json）：
      {"session": {"expires": 1795448303.0, "days_left": 30.0,
                   "session_cookie": False}, ...}
    """
    out = {}
    now = time.time()
    for c in cookies or []:
        name = c.get("name")
        if not name or name not in AUTH_COOKIE_NAMES:
            continue
        exp = c.get("expires")
        info = {"expires": exp, "session_cookie": False, "days_left": None}
        if isinstance(exp, (int, float)) and exp > 0:
            info["days_left"] = round((exp - now) / 86400.0, 2)
        else:
            info["session_cookie"] = True
        out[name] = info
    return out


def describe_cookie_life(cookies) -> str:
    """一行话讲清楚 cookie 还能活多久（给日志看）。"""
    fields = cookie_expiry_info(cookies)
    if not fields:
        return "无登录态 cookie"
    days = [(i["days_left"], n) for n, i in fields.items() if i.get("days_left") is not None]
    if not days:
        return "全部为会话 cookie（关浏览器即失效）"
    d, name = min(days)
    if d <= 0:
        return f"{name} 已过期 {abs(d):.1f} 天"
    return f"{name} 还剩 {d:.1f} 天"


def _login_result(cookie: str, cookies: list, username: str) -> dict:
    """登录成功的统一出口。

    带上 cookie 的到期信息（expires / 还剩几天），让上层能判断
    「这次拿到的 cookie 还能用多久、要不要提前主动换新」，
    而不用等 cookie 掉了才知道要重新登录。
    """
    return {
        "cookie": cookie,
        "cookies": cookies,
        "username": username,
        "expiry": cookie_expiry_info(cookies),
        "cookie_life": describe_cookie_life(cookies),
    }


def _auth_cookie_present(cookies) -> bool:
    """判断这批 cookie 里有没有「已登录」的凭据。

    NodeSeek 登录成功后下发的关键 cookie：
      pjwt   —— 登录 JWT（payload 里带 id/name，油猴脚本就是靠它认账号）
      session —— 服务端会话
      smac  —— 会话签名
    其中 pjwt 最可靠（游客不会有带 id/name 的 JWT）。
    """
    names = {c.get("name") for c in cookies or []}
    if "pjwt" in names:
        return True
    # 兜底：session + smac 同时存在也算已登录
    return "session" in names and "smac" in names


def _verify_authenticated(page) -> Optional[bool]:
    """拿一次真实的站内请求确认登录态。

    返回 True=已登录，False=未登录，None=判断不了（网络/风控）。
    比「看 cookie 里有没有某个名字」可靠，因为站点随时可能改字段名。
    """
    try:
        res = page.evaluate(
            """async (api) => {
                try {
                    const r = await fetch(api, {
                        method: 'GET',
                        credentials: 'include',
                        headers: {'x-csrf-challenge': 'simple-token'}
                    });
                    const t = await r.text();
                    return {status: r.status, text: (t || '').slice(0, 400)};
                } catch (e) {
                    return {error: String(e && e.message ? e.message : e)};
                }
            }""",
            AUTH_PROBE_API,
        )
    except Exception as e:
        _log(f"登录态探测异常：{type(e).__name__}")
        return None
    if not res or res.get("error"):
        return None
    status = res.get("status")
    text = (res.get("text") or "")
    if status in (401, 403):
        return False
    if status == 200:
        # 登录用户会返回 credit 等业务数据；未登录时不会带 success 字段
        return True if ("success" in text or "credit" in text or "balance" in text) else None
    return None


def _install_api_logger(page):
    """给页面装一个「API 响应记录器」。

    用途：邮箱验证提交后，服务器到底回了什么（成功？验证码错误？过期？）
    只有看到原文才能确定。而 Camoufox（Firefox 内核）下 Playwright 读响应体
    会因 content-encoding: br 报 NS_ERROR_INVALID_CONTENT_ENCODING，
    但页面 JS 里的 resp.text() 用的是浏览器自己的解码器，能正常读到。
    """
    try:
        page.evaluate(
            """() => {
                if (window.__ns_api_log) return;
                window.__ns_api_log = [];
                const push = (url, status, body) => {
                    try {
                        const item = {
                            url: String(url).slice(0, 200),
                            status: status,
                            body: String(body || '').slice(0, 800)
                        };
                        window.__ns_api_log.push(item);
                        if (window.__ns_api_log.length > 50) window.__ns_api_log.shift();
                        // 同时写一份到 localStorage：页面一跳转 window 上的东西就没了，
                        // 但 localStorage 是同源持久化的，跨页面还能读到。
                        try {
                            localStorage.setItem('__ns_api_log', JSON.stringify(window.__ns_api_log));
                        } catch (e) {}
                    } catch (e) {}
                };
                try {
                    const origFetch = window.fetch.bind(window);
                    window.fetch = async function(input, init) {
                        const url = typeof input === 'string' ? input : (input && input.url) || '';
                        const resp = await origFetch(input, init);
                        if (url.indexOf('/api/') !== -1) {
                            try {
                                const clone = resp.clone();
                                const t = await clone.text();
                                push(url, resp.status, t);
                            } catch (e) {
                                push(url, resp.status, 'CLONE_ERR:' + (e && e.message ? e.message : e));
                            }
                        }
                        return resp;
                    };
                } catch (e) {}
                try {
                    const origOpen = XMLHttpRequest.prototype.open;
                    const origSend = XMLHttpRequest.prototype.send;
                    XMLHttpRequest.prototype.open = function(method, url) {
                        this.__ns_url = url;
                        return origOpen.apply(this, arguments);
                    };
                    XMLHttpRequest.prototype.send = function() {
                        const xhr = this;
                        if (xhr.__ns_url && String(xhr.__ns_url).indexOf('/api/') !== -1) {
                            xhr.addEventListener('load', function() {
                                let b = '';
                                try { b = xhr.responseText; } catch (e) { b = 'READ_ERR'; }
                                push(xhr.__ns_url, xhr.status, b);
                            });
                        }
                        return origSend.apply(this, arguments);
                    };
                } catch (e) {}
            }"""
        )
    except Exception as e:
        _log(f"API 记录器安装失败：{type(e).__name__}")


def _read_api_log(page) -> list:
    """读回 API 记录器收集到的响应列表。

    优先读 window（页面没跳转时）；页面跳走了就读 localStorage 里的副本。
    """
    try:
        log = page.evaluate("() => window.__ns_api_log || []")
        if isinstance(log, list) and log:
            return log
    except Exception:
        pass
    try:
        raw = page.evaluate("() => localStorage.getItem('__ns_api_log')")
        if raw:
            import json as _json
            log = _json.loads(raw)
            if isinstance(log, list):
                return log
    except Exception:
        pass
    return []



# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------
def _dump_form_structure(page):
    """dump 登录页所有 input 元素 + 所有链接（a 的 href/文字），
    用于判断站点是否改版、登录标识是「用户名」还是「邮箱」、有没有多 tab / 其他登录入口。"""
    try:
        info = page.evaluate(
            """() => {
                const out = [];
                for (const el of document.querySelectorAll('input')) {
                    const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    out.push({
                        id: el.id || '',
                        name: el.name || '',
                        type: el.type || '',
                        placeholder: (el.placeholder || '').slice(0, 30),
                        autocomplete: el.getAttribute('autocomplete') || '',
                        visible,
                    });
                }
                // 所有链接：href + 文字，重点看有没有「邮箱验证码登录 / 验证码登录 / 密码登录」等入口
                const links = [];
                for (const el of document.querySelectorAll('a')) {
                    const t = (el.innerText || '').trim();
                    const h = el.getAttribute('href') || '';
                    if (t || h) links.push({ text: t.slice(0, 30), href: h.slice(0, 80) });
                }
                // 所有按钮文字
                const btns = [];
                for (const el of document.querySelectorAll('button')) {
                    const t = (el.innerText || '').trim();
                    if (t) btns.push(t.slice(0, 30));
                }
                return { inputs: out, links: links.slice(0, 30), btns: Array.from(new Set(btns)).slice(0, 20) };
            }"""
        )
        if info:
            _dbg(f"登录页输入框：{info.get('inputs')}")
            _dbg(f"登录页链接：{info.get('links')}")
            _dbg(f"登录页按钮：{info.get('btns')}")
    except Exception as e:
        _dbg(f"表单结构 dump 失败：{type(e).__name__}")


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
    _dump_form_structure(page)

    # 登录表单可能是异步渲染的，dump 之后再多等一会儿，让表单真正加载出来
    page.wait_for_timeout(3000)
    _dbg("等待登录表单渲染后再次 dump…")
    _dump_form_structure(page)

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

    # 诊断：打印匹配到的用户名/密码框的真实属性，确认没有误匹配到搜索框
    try:
        _dbg(f"匹配到的用户名框：{user_input.evaluate('el => ({id: el.id, name: el.name, type: el.type, ph: el.placeholder})')}")
    except Exception:
        pass

    last_msg = ""
    for attempt in range(1, max_attempts + 1):
        _log(f"第 {attempt}/{max_attempts} 次尝试登录：{username}")
        _dismiss_dialog(page)

        pwd_input = _first_visible(page, PASSWORD_SELECTORS, timeout=5000)
        if pwd_input is None:
            raise LoginError("PAGE_ERROR", "登录页找不到密码输入框")
        try:
            _dbg(f"匹配到的密码框：{pwd_input.evaluate('el => ({id: el.id, name: el.name, type: el.type, ph: el.placeholder})')}")
        except Exception:
            pass

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

        # 关键修复：不再依赖「点击 submit → expect_response 读响应体」。
        # 根因（2026-10 实测）：登录响应是 content-encoding: br（Brotli），
        # Camoufox/Firefox 内核下 Playwright 读 body 报 NS_ERROR_INVALID_CONTENT_ENCODING，
        # resp.body() 直接 Error，导致永远只能看到「HTTP 200」而不知道失败原因。
        # 改法：在页面 JS 上下文里自己 fetch 登录，用 resp.text() 在 JS 层读 body
        # （浏览器自己的解码器能正确解 br），把 status + body 原文带回 Python。
        result = _signin_via_page_fetch(page, username, password)
        if result is None:
            # 主动 fetch 失败（可能前端有额外 header 逻辑），回退到点击 submit
            _log("主动 fetch 登录失败，回退到点击登录按钮…")
            if submit is None:
                raise LoginError("PAGE_ERROR", "找不到登录按钮")
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
            page.wait_for_timeout(2500)
        else:
            resp = None

        # 登录接口成功与否，不靠读响应体判断——登录成功后页面会立刻跳转，
        # 响应体随即被浏览器丢弃（Camoufox/Firefox 下读 body 会报
        # NS_ERROR_INVALID_CONTENT_ENCODING）。真正的判据是登录态 cookie
        # （pjwt / session / smac）有没有出现。
        page.wait_for_timeout(2500)
        all_cookies = page.context.cookies()
        cookie = cookies_to_string(all_cookies)
        _log(f"提交后 cookie：{_cookie_names(all_cookies)}")
        if _auth_cookie_present(all_cookies):
            verified = _verify_authenticated(page)
            if verified is False:
                _log("⚠️ 有 cookie 但站内接口判定未登录，按未登录处理")
            else:
                _log("登录成功（已拿到登录态 cookie）")
                return _login_result(cookie, all_cookies, username)

        # 没有 sessionid：优先用主动 fetch 拿到的响应体，其次读拦截器，最后读网络层响应
        status = None
        data = None
        if result and isinstance(result, dict):
            status = result.get("status")
            body_text = result.get("body") or ""
            if body_text:
                try:
                    data = json.loads(body_text)
                except Exception:
                    data = body_text
            _log(f"主动 fetch 响应：status={status} body={body_text[:500]}")

        if data is None:
            debug = _read_signin_debug(page)
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
                    data = raw.decode("utf-8", errors="replace")[:300]
                    _log(f"响应体非 JSON：{data}")
            try:
                _log(f"响应头：content-type={resp.headers.get('content-type')} "
                     f"content-encoding={resp.headers.get('content-encoding')} "
                     f"set-cookie={resp.headers.get('set-cookie')}")
            except Exception:
                pass

        # 接口说成功、也没要求二次验证，但 cookie 里还没看到登录态
        # → 有些站点是「页面跳走时」才下发 cookie，这里主动刷一次首页再取
        if (data and isinstance(data, dict) and data.get("success")
                and not data.get("redirect") and not _auth_cookie_present(all_cookies)):
            _log("接口返回成功但暂未看到登录态 cookie，刷新首页再取一次…")
            try:
                page.goto(ORIGIN + "/", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                all_cookies = page.context.cookies()
                cookie = cookies_to_string(all_cookies)
                _log(f"刷新后 cookie：{_cookie_names(all_cookies)}")
                if _auth_cookie_present(all_cookies):
                    _log("登录成功（刷新后拿到登录态 cookie）")
                    return _login_result(cookie, all_cookies, username)
            except Exception as e:
                _log(f"刷新取 cookie 失败：{type(e).__name__}")

        # 邮箱二次验证：密码登录成功（success:true）但返回 redirect 到 emailSignIn/smsSignIn，
        # 说明账号或「新设备」需要邮箱验证码二次确认。这是登录流程的一部分，不是失败。
        if data and isinstance(data, dict) and data.get("success") and data.get("redirect"):
            redirect = data.get("redirect") or ""
            # 诊断：这次要码，是「这台设备第一次登录」还是「设备记忆失效了」？
            # 依据 = profile 里之前有没有留过 NodeSeek 的登录态痕迹
            #   · 有痕迹 → 之前登录过，这次仍要码 → 设备记忆可能过期了（异常，要告警）
            #   · 没痕迹 → 首次登录，正常流程，本来就该要码
            prior = _prior_login_traces(page)
            if prior:
                _log(f"⚠️ 这台设备之前登录过（profile 里有 {prior}），本次仍要求邮箱验证")
                _log("   → 说明 NodeSeek 侧的「记住设备」状态可能已过期，"
                     "或换了出口 IP / 换了浏览器指纹")
                device_memory_expired = True
            else:
                _log("首次在新设备上登录，需要邮箱验证码属正常流程")
                device_memory_expired = False

            _log(f"密码验证通过，但需要二次验证，redirect={redirect}")
            email_verify = _handle_email_verify(page, redirect, username)
            if email_verify:
                email_verify["device_memory_expired"] = device_memory_expired
                if device_memory_expired:
                    _log("（提醒：本次重新做了邮箱验证，说明「记住设备」没能免掉这次验证）")
                return email_verify
            # 邮箱验证没自动完成，抛出明确错误（带 redirect 信息供下一步逆向）
            raise LoginError(
                "NEED_EMAIL_VERIFY",
                f"密码已通过但需邮箱验证码，redirect={redirect}"
                + ("，且设备记忆疑似已过期" if device_memory_expired else ""),
                device_memory_expired=device_memory_expired,
            )

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
            if _auth_cookie_present(page.context.cookies()):
                _log("2FA 登录成功")
                return _login_result(cookie, page.context.cookies(), username)
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

        # 关键诊断：如果跳到了 emailSignIn.html（邮箱验证码登录页），
        # dump 那个页面的结构，看它到底是什么、验证码框和接口在哪。
        cur_url = ""
        try:
            cur_url = page.url or ""
        except Exception:
            pass
        if "emailSignIn" in cur_url or "email" in cur_url.lower():
            _dbg("检测到跳转到邮箱登录页，dump 该页结构：")
            _dump_form_structure(page)
            _debug_dump(page, "email_signin")

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
