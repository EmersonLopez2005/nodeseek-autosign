#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""密钥自查：提交前扫一遍仓库，确认没有把凭据带进 git。

用法：
    python3 scan_secrets.py            # 扫当前工作区（只看会被提交的文件）
    python3 scan_secrets.py --history  # 连 git 历史一起扫（慢，扫全历史）
    python3 scan_secrets.py --staged   # 只扫已 git add 的文件

退出码：0 = 干净；1 = 发现疑似密钥
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# 每条规则：(名称, 正则, 严重级别, 是否无视「示例上下文」)
#
# 第四个字段 always_report 的含义：这种格式本身足够独特，
# 只要匹配上就值得报出来，不管上下文有没有写「例：」「示例」。
#
# 为什么需要它：上游源码里最常见的泄露形态就是
#   'TG_BOT_TOKEN': '',   # 例：<真实token>
# 注释写着「例：」，但值是真的。早期版本靠上下文判断占位符，
# 结果这类行被整行放过 —— 假阴性，恰恰漏掉了最该报的。
#
# 说明：这些模式都做了「排除已知占位符」的处理，避免把 .env.example 里的空值当泄露。
RULES = [
    ("Telegram Bot Token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"), "高", True),
    # 半截 token：数字ID + 短尾巴，注释里写「例：<数字ID>:<短尾巴>」那种。
    # 完整规则要求尾巴 30+ 位，这种短尾巴匹配不上 → 漏报。踩过这个坑。
    # （刻意不写真实样例：写出来就是新的泄露点，还会被自己扫出来）
    ("Telegram Bot Token(短尾)", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{3,}\b"), "高", True),
    ("钉钉机器人 access_token", re.compile(r"access_token=[A-Za-z0-9]{20,}"), "高", True),
    ("企业微信 webhook", re.compile(r"https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9-]{20,}"), "高", True),
    ("Server酱 SendKey", re.compile(r"\bSQ[A-Za-z0-9]{20,}\b"), "高", True),
    ("PushPlus Token", re.compile(r"\b[0-9a-f]{32}\b(?=.{0,40}pushplus)"), "中", True),
    ("GitHub PAT", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "高", True),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "高", True),
    ("私钥块", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"), "高", True),
    ("NodeSeek 登录态 cookie", re.compile(r"\b(?:pjwt|session|cf_clearance)=[A-Za-z0-9%_.-]{25,}"), "高", True),
]

# 这些是明显的占位/示例值，不算泄露。
# 注意：判断的是「匹配到的值本身」，不是整行 —— 整行判断会漏掉最危险的一类：
#   'TG_BOT_TOKEN': '',   # 例：<数字ID>:<字母数字串>   ← 注释里写「例：」但值是真的
# （这里刻意不写真实样例：写出来就会变成新的泄露点，还会被 secret scanning 命中）
PLACEHOLDER_VALUES = {
    "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "your_token_here", "changeme", "placeholder",
}
PLACEHOLDER_PAT = re.compile(
    r"(?i)(你的|示例|例：|格式|填|这里|改成|你的域名|your|example|placeholder|"
    r"changeme|todo|bot<token>|<token>|\bx{4,}\b|\*{4,}|xxx+)"
)


def _value_is_placeholder(val: str, ctx: str, always_report: bool = False) -> bool:
    """判断命中的这个值是不是占位符。

    三层判定，从严到松：
      1. 值本身就是明显的占位（xxx / your_token_here / <token>…）→ 放过
      2. 规则标记了 always_report（格式足够独特）→ 一定报出来
      3. 上下文里有「例：」「示例」这类提示词，且值本身像样例 → 放过

    第 2 条是关键：上游源码最常见的泄露形态就是注释里写「例：」但值是真的。
    只靠上下文判断会把这类行整行放过 —— 假阴性，恰恰漏掉最该报的。
    """
    v = val.lower()
    if v in PLACEHOLDER_VALUES:
        return True
    if PLACEHOLDER_PAT.search(v):
        return True
    if always_report:
        return False
    # 上下文里紧挨着「例：」「你的」这类提示词，且值本身像示例 → 放过
    if PLACEHOLDER_PAT.search(ctx.lower()):
        # 但如果值长得像真凭据（高熵、含大小写数字混合且够长），照样报出来
        if len(val) >= 30 and re.search(r"[a-z]", val) and re.search(r"[A-Z]", val) \
                and re.search(r"[0-9]", val):
            return False
        return True
    return False

