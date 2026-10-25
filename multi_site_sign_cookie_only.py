# -*- coding: utf-8 -*-

import os
import time
import json
import random
import re
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# === 核心库加载 ===
try:
    from curl_cffi import requests
    print("成功加载 curl_cffi 模块 (v3)")
except ImportError:
    print("【严重警告】未安装 curl_cffi 模块！")
    import requests

# 验证码解决器
try:
    from turnstile_solver import TurnstileSolver
except ImportError:
    print("警告：验证码解决器模块未找到")
    TurnstileSolver = None

# Playwright 浏览器登录
try:
    from browser_login import browser_login, browser_login_detail, browser_sign, PLAYWRIGHT_AVAILABLE
    if PLAYWRIGHT_AVAILABLE:
        print("成功加载 Playwright 浏览器登录模块")
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    browser_login = None
    browser_login_detail = None
    browser_sign = None

# 环境变量（显式指向脚本同目录的 .env，避免 cwd 不同读不到）
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_ENV_FILE):
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_FILE, override=False)
    except ImportError:
        print("!! 检测到 .env，但没装 python-dotenv，里面的配置不会生效")
        print("   修复：.venv/bin/pip install python-dotenv")
        print("   （或者直接把变量 export 到环境里）")

# === 通知模块 ===
hadsend = False
send = None
try:
    from notify import send
    hadsend = True
except ImportError:
    print("未加载通知模块")

# === 站点配置 ===
SITES_CONFIG = {
    "nodeseek": {
        "name": "NodeSeek",
        "type": "nodeseek",
        "sign_api": "https://www.nodeseek.com/api/attendance",
        "stats_api": "https://www.nodeseek.com/api/account/credit/page-",
        "board_url": "https://www.nodeseek.com/board",
        "origin": "https://www.nodeseek.com",
        "cookie_var": "NS_COOKIE",
        "login_url": "https://www.nodeseek.com/signIn.html",
        "login_api": "https://www.nodeseek.com/api/account/signIn",
        "sitekey": "0x4AAAAAAAaNy7leGjewpVyR",
        "user_var": "NS_USER",
        "pass_var": "NS_PASS",
        "account_var": "NS_USER_PASS"
    },
}

# === Refract 签名配置 ===
REFRACT_KEY = "CHICZkKViFoZmVbIH1Y6"
REFRACT_VERSION = "0.3.34"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
IMPERSONATE_VER = "chrome124"


def compute_refract_sign(method, url, user_agent, body, refract_key):
    """计算 refract-sign: SHA-1(method\\n\\nurl\\n\\nua\\n\\nbody\\n\\nkey)"""
    sign_str = f"{method}\n\n{url}\n\n{user_agent}\n\n{body}\n\n{refract_key}"
    return hashlib.sha1(sign_str.encode()).hexdigest()


def make_refract_headers(method, url, body="", refract_key=None):
    """生成 refract-sign / refract-version / refract-key 请求头"""
    key = refract_key or REFRACT_KEY
    sign = compute_refract_sign(method, url, BROWSER_UA, body, key)
    return {
        "refract-sign": sign,
        "refract-version": REFRACT_VERSION,
        "refract-key": key,
    }


# === 通知状态管理 ===
NOTIFICATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "cookie", "notification_status.json")


