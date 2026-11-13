# -*- coding: utf-8 -*-
"""DeepFlood 接入的回归测试：重点证明「NS 行为逐字未变」。"""
import io
import json
import os
import sys

os.environ.setdefault("NS_RANDOM", "true")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# --- 本机没装 curl_cffi / requests 时用最小桩顶掉，只测逻辑 ---
import types


def _install_stubs():
    try:
        import requests  # noqa: F401
        return
    except ImportError:
        pass

    req = types.ModuleType("requests")

    class _S:
        def __init__(self):
            self.headers = {}
            self.cookies = types.SimpleNamespace(set=lambda *a, **k: None)

        def get(self, *a, **k):
            raise RuntimeError("网络被禁用（测试桩）")

        def post(self, *a, **k):
            raise RuntimeError("网络被禁用（测试桩）")

    req.Session = _S
    sys.modules["requests"] = req

    cffi = types.ModuleType("curl_cffi")
    cffi.requests = req
    sys.modules["curl_cffi"] = cffi


_install_stubs()

import multi_site_sign_cookie_only as M


class Resp:
    def __init__(self, payload, code=200):
        self._p = payload
        self.status_code = code
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        return self._p


class FakeSession:
    """记录最后一次 POST 的 URL，返回预设响应。"""
    def __init__(self, payload, code=200):
        self.payload = payload
        self.code = code
        self.last_post_url = None
        self.last_headers = None
        self.cookies = []

    def get(self, *a, **k):
        return Resp({}, 200)

    def post(self, url, headers=None, **k):
        self.last_post_url = url
        self.last_headers = headers
        return Resp(self.payload, self.code)


PASS = FAIL = 0


def chk(name, got, want):
    global PASS, FAIL
    if got == want:
        print(f"✅ {name}")
        PASS += 1
    else:
        print(f"❌ {name}\n     期望={want!r}\n     实际={got!r}")
        FAIL += 1


NS = M.SITES_CONFIG["nodeseek"]
DF = M.SITES_CONFIG["deepflood"]

# ---------------- sign()：NS ----------------
print("=== 1. NS 签到判定（必须与改动前一致）===")

case = FakeSession({"success": True, "message": "今天的签到收益是5个鸡腿"})
M.create_session = lambda c=None: case
st = M.sign("k=v", NS, "true")
chk("NS 成功（鸡腿）", st[0], "success")
chk("NS 用环境变量的 random=true", case.last_post_url.endswith("random=true"), True)
chk("NS 请求了正确的域名", case.last_post_url.startswith("https://www.nodeseek.com/api/attendance"), True)

case2 = FakeSession({"success": False, "message": "今天已完成签到，请勿重复操作"})
M.create_session = lambda c=None: case2
chk("NS 已完成签到 → already", M.sign("k=v", NS, "true")[0], "already")

# 关键：这句在改动前是 fail（NS 没配 already_keywords），现在必须仍是 fail
case3 = FakeSession({"success": False, "message": "今天已签到"})
M.create_session = lambda c=None: case3
chk("NS 未配置的关键词不生效（今天已签到→fail，与旧版一致）",
    M.sign("k=v", NS, "true")[0], "fail")

# NS 的 random 仍受 env 控制
case4 = FakeSession({"success": True, "message": "鸡腿"})
M.create_session = lambda c=None: case4
M.sign("k=v", NS, "false")
chk("NS 传 false 时 random=false（env 仍然生效）",
    case4.last_post_url.endswith("random=false"), True)

# ---------------- sign()：DF ----------------
print("\n=== 2. DF 签到 ===")

case5 = FakeSession({"success": True, "message": "今日签到获得鸡腿11个，当前排名第9"})
M.create_session = lambda c=None: case5
st = M.sign("k=v", DF, "false")   # 故意传 false，验证站点配置优先
chk("DF 成功", st[0], "success")
chk("DF 域名正确", case5.last_post_url.startswith("https://www.deepflood.com/api/attendance"), True)
chk("DF 即使 env 传 false 也用 random=true（试试手气）",
    case5.last_post_url.endswith("random=true"), True)

case6 = FakeSession({"success": False, "message": "今天已签到"})
M.create_session = lambda c=None: case6
chk("DF 已签到关键词生效 → already", M.sign("k=v", DF, "true")[0], "already")

case7 = FakeSession({"success": False, "message": "USER NOT FOUND", "status": 404})
M.create_session = lambda c=None: case7
chk("DF cookie 失效 → invalid", M.sign("k=v", DF, "true")[0], "invalid")

# ---------------- 登录分流 ----------------
print("\n=== 3. 登录分流：DF 走一键登录（复用 NS profile），NS 走原路径 ===")

calls = {"df": [], "ns": []}
M.PLAYWRIGHT_AVAILABLE = True