TEXT_EXT = {".py", ".md", ".sh", ".txt", ".json", ".yml", ".yaml", ".env", ".example", ".js", ".toml", ".ini", ".cfg"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "cookie", "data", "logs"}

# 写着「正则本身」的行不算泄露 —— 扫描器/清理工具的源码里必然出现
# 形如 数字:字母数字 的模式串，那是规则定义，不是凭据。
# 判据：同一行里出现正则元字符（\b \d \w {n,} (…) 等）。
_REGEX_SOURCE_HINT = re.compile(r"\\(?:b|d|w|s|D|W|S|B|A|Z)|\{\d+,\}|\(\?:|re\.compile|raw\"")


def _is_rule_definition_line(line: str) -> bool:
    """这行是不是在写正则规则本身（而不是写真实凭据）。"""
    return bool(_REGEX_SOURCE_HINT.search(line))


def _local_literal_rules():
    """读 .purge-secrets.local 里的字面量清单，转成规则。

    这些是没有通用形状、只能精确匹配的值（裸数字 ID、具体邮箱）。
    清单文件已被 .gitignore 排除，不进 git，因此不会变成新的泄露点。
    """
    path = os.path.join(ROOT, ".purge-secrets.local")
    out = []
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                out.append((f"字面量: {line[:6]}…",
                            re.compile(re.escape(line)), "高", True))
    except OSError:
        pass
    return out


def all_rules():
    """全部规则 = 内置形态规则 + 本地字面量清单。"""
    return RULES + _local_literal_rules()


def scan_text(text: str, where: str, hits: list):
    for i, line in enumerate(text.splitlines(), 1):
        if _is_rule_definition_line(line):
            continue
        for name, pat, level, always in all_rules():
            for m in pat.finditer(line):
                val = m.group(0)
                # 长度门槛只用来滤掉误命中的碎片。
                # always_report 的规则不能卡这个门槛 ——
                # 短尾 token 只有 14 字符，卡掉就等于漏报（踩过这个坑）。
                if len(val) < 20 and not always:
                    continue
                # 只看值本身 + 紧邻上下文判断是否占位符
                ctx = line[max(0, m.start() - 40): m.end() + 40]
                if _value_is_placeholder(val, ctx, always):
                    continue
                hits.append((level, name, where, i, val[:24] + "…" if len(val) > 24 else val))


def iter_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git")]
        for f in files:
            if f == os.path.basename(__file__):
                continue
            ext = os.path.splitext(f)[1].lower()
            name = f.lower()
            if ext in TEXT_EXT or name in (".env", ".env.example", ".gitignore", "dockerfile", "makefile"):
                yield os.path.join(base, f)


def scan_worktree(hits: list):
    for path in iter_files():
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                scan_text(fh.read(), os.path.relpath(path, ROOT), hits)
        except Exception:
            pass


def scan_staged(hits: list):
    try:
        out = subprocess.run(["git", "diff", "--cached", "--name-only"],
                             cwd=ROOT, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return
    for rel in out.split():
        if not rel:
            continue
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p) and not rel.startswith(".git"):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                    scan_text(fh.read(), f"[已暂存] {rel}", hits)
            except Exception:
                pass