def load_notification_status():
    try:
        if os.path.exists(NOTIFICATION_FILE):
            with open(NOTIFICATION_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except: pass
    return {}


def save_notification_status(status):
    try:
        os.makedirs(os.path.dirname(NOTIFICATION_FILE), exist_ok=True)
        with open(NOTIFICATION_FILE, 'w', encoding='utf-8') as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存通知状态失败: {e}")


def should_send_notification(site_name):
    """检查是否应该发送通知（每天只发送一次）"""
    status = load_notification_status()
    today = datetime.now().strftime('%Y-%m-%d')
    site_status = status.get(site_name, {})
    last_sent = site_status.get('last_sent_date')
    return last_sent != today


def mark_notification_sent(site_name):
    """标记通知已发送"""
    status = load_notification_status()
    today = datetime.now().strftime('%Y-%m-%d')
    if site_name not in status: status[site_name] = {}
    status[site_name]['last_sent_date'] = today
    save_notification_status(status)


# === Cookie 文件操作 ===
_COOKIE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie")


def get_cookie_file_path(site_name, account_index=None):
    """按脚本所在目录定位，避免青龙里 cwd 不同导致读写到别处。"""
    if account_index is not None:
        name = f"{site_name.upper()}_COOKIE_{account_index}.txt"
    else:
        name = f"{site_name.upper()}_COOKIE.txt"
    return os.path.join(_COOKIE_DIR, name)


def load_cookies_from_file(site_name, account_index=None):
    try:
        path = get_cookie_file_path(site_name, account_index)
        if os.path.exists(path):
            with open(path, "r", encoding='utf-8') as f:
                content = f.read().strip()
                if content: return content
    except: pass
    return ""


# === Cookie 到期元数据 ===
#
# 为什么需要：NodeSeek 的登录 cookie 约 30 天过期。等它掉了才发现要重登，
# 很可能撞上「设备记忆也过期」→ 又要人工收邮箱验证码。
# 所以登录成功时把到期时间记下来，快到期就主动换新，把风险窗口前移。
_META_SUFFIX = ".meta.json"


def get_cookie_meta_path(site_name, account_index=None):
    path = get_cookie_file_path(site_name, account_index)
    return os.path.splitext(path)[0] + _META_SUFFIX


def save_cookie_meta(site_name, account_index, meta):
    """记录 cookie 的到期信息。"""
    try:
        path = get_cookie_meta_path(site_name, account_index)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存cookie元数据失败: {e}")


def load_cookie_meta(site_name, account_index=None):
    try:
        path = get_cookie_meta_path(site_name, account_index)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def cookie_days_left(meta) -> float:
    """从元数据算出 cookie 还剩几天。返回 None 表示不知道（没记录过）。"""
    if not meta:
        return None
    exp = meta.get("expiry", {}) if isinstance(meta.get("expiry"), dict) else {}
    soonest = None
    for name, info in exp.items():
        if not isinstance(info, dict):
            continue
        d = info.get("days_left")
        if d is None:
            continue
        if soonest is None or d < soonest:
            soonest = d
    if soonest is None:
        return None
    # 元数据是登录当时写的，隔了几天要扣掉
    age = 0.0
    ts = meta.get("ts")
    if isinstance(ts, (int, float)):
        age = max(0.0, (time.time() - ts) / 86400.0)
    return soonest - age


def should_renew_cookie(meta, threshold_days: float) -> bool:
    """是否该提前换新 cookie。

    规则（任一命中就换）：
      · 剩不到 threshold_days 天      —— 快掉了，提前换
      · 已经过期                       —— 肯定换
      · 元数据显示是会话 cookie（关浏览器即失效）—— 换
    元数据里没记录到期时间 → 不主动换（避免无谓的登录次数，
    登录本身有风控成本，多次登录会被 RATE_LIMITED）。
    """
    if not meta:
        return False
    exp = meta.get("expiry", {}) if isinstance(meta.get("expiry"), dict) else {}
    if not exp:
        return False
    # 全是会话 cookie → 每次跑都得重新登录，不如主动换
    if all(isinstance(v, dict) and v.get("session_cookie") for v in exp.values()):
        return True
    d = cookie_days_left(meta)
    if d is None:
        return False
    return d <= threshold_days


def save_cookie_to_file(site_name, cookie_str, account_index=None):
    try:
        path = get_cookie_file_path(site_name, account_index)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding='utf-8') as f:
            f.write(cookie_str)
        print(f"Cookie已保存: {path}")
        return True
    except Exception as e:
        print(f"保存Cookie失败: {e}")
        return False


def split_env_items(value):
    return [p.strip() for p in value.split("&") if p.strip()]


def get_env_cookie(site_config, account_index):
    cookies = split_env_items(os.getenv(site_config["cookie_var"], ""))
    idx = account_index - 1
    if 0 <= idx < len(cookies) and "=" in cookies[idx]:
        return cookies[idx]
    return ""


def looks_like_cookie(value):
    return "=" in value and ";" in value