def fake_df_login(site_config, user, pwd, ns_account=None, ns_cookie="", headless=None, **kw):
    calls["df"].append((user, ns_account))
    calls.setdefault("ns_cookies", []).append(ns_cookie)
    return {"cookie": "session=dfok", "cookie_life": "还剩 29 天",
            "profile_account": ns_account}


def fake_ns_login(site_config, user, pwd, headless=True):
    calls["ns"].append(user)
    return {"cookie": "session=nsok", "cookie_life": "还剩 29 天",
            "device_memory_expired": False, "needs_email_verify": False}


M.deepflood_login_detail = fake_df_login
M.browser_login_detail = fake_ns_login
M.save_cookie_to_file = lambda *a, **k: None
M.save_cookie_meta = lambda *a, **k: None
M.load_cookie_meta = lambda *a, **k: {}

# 多账号：DF 第 1/2 个账号分别对应 NS 的两个账号
os.environ["DF_NS_ACCOUNT"] = "账号A&账号B"
c1, m1 = M.recover_cookie("deepflood", DF, 1, "df_a", "p")
c2, m2 = M.recover_cookie("deepflood", DF, 2, "df_b", "p")
chk("DF 账号1 → NS profile「账号A」", calls["df"][0][1], "账号A")
chk("DF 账号2 → NS profile「账号B」", calls["df"][1][1], "账号B")
chk("DF 账号1 拿到 cookie", c1, "session=dfok")
chk("DF 账号2 拿到 cookie", c2, "session=dfok")
chk("DF 没走 NS 的登录路径", len(calls["ns"]), 0)

# 没配 DF_NS_ACCOUNT → 默认用 DF 用户名当 profile 名
os.environ.pop("DF_NS_ACCOUNT", None)
M.recover_cookie("deepflood", DF, 1, "same_name", "p")
chk("未配 DF_NS_ACCOUNT 时回落到 DF 用户名", calls["df"][-1][1], "same_name")

# 只配一个 NS 账号 → 所有 DF 账号都用它
os.environ["DF_NS_ACCOUNT"] = "only_ns"
M.recover_cookie("deepflood", DF, 2, "df_c", "p")
chk("只配一个 NS 账号时全部复用", calls["df"][-1][1], "only_ns")

# NS 一个字节都没受影响
c3, _ = M.recover_cookie("nodeseek", NS, 1, "ns_user", "p")
chk("NS 仍走 browser_login_detail", len(calls["ns"]), 1)
chk("NS 没走 DF 的登录", len(calls["df"]), 4)
chk("NS 配置里没有 login_mode", NS.get("login_mode"), None)
chk("NS 配置里没有 ns_account_var", NS.get("ns_account_var"), None)

# DF 登录失败时，原因要能传出到告警
def failing_df_login(*a, **k):
    return {"error": "NS_NOT_LOGGED_IN", "message": "这个 profile 里 NodeSeek 没登录"}


M.deepflood_login_detail = failing_df_login
cbad, mbad = M.recover_cookie("deepflood", DF, 1, "df_a", "p")
chk("DF 失败时 cookie 为 None", cbad, None)
chk("DF 失败原因带 error 码", "NS_NOT_LOGGED_IN" in (mbad.get("login_error") or ""), True)

print("\n=== 4. process_site 端到端（DF 无 cookie → 触发一键登录）===")

login_attempts = {"df": 0}


def counting_df_login(*a, **k):
    login_attempts["df"] += 1
    return {"cookie": "", "error": "NS_NOT_LOGGED_IN", "message": "NS 没登录"}


M.deepflood_login_detail = counting_df_login
os.environ["DF_USER_PASS"] = "df_user&df_pass"
os.environ["DF_COOKIE"] = ""
M.load_cookies_from_file_orig = M.load_cookies_from_file
M.load_cookies_from_file = lambda *a, **k: ""
M.send = lambda *a, **k: None
M.NOTIFICATION_FILE = os.devnull

try:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    M.process_site("deepflood", DF, "true")
    sys.stdout = old
finally:
    M.load_cookies_from_file = M.load_cookies_from_file_orig

out = buf.getvalue()
chk("DF 无 cookie 时会尝试一键登录", login_attempts["df"], 1)
chk("DF 失败原因写进告警文案", "NS 没登录" in out, True)

print("\n=== 5. 账号数计数（cookie 文件 / 跟随 NS / NS 不受影响）===")

import tempfile

# 隔离出一个空 cookie 目录，避免读到本机真实的 cookie 文件
_tmpdir = tempfile.mkdtemp(prefix="dfcnt_")
_old_cookie_dir = M._COOKIE_DIR
M._COOKIE_DIR = _tmpdir

_real_sign = M.sign
M.sign = lambda *a, **k: ("success", "今天签到成功")
M.check_cookie_validity = lambda *a, **k: True
M.send = lambda *a, **k: None
M.NOTIFICATION_FILE = os.devnull