def scan_history(hits: list):
    """扫全历史。

    用 `git grep -P`（PCRE），不能用 `-E`：POSIX ERE 不支持 \\b 单词边界，
    会导致 Telegram token 这类规则静默失配（踩过这个坑）。

    两个参数陷阱（都踩过）：
      1. 多模式必须每个都带 `-e`。写成 `git grep -P pat1 pat2 rev` 会把 pat2
         当成 revision 解析而直接报错。
      2. rev 后面**不能**跟 `--`。`git grep -P -e pat -- rev` 会把 rev 当成
         路径限定符，搜不到任何东西却不报错 —— 静默假阴性，最危险的一种。
         正确形式：`git grep -P -e pat1 -e pat2 rev`。
    如果 git 没编译 PCRE，自动退回逐 commit 用 Python 正则扫。
    """
    try:
        revs = subprocess.run(["git", "rev-list", "--all"], cwd=ROOT,
                              capture_output=True, text=True, timeout=180).stdout.split()
    except Exception:
        return
    if not revs:
        return

    pcre = True
    seen = set()
    for rev in revs:
        try:
            cmd = ["git", "grep", "-I", "-n", "-P"]
            for _, pat, _lvl, _alw in all_rules():
                cmd += ["-e", pat.pattern]
            cmd.append(rev)
            res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
            if res.returncode not in (0, 1):
                pcre = False
                break
            out = res.stdout
        except Exception:
            pcre = False
            break

        for line in out.splitlines():
            if ":" not in line:
                continue
            # 输出格式是 `rev:file:line:content`。
            # 必须从左侧按固定结构切，不能 rsplit(":", 1) ——
            # 内容里本身含冒号（token 就是 数字:字母数字），
            # rsplit 会把最右边的冒号当分隔符，把 token 前缀切进 loc 里，
            # content 只剩尾巴，正则自然匹配不上 → 静默漏报（踩过这个坑）。
            #
            # rev 是 40 位 SHA，据此定位最稳（路径含冒号也不会切错）：
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue
            _rev, loc, _lineno, content = parts
            content = content.strip()
            key = (loc.strip(), content[:60])
            if key in seen:
                continue
            seen.add(key)
            for name, pat, level, always in all_rules():
                for m in pat.finditer(content):
                    val = m.group(0)
                    if len(val) < 20 and not always:
                        continue
                    ctx = content[max(0, m.start() - 40): m.end() + 40]
                    if _value_is_placeholder(val, ctx, always):
                        continue
                    hits.append((level, name, f"[历史] {loc.strip()}", 0,
                                 val[:24] + "…"))
                    break

    if pcre:
        return

    # 退回：逐 commit 用 Python 正则扫
    print("  （git 无 PCRE 支持，改用逐 commit 扫描，慢一点）")
    for rev in revs:
        try:
            out = subprocess.run(["git", "show", "--format=", rev], cwd=ROOT,
                                 capture_output=True, text=True, timeout=120).stdout
        except Exception:
            continue
        for name, pat, level, always in all_rules():
            for m in pat.finditer(out):
                val = m.group(0)
                if len(val) < 20 and not always:
                    continue
                key = (rev[:7], val[:40])
                if key in seen:
                    continue
                seen.add(key)
                ctx = out[max(0, m.start() - 40): m.end() + 40]
                if _value_is_placeholder(val, ctx, always):
                    continue
                hits.append((level, name, f"[历史 {rev[:7]}]", 0, val[:24] + "…"))
                break


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    hits: list = []

    if mode == "--history":
        print("扫描 git 全历史（慢，几十秒）…")
        scan_history(hits)
    elif mode == "--staged":
        print("扫描已暂存文件…")
        scan_staged(hits)
    else:
        print("扫描工作区文件…")
        scan_worktree(hits)

    if not hits:
        print("\n✅ 没有发现疑似密钥")
        return 0

    print(f"\n❌ 发现 {len(hits)} 处疑似密钥：\n")
    for level, name, where, line, val in hits:
        loc = f"{where}:{line}" if line else where
        print(f"  [{level}] {name}")
        print(f"        位置：{loc}")
        print(f"        内容：{val}")
    print("\n处理办法：")
    print("  1. 立刻去对应平台吊销/重置这个凭据（删代码不等于换密钥）")
    print("  2. 把代码里的值换成占位符，或移到 .env（.env 已被 .gitignore 排除）")
    print("  3. 如果已经进过 git 历史，需要清历史：见 README「密钥泄露应急处理」")
    return 1


if __name__ == "__main__":
    sys.exit(main())