def get_accounts(site_config):
    """读取账号密码。优先使用 *_USER_PASS，其次兼容 NS_USER/NS_PASS，最后兼容旧的 NS_COOKIE=用户&密码。"""
    account_var = site_config.get("account_var")
    account_parts = split_env_items(os.getenv(account_var, "")) if account_var else []

    if not account_parts:
        users = split_env_items(os.getenv(site_config.get("user_var", ""), ""))
        pwds = split_env_items(os.getenv(site_config.get("pass_var", ""), ""))
        account_parts = []
        for i, user in enumerate(users):
            account_parts.append(user)
            if i < len(pwds):
                account_parts.append(pwds[i])

    if not account_parts:
        legacy_parts = split_env_items(os.getenv(site_config["cookie_var"], ""))
        if legacy_parts and not any(looks_like_cookie(p) for p in legacy_parts):
            print(f"提示：检测到 {site_config['cookie_var']} 可能仍在使用旧格式 用户&密码，建议迁移到 {account_var}")
            account_parts = legacy_parts

    accounts = []
    for i in range(0, len(account_parts), 2):
        user = account_parts[i]
        pwd = account_parts[i + 1] if i + 1 < len(account_parts) else None
        accounts.append({"user": user, "pwd": pwd})
    return accounts


# === 核心业务逻辑 ===
def create_session(cookie_str=None):
    """创建一个预配置的 Session 对象"""
    session = requests.Session()
    session.headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie_str:
        for cookie in re.split(r";\s*", cookie_str):
            if '=' in cookie:
                k, v = cookie.split('=', 1)
                session.cookies.set(k.strip(), v.strip())
    return session


def cookie_dict_to_str(cookie_dict):
    return "; ".join([f"{k}={v}" for k, v in cookie_dict.items() if v is not None])


def response_looks_like_waf(response):
    text = (response.text or "").lower()
    return any(s in text for s in [
        "just a moment", "cf-chl", "cloudflare", "challenge-platform", "turnstile"
    ])


def check_cookie_validity(site_config, cookie_str, retries=3):
    """返回 True=有效，False=明确无效，None=网络/WAF等不确定状态。"""
    last_reason = ""
    for attempt in range(1, retries + 1):
        try:
            session = create_session(cookie_str)
            session.get(
                site_config['board_url'],
                headers={"Referer": site_config["origin"]},
                impersonate="chrome124",
                timeout=15
            )
            response = session.get(
                f"{site_config['stats_api']}1",
                headers={"Referer": site_config["board_url"], "Origin": site_config["origin"]},
                impersonate="chrome124",
                timeout=15
            )

            if response_looks_like_waf(response):
                last_reason = "疑似 WAF/Cloudflare 页面"
                print(f"Cookie校验第{attempt}次遇到{last_reason}，暂不判定Cookie失效")
                time.sleep(random.uniform(2, 4))
                continue

            if response.status_code == 200:
                try:
                    data = response.json()
                    if data.get("success") is not None:
                        return True
                except: pass
                text = response.text.lower()
                if "credit" in text or "balance" in text or "success" in text:
                    return True
                last_reason = "200但响应内容不匹配"
            elif response.status_code in (401, 403, 404, 500):
                print(f"Cookie校验返回 {response.status_code}，判定Cookie无效")
                return False
            else:
                last_reason = f"HTTP {response.status_code}"
                print(f"Cookie校验第{attempt}次失败: {last_reason}")
        except Exception as e:
            last_reason = str(e)
            print(f"Cookie校验第{attempt}次异常: {last_reason}")

        if attempt < retries:
            time.sleep(random.uniform(2, 4))

    print(f"Cookie校验多次失败但无法确认失效: {last_reason}")
    return None