try:
    # 5a：只有 cookie 文件，没有 DF_USER_PASS / DF_COOKIE → 以前会被判 0 个账号跳过
    with open(os.path.join(_tmpdir, "DEEPFLOOD_COOKIE_1.txt"), "w", encoding="utf-8") as f:
        f.write("session=dfonly; pjwt=x")
    chk("count_saved_cookie_files 能数到 DF cookie 文件",
        M.count_saved_cookie_files("deepflood"), 1)

    os.environ["DF_USER_PASS"] = ""
    os.environ["DF_COOKIE"] = ""
    os.environ["DF_NS_ACCOUNT"] = "账号A"
    M.load_cookies_from_file = lambda site, idx=None: (
        "session=dfonly; pjwt=x" if site == "deepflood" else "")

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        M.process_site("deepflood", DF, "true")
    finally:
        sys.stdout = old
    out = buf.getvalue()
    chk("只有 cookie 文件时不再整站跳过", "跳过" in out, False)
    chk("只有 cookie 文件时确实跑了 1 个账号", out.count("--- 账号:"), 1)

    # 5b：DF 什么都没配 → 账号数跟随 NS（NS 配 2 个账号）
    os.remove(os.path.join(_tmpdir, "DEEPFLOOD_COOKIE_1.txt"))
    os.environ["NS_USER_PASS"] = "ns_a&p1&ns_b&p2"
    chk("NS 账号数算出来是 2", M.count_ns_accounts(), 2)

    attempts = {"n": 0}

    def ok_df_login(*a, **k):
        attempts["n"] += 1
        return {"cookie": "session=dfok", "cookie_life": "还剩 29 天"}

    M.deepflood_login_detail = ok_df_login
    M.load_cookies_from_file = lambda site, idx=None: ""

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        M.process_site("deepflood", DF, "true")
    finally:
        sys.stdout = old
    out = buf.getvalue()
    chk("DF 全空时跟随 NS 账号数跑 2 个", out.count("--- 账号:"), 2)
    chk("DF 全空时触发 2 次一键登录", attempts["n"], 2)

    # 5c：只存了账号 1 的 cookie，NS 有 2 个账号 → DF 仍要跑 2 个
    #（第 2 个没有 cookie，走一键登录；不能因为文件数=1 就漏掉）
    with open(os.path.join(_tmpdir, "DEEPFLOOD_COOKIE_1.txt"), "w", encoding="utf-8") as f:
        f.write("session=dfonly")
    os.environ["NS_USER_PASS"] = "ns_a&p1&ns_b&p2"
    os.environ["DF_USER_PASS"] = ""
    os.environ["DF_COOKIE"] = ""
    attempts2 = {"n": 0}

    def ok_df_login2(*a, **k):
        attempts2["n"] += 1
        return {"cookie": "session=dfok2", "cookie_life": "还剩 29 天"}

    M.deepflood_login_detail = ok_df_login2
    M.load_cookies_from_file = lambda site, idx=None: (
        "session=dfonly" if (site == "deepflood" and idx == 1) else "")

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        M.process_site("deepflood", DF, "true")
    finally:
        sys.stdout = old
    out = buf.getvalue()
    chk("只有账号1的 cookie 时仍跑满 2 个账号", out.count("--- 账号:"), 2)
    chk("缺 cookie 的那个走一键登录", attempts2["n"], 1)
    os.remove(os.path.join(_tmpdir, "DEEPFLOOD_COOKIE_1.txt"))

    # 5d：NS 的计数逻辑不受影响（nodeseek 没配 count_cookie_files）
    with open(os.path.join(_tmpdir, "NODESEEK_COOKIE_1.txt"), "w") as f:
        f.write("session=ns1")
    with open(os.path.join(_tmpdir, "NODESEEK_COOKIE_2.txt"), "w") as f:
        f.write("session=ns2")
    chk("NS 配置里没有 count_cookie_files", NS.get("count_cookie_files"), None)
    chk("NS 配置里没有 count_follow_ns", NS.get("count_follow_ns"), None)

    os.environ["NS_USER_PASS"] = "ns_a&p1"          # 只配 1 个账号
    os.environ["NS_COOKIE"] = "session=ns1; pjwt=x"
    M.load_cookies_from_file = lambda site, idx=None: (
        "session=ns1; pjwt=x" if site == "nodeseek" else "")

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        M.process_site("nodeseek", NS, "true")
    finally:
        sys.stdout = old
    out = buf.getvalue()
    chk("NS 仍只跑 1 个账号（cookie 文件不参与计数）", out.count("--- 账号:"), 1)
finally:
    M._COOKIE_DIR = _old_cookie_dir
    M.sign = _real_sign
    M.load_cookies_from_file = M.load_cookies_from_file_orig

print(f"\n===== 结果：{PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