def auto_login(site_config, username, password):
    try:
        if not username or not password:
            print("未配置账号或密码，无法自动登录")
            return None
        if TurnstileSolver is None:
            print("验证码解决器不可用，无法自动登录")
            return None

        api_key = os.getenv("CLOUDFREED_API_KEY", "")
        base_url = os.getenv("CLOUDFREED_BASE_URL", "http://localhost:3000")
        if not api_key:
            print("已开启 ALLOW_CLOUDFREED，但未配置 CLOUDFREED_API_KEY，跳过")
            return None

        solver = TurnstileSolver(api_base_url=base_url, client_key=api_key)
        session = create_session()

        # 1. 访问登录页
        login_url = site_config["login_url"]
        login_headers = {"Referer": site_config["origin"]}
        login_headers.update(make_refract_headers("GET", login_url))
        session.get(login_url, headers=login_headers, impersonate=IMPERSONATE_VER, timeout=15)

        # 2. 解决验证码
        print("正在解决验证码...")
        token = solver.solve(
            login_url,
            site_config["sitekey"],
            user_agent=BROWSER_UA,
            verbose=False
        )
        if not token:
            print("验证码失败")
            return None

        # 3. 登录请求（新版接口：token 走 header，body 只留账号密码）
        login_data = {
            "username": username,
            "password": password,
        }

        login_body = json.dumps(login_data)
        login_api = site_config["login_api"]
        headers = {
            "Origin": site_config["origin"],
            "Referer": login_url,
            "Content-Type": "application/json",
            "x-captcha-token": token,
            "x-captcha-source": "turnstile",
        }
        # 新版接口还会校验这两个头；没有的话服务端可能直接判定为脚本。
        # 可以从浏览器 F12 里抄一次（localStorage.security_token / fp visitorId），
        # 但更稳的做法是用家宽浏览器 agent（本脚本已优先走这条路）。
        if os.getenv("NS_SECURITY_TOKEN"):
            headers["x-security-token"] = os.getenv("NS_SECURITY_TOKEN", "")
        if os.getenv("NS_INTEGRITY_TOKEN"):
            headers["x-integrity-token"] = os.getenv("NS_INTEGRITY_TOKEN", "")
        headers.update(make_refract_headers("POST", login_api, body=login_body))

        resp = session.post(
            login_api,
            json=login_data,
            headers=headers,
            impersonate=IMPERSONATE_VER,
            timeout=15
        )

        if resp.status_code == 200:
            cookie_str = cookie_dict_to_str(session.cookies.get_dict())
            if cookie_str:
                print("登录成功")
                return cookie_str
            print("登录返回200，但未获取到Cookie")
            return None

        print(f"登录失败: {resp.status_code}")
        print(f"登录响应: {(resp.text or '')[:500]}")
        if resp.status_code in (403, 429):
            print("提示：这是 Cloudflare / 风控拦截。纯 HTTP 登录已被论坛前端加固，"
                  "请部署家宽浏览器 agent 并配置 LOGIN_AGENT_URL（见 部署指南_账号密码自动登录.md）")
        return None

    except Exception as e:
        print(f"登录异常: {e}")
        return None


def sign(cookie, site_config, ns_random):
    if not cookie: return "fail", "无Cookie"

    try:
        session = create_session(cookie)

        # 1. 模拟浏览 (GET) - 预热
        session.get(
            site_config["board_url"],
            headers={"Referer": site_config["origin"]},
            impersonate="chrome124",
            timeout=15
        )

        time.sleep(random.uniform(2, 4))

        # 2. 签到 (POST)
        url = f"{site_config['sign_api']}?random={ns_random}"
        headers = {
            'Origin': site_config["origin"],
            'Referer': site_config["board_url"],
            'Content-Type': 'application/json',
            # 站点的状态变更请求都带这个头，补上更贴近真实前端
            'x-csrf-challenge': 'simple-token',
        }

        resp = session.post(url, headers=headers, impersonate="chrome124", timeout=15)

        try:
            data = resp.json()
        except:
            if response_looks_like_waf(resp):
                return "fail", "被WAF拦截"
            return "fail", f"未知响应 (Code {resp.status_code})"

        msg = data.get("message", "")
        if "鸡腿" in msg or data.get("success"): return "success", msg
        elif "已完成签到" in msg: return "already", msg
        elif data.get("status") == 404: return "invalid", msg
        return "fail", msg

    except Exception as e:
        return "error", str(e)


def get_stats(cookie, site_config, days=30):
    if not cookie: return None
    try:
        session = create_session(cookie)

        # 统计接口也需要预热访问
        session.get(
            site_config["board_url"],
            headers={"Referer": site_config["origin"]},
            impersonate="chrome124",
            timeout=15
        )

        shanghai_tz = ZoneInfo("Asia/Shanghai")
        now = datetime.now(shanghai_tz)
        start_time = now - timedelta(days=days)

        all_records = []
        page = 1

        while page <= 10:
            url = f"{site_config['stats_api']}{page}"
            headers = {
                "Referer": site_config["board_url"],
                "Origin": site_config["origin"]
            }
            resp = session.get(url, headers=headers, impersonate="chrome124", timeout=10)
            try:
                data = resp.json()
            except: break

            if not data.get("success") or not data.get("data"): break
            records = data.get("data", [])
            if not records: break

            all_records.extend(records)
            page += 1
            time.sleep(0.5)

        valid_records = []
        for r in all_records:
            try:
                amt, _, desc, ts = r
                dt = datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(shanghai_tz)
                if dt >= start_time and ("签到" in desc or "鸡腿" in desc):
                    valid_records.append({'amount': amt, 'date': dt.strftime('%Y-%m-%d')})
            except: continue

        total = sum(r['amount'] for r in valid_records)
        count = len(valid_records)
        avg = round(total/count, 2) if count > 0 else 0

        return {'total': total, 'avg': avg, 'days': count, 'period': f"近{days}天"}
    except: return None


# === 主逻辑 ===
def load_account_cookie(site_name, site_config, account_index):
    """优先使用环境变量中的手动 Cookie，避免本地旧 Cookie 文件覆盖用户最新配置。"""
    return get_env_cookie(site_config, account_index) or load_cookies_from_file(site_name, account_index)


def recover_cookie(site_name, site_config, account_index, user, pwd):
    """恢复登录。默认只用浏览器（本机或家宽 agent）。

    2026-10 起 NodeSeek 的 /api/account/signIn 需要
    x-captcha-token(Turnstile) + x-integrity-token(FingerprintJS) + x-security-token，
    后两者只能由真浏览器产生，CloudFreed 打码已经解决不了问题，所以它只作为
    显式开启的兜底（ALLOW_CLOUDFREED=true），默认完全不碰。

    返回 (cookie, meta)：meta 里带 cookie 的到期信息，
    用于「主动续期」——快到期时提前换新，别等 cookie 掉了才发现。
    """
    meta = {}

    # 这台设备（这个 profile + 这个出口 IP）之前**登录成功过**吗？
    # 记在 cookie meta 里（比浏览器 cookie 更持久，浏览器清了也还在）。
    #
    # 这是判断「设备记忆是否真的失效」的权威依据：
    #   曾登录成功过 + 本次仍要邮箱码 → 设备记忆确实失效 → 该告警
    #   从没登录成功过              → 首次在新设备上登录，正常流程 → 不告警
    #
    # 2026-10-25 修正：之前拿 profile 里的 cf_clearance/fog 当「登录过」的证据，
    # 那是 Cloudflare 与指纹服务的 cookie，第一次访问就会留，
    # 结果每个新机器第一次登录都误报「设备记忆失效」。
    prev_meta = load_cookie_meta(site_name, account_index) or {}
    prior_logged_in = bool(prev_meta.get("device_logged_in"))

    if PLAYWRIGHT_AVAILABLE and browser_login_detail:
        print("尝试浏览器登录（本地 / 家宽 agent）...")
        r = browser_login_detail(site_config, user, pwd, headless=True)
        cookie = (r or {}).get("cookie")
        # 本次是否被要求了邮箱二次验证
        needs_code = bool((r or {}).get("needs_email_verify"))
        # login_core 的判定：本次要码 且 profile 里有 NodeSeek 登录态痕迹
        dme = bool((r or {}).get("device_memory_expired")) and needs_code
        # 历史判定：这台设备曾登录成功过，这次仍要码 → 记忆确实失效
        if needs_code and prior_logged_in:
            dme = True
        if cookie:
            save_cookie_to_file(site_name, cookie, account_index)
            meta = {
                # ts 必填：cookie_days_left() 用它扣掉「距上次登录过了多少天」
                "ts": time.time(),
                "expiry": r.get("expiry"),
                "cookie_life": r.get("cookie_life"),
                "device_memory_expired": dme,
                # 登录成功 → 记下「这台设备登录成功过」，下次据此判断设备记忆
                "device_logged_in": True,
                "needs_email_verify": needs_code,
            }
            save_cookie_meta(site_name, account_index, meta)
            if needs_code:
                print(f"本次走了邮箱二次验证（这台设备曾登录成功过：{prior_logged_in}）")
            return cookie, meta
        print("浏览器登录未成功")
        if dme:
            meta["device_memory_expired"] = True

    if os.getenv("ALLOW_CLOUDFREED", "").strip().lower() in ("1", "true", "yes"):
        print("尝试传统 API 登录（CloudFreed，已显式开启）...")
        cookie = auto_login(site_config, user, pwd)
        if cookie:
            save_cookie_to_file(site_name, cookie, account_index)
            return cookie, meta

    return None, meta


def sign_failure_should_recover(status, msg):
    """签到结果明确说明当前Cookie不可用时，触发登录恢复并重试一次。WAF不代表Cookie失效。"""
    text = (msg or "").lower()
    if status in ["invalid", "error"]:
        return True
    return any(s in text for s in [
        "user not found",
        "unauthorized",
        "forbidden",
        "无cookie",
        "登录",
        "cookie"
    ])


def process_site(site_name, site_config, ns_random):
    print(f"\n{'='*30}\n处理站点: {site_config['name']}\n{'='*30}")

    accounts = get_accounts(site_config)
    env_cookies = split_env_items(os.getenv(site_config["cookie_var"], ""))
    account_count = max(len(accounts), len([c for c in env_cookies if "=" in c]))

    if account_count == 0:
        print(f"未配置 {site_config.get('account_var')} / {site_config['cookie_var']}，跳过")
        return

    site_results = []

    for i in range(account_count):
        idx = i + 1
        account = accounts[i] if i < len(accounts) else {}
        user = account.get("user") or f"账号{idx}"
        pwd = account.get("pwd")

        print(f"\n--- 账号: {user} ---")

        cookie = load_account_cookie(site_name, site_config, idx)
        meta = load_cookie_meta(site_name, idx)
        renew_threshold = float(os.getenv("NS_RENEW_BEFORE_DAYS", "5") or 5)
        device_memory_expired = False
        email_verify_used = False

        # ---- 主动续期 ----
        # NodeSeek 的登录 cookie 约 30 天过期。等它掉了才重登，很可能撞上
        # 「设备记忆也过期」→ 又要人工收邮箱验证码。
        # 所以：剩余寿命不足阈值时，提前主动换新一次。
        # 换新用的还是同一个持久化 profile，NodeSeek 记得这台设备，
        # 所以这一步不需要邮箱验证码。
        if cookie and meta:
            days = cookie_days_left(meta)
            if days is not None:
                print(f"Cookie 剩余寿命：{days:.1f} 天")
                if should_renew_cookie(meta, renew_threshold):
                    print(f"Cookie 不足 {renew_threshold} 天，主动续期…")
                    new_cookie, new_meta = recover_cookie(site_name, site_config, idx, user, pwd)
                    if new_cookie:
                        cookie = new_cookie
                        meta = new_meta or meta
                        # 主动续期同样可能撞上「设备记忆失效」，
                        # 这里要同步状态，否则告警会漏。
                        device_memory_expired = bool(meta.get("device_memory_expired"))
                        email_verify_used = bool(meta.get("needs_email_verify"))
                        print("主动续期完成")
                    else:
                        print("主动续期失败，继续用现有 Cookie 尝试")

        if cookie:
            check_result = check_cookie_validity(site_config, cookie)
            if check_result is False:
                print("Cookie已确认失效，尝试恢复登录...")
                cookie = None
            elif check_result is None:
                print("Cookie状态不确定，继续尝试使用现有Cookie签到")
            else:
                print("Cookie有效")
        else:
            print("无可用Cookie，尝试登录...")

        if not cookie:
            cookie, meta = recover_cookie(site_name, site_config, idx, user, pwd)
            device_memory_expired = bool(meta.get("device_memory_expired"))
            email_verify_used = bool(meta.get("needs_email_verify"))

        if not cookie:
            print("放弃: 无法获取有效Cookie")
            msg = "登录失败"
            if device_memory_expired:
                msg = ("登录失败：NodeSeek「记住设备」疑似已失效，"
                       "这次仍要求邮箱验证码。请手动跑一次登录完成验证")
            site_results.append({
                'user': user,
                'status': 'fail',
                'msg': msg,
                'device_memory_expired': device_memory_expired,
                'email_verify_used': email_verify_used,
            })
            continue

        status, msg = sign(cookie, site_config, ns_random)
        print(f"签到结果: {msg}")

        if sign_failure_should_recover(status, msg):
            print("签到失败疑似Cookie不可用，尝试恢复登录后重试一次...")
            new_cookie, new_meta = recover_cookie(site_name, site_config, idx, user, pwd)
            if new_cookie:
                cookie = new_cookie
                device_memory_expired = bool(new_meta.get("device_memory_expired"))
                email_verify_used = bool(new_meta.get("needs_email_verify"))
                status, msg = sign(cookie, site_config, ns_random)
                print(f"重试签到结果: {msg}")
            else:
                print("恢复登录失败，无法重试签到")

        stats = None
        if status in ["success", "already"]:
            stats = get_stats(cookie, site_config)
            if stats:
                print(f"统计: {stats['days']}天 | 总收益:{stats['total']} | 平均:{stats['avg']}")

        site_results.append({
            'user': user,
            'status': status,
            'msg': msg,
            'stats': stats,
            'device_memory_expired': device_memory_expired,
            'email_verify_used': email_verify_used,
        })

    # === 设备记忆失效告警 ===
    # 这是本项目最需要「立刻知道」的事件：
    #   这台设备之前登录过 → NodeSeek 却仍要求邮箱验证码 → 说明「记住设备」失效了。
    # 如果不告警，你可能连续几天签到失败都不知道原因。
    # 用独立通道推送，不受「今日已通知过」的限制。
    dme = [r for r in site_results if r.get('device_memory_expired')]
    if dme:
        lines = "\n".join(
            f"· {r['user']}：本次{'走了邮箱二次验证' if r.get('email_verify_used') else '仍被要求邮箱验证码'}"
            f"（签到结果：{r['msg']}）"
            for r in dme
        )
        alert = (
            "⚠️ NodeSeek「记住设备」疑似已失效\n"
            f"{lines}\n\n"
            "这台设备之前**登录成功过**，这次却仍被要求邮箱验证码。\n"
            "含义：NodeSeek 侧的设备记忆可能已过期，或出口 IP / 浏览器指纹变了。\n"
            "      后果是以后每次 cookie 失效（约 30 天）都要人工收一次码。\n\n"
            "处理：\n"
            "1) 手动跑一次登录完成邮箱验证，让它重新记住这台设备\n"
            "   cd /root/nodeseek-autosign && .venv/bin/python multi_site_sign_cookie_only.py\n"
            "2) 想要真正无人值守 → 配置自动收码（见部署指南「让验证码也自动化」）\n"
            "3) 若怀疑是 IP/指纹变了 → 检查 AGENT_PROXY 是否变动"
        )
        print("⚠️ 检测到设备记忆失效，正在告警…")
        try:
            send("⚠️ NodeSeek 设备记忆失效", alert)
        except Exception as e:
            print(f"告警推送失败: {e}")

    # 发送通知
    if hadsend and should_send_notification(site_name):
        msg_lines = []
        for r in site_results:
            line = f"{r['user']}: {r['msg']}"
            if r.get('stats'):
                line += f" (近30天: {r['stats']['total']}个)"
            msg_lines.append(line)

        if msg_lines:
            notify_content = f"{site_config['name']} 汇总\n" + "\n".join(msg_lines)
            send(f"{site_config['name']} 签到", notify_content)
            mark_notification_sent(site_name)
            print("通知已尝试推送")
    else:
        print("今日已通知过，跳过推送")


if __name__ == "__main__":
    print("脚本启动...")
    ns_random = os.getenv("NS_RANDOM", "true")
    for name, config in SITES_CONFIG.items():
        try: process_site(name, config, ns_random)
        except Exception as e: print(f"站点异常: {e}")
