#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清除 git 历史里的密钥。

用法：
    python3 purge_history.py --dry-run    # 只看会改什么，不动历史（默认）
    python3 purge_history.py --yes        # 真的重写历史 + force push

设计原则
--------
**脚本自身绝不能携带密钥。** 早期版本把真实 token 写死在 SECRETS 数组里，
结果「清理工具」自己就是泄露源 —— 扫历史时永远扫不干净。

所以这里分两类规则：

1. **形态规则**（内置正则）：按凭据的形状识别，比如
   `\\b\\d{8,12}:[A-Za-z0-9_-]{30,}\\b` 命中任意 Telegram bot token。
   换一枚 token 也照样能抓到，不需要知道具体值。
2. **字面量清单**（`.purge-secrets.local`，已被 .gitignore 排除）：
   裸数字、具体邮箱这类没有通用形状、只能精确匹配的值放这里。
   该文件不进 git，所以也不会变成新的泄露点。

前提：能自己吊销的凭据，必须先在平台侧吊销/重置。
      这枚 token 属于上游作者，你没有吊销权限 —— 见 README「密钥泄露应急处理」。
      但它已经公开流通，删除不等于换密钥，仍需从历史里抹掉。

原理：用 git-filter-repo 把所有提交里的敏感串替换成占位符；
      没有 filter-repo 就退回 filter-branch（git 自带，慢一点）。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 形态规则
# 每条：(名称, 正则, 替换占位符)
# 只放「形状」，不放具体值 —— 这些正则对任何一枚同类凭据都成立。
SHAPE_RULES = [
    ("Telegram Bot Token", r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b", "[REDACTED_TG_TOKEN]"),
    # 半截 token（数字ID + 短尾巴），例：注释里写「例：<数字ID>:<短尾巴>」
    # 完整 token 规则要求尾巴 30+ 位，这种短尾巴匹配不上，会残留成泄露点。
    ("Telegram Bot Token(短尾)", r"\b\d{8,12}:[A-Za-z0-9_-]{3,}\b", "[REDACTED_TG_TOKEN]"),
    ("钉钉 access_token", r"access_token=[A-Za-z0-9]{20,}", "[REDACTED_DD_TOKEN]"),
    ("企业微信 webhook", r"qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9-]{20,}",
     "[REDACTED_WECOM_KEY]"),
    ("Server酱 SendKey", r"\bSQ[A-Za-z0-9]{20,}\b", "[REDACTED_SENDKEY]"),
    ("GitHub PAT", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", "[REDACTED_GH_PAT]"),
    ("AWS Access Key", r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED_AWS_KEY]"),
    ("私钥块", r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]"),
    ("NodeSeek cookie", r"\b(?:pjwt|session|cf_clearance)=[A-Za-z0-9%_.-]{25,}", "[REDACTED_COOKIE]"),
]

LOCAL_LIST = ".purge-secrets.local"

# 通用「看起来像凭据」但未必是密钥的行，避免误伤正常的说明文案。
_SKIP_LINE_PAT = re.compile(
    r"(?i)(your|example|placeholder|changeme|xxx|<token>|bot<token>|\*{4,})"
)

# 写着「正则本身」的行不是凭据 —— 本文件和 scan_secrets.py 的源码里
# 必然出现形如 数字:字母数字 的模式串，那是规则定义。
_REGEX_SOURCE_HINT = re.compile(r"\\(?:b|d|w|s|D|W|S|B|A|Z)|\{\d+,\}|\(\?:|re\.compile|raw\"")


def _is_rule_definition_line(line: str) -> bool:
    """这行是不是在写正则规则本身（而不是写真实凭据）。"""
    return bool(_REGEX_SOURCE_HINT.search(line))


def _run(args, **kw):
    return subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", **kw
    )


def load_literals():
    """读 .purge-secrets.local，返回 [(字面量, 占位符)]。文件不存在则为空。"""
    path = os.path.join(ROOT, LOCAL_LIST)
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append((line, "[REDACTED]"))
    return out


def build_rules():
    """返回 [(名称, 正则串, 占位符)]，形态规则 + 字面量规则。"""
    rules = [(n, p, r) for n, p, r in SHAPE_RULES]
    for lit, _ph in load_literals():
        # 字面量按字面匹配：转义后加边界，避免短数字误命中更长的数字
        pat = r"(?<![0-9A-Za-z])" + re.escape(lit) + r"(?![0-9A-Za-z])"
        rules.append((f"字面量: {lit[:6]}…", pat, "[REDACTED]"))
    return rules


def compile_rules(rules):
    return [(n, re.compile(p), ph) for n, p, ph in rules]


def scan_history(rules):
    """扫所有可达提交，返回 {commit: [(名称, 文件, 行号, 片段)]}。"""
    revs = _run(["git", "rev-list", "--all"]).stdout.split()
    compiled = compile_rules(rules)
    hits = {}
    for rev in revs:
        files = _run(["git", "ls-tree", "-r", "--name-only", rev]).stdout.splitlines()
        for f in files:
            blob = _run(["git", "show", f"{rev}:{f}"]).stdout
            if not blob:
                continue
            for i, line in enumerate(blob.splitlines(), 1):
                if _is_rule_definition_line(line):
                    continue
                for name, pat, _ph in compiled:
                    m = pat.search(line)
                    if not m:
                        continue
                    frag = m.group(0)
                    if _SKIP_LINE_PAT.search(frag):
                        continue
                    hits.setdefault(rev, []).append((name, f, i, frag[:12] + "…"))
    return hits


def show_hits(hits, label="命中"):
    if not hits:
        print(f"  {label}：无")
        return
    for rev, lst in hits.items():
        print(f"  {label} {rev[:7]}  ({len(lst)} 处)")
        for name, f, i, frag in lst[:8]:
            print(f"      [{name}] {f}:{i}  {frag}")
        if len(lst) > 8:
            print(f"      … 还有 {len(lst) - 8} 处")


def make_filter_repo_expr(rules):
    """生成 git-filter-repo --replace-text 用的表达式文件内容。

    filter-repo 的表达式格式是 `regex:PATTERN==>REPLACEMENT`
    （每行一条，`==>` 是分隔符），不是 sed 风格的 s/// —— 用错会静默不生效。
    占位符里的 [ ] 不是正则元字符，原样写入即可。
    """
    lines = []
    for name, pat, ph in rules:
        lines.append(f"regex:{pat}==>{ph}")
    return "\n".join(lines) + "\n"


def run_filter_repo(rules):
    expr = make_filter_repo_expr(rules)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(expr)
        expr_file = fh.name
    try:
        # 优先用 `git filter-repo` 子命令；pip 装的模块常常没把可执行文件
        # 放进 PATH（Windows 上尤其常见），这时退回 `python -m git_filter_repo`。
        # filter-repo 默认就重写全部分支和标签，不需要额外的 --all。
        cmd = ["git", "filter-repo", "--force", "--replace-text", expr_file]
        if not shutil.which("git-filter-repo"):
            cmd = [sys.executable, "-m", "git_filter_repo",
                   "--force", "--replace-text", expr_file]
        print("  " + " ".join(cmd[:4]) + " …")
        res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        if res.returncode != 0:
            print(res.stdout[-2000:])
            print(res.stderr[-2000:])
            return False
        print(res.stdout[-1500:])
        return True
    finally:
        try:
            os.unlink(expr_file)
        except OSError:
            pass


def run_filter_branch(rules):
    """没有 filter-repo 时的退路：filter-branch + tree-filter。

    用 python 就地改写每个文件，避免 sed 的跨平台转义差异。
    """
    payload = [{"name": n, "pattern": p, "replacement": ph} for n, p, ph in rules]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        payload_file = fh.name
    try:
        helper = (
            "import json,os,re,sys\n"
            f"rules=json.load(open({payload_file!r},encoding='utf-8'))\n"
            "rx=[(r['name'],re.compile(r['pattern']),r['replacement']) for r in rules]\n"
            "for root,dirs,files in os.walk('.'):\n"
            "    dirs[:]=[d for d in dirs if d!='.git']\n"
            "    for fn in files:\n"
            "        p=os.path.join(root,fn)\n"
            "        try:\n"
            "            data=open(p,'rb').read()\n"
            "            try: text=data.decode('utf-8')\n"
            "            except UnicodeDecodeError: continue\n"
            "            orig=text\n"
            "            for name,pat,rep in rx:\n"
            "                text=pat.sub(rep,text)\n"
            "            if text!=orig:\n"
            "                open(p,'w',encoding='utf-8',newline='').write(text)\n"
            "        except OSError: pass\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(helper)
            helper_file = fh.name
        try:
            cmd = [
                "git", "filter-branch", "--force",
                "--tree-filter", f'python "{helper_file}"',
                "--tag-name-filter", "cat", "--", "--all",
            ]
            print("  git filter-branch --tree-filter …（较慢，请耐心）")
            res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
            print(res.stdout[-1500:])
            if res.returncode != 0:
                print(res.stderr[-2000:])
                return False
            return True
        finally:
            try:
                os.unlink(helper_file)
            except OSError:
                pass
    finally:
        try:
            os.unlink(payload_file)
        except OSError:
            pass


def cleanup_refs_and_gc():
    """删掉 filter-branch 留下的 refs/original/*，回收旧对象。

    不做这步的话旧对象照样能被 clone 拿到 —— 验证会报「仍有残留」，
    而且密钥其实还在仓库里。
    """
    for ns in ("refs/original/", "refs/replace/"):
        refs = _run(["git", "for-each-ref", "--format=%(refname)", ns]).stdout.split()
        for r in refs:
            _run(["git", "update-ref", "-d", r])
            print(f"  已删备份引用 {r}")
        shutil.rmtree(os.path.join(ROOT, ".git", "refs", ns.split("/")[1]),
                      ignore_errors=True)
    _run(["git", "reflog", "expire", "--expire=now", "--all"])
    _run(["git", "gc", "--prune=now", "--aggressive"])
    print("  已清理备份引用并回收对象")


def main():
    ap = argparse.ArgumentParser(description="清除 git 历史里的密钥")
    ap.add_argument("--dry-run", action="store_true", help="只看会改什么，不动历史")
    ap.add_argument("--yes", "-y", action="store_true", help="真的重写历史 + force push")
    ap.add_argument("--no-push", action="store_true", help="只改本地，不推送")
    args = ap.parse_args()

    if _run(["git", "rev-parse", "--git-dir"]).returncode != 0:
        print("这不是 git 仓库")
        return 1

    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    remote = _run(["git", "remote", "get-url", "origin"]).stdout.strip() or "(无)"

    print("== 当前状态 ==")
    print(f"  分支：{branch}")
    print(f"  远程：{remote}")

    rules = build_rules()
    lits = load_literals()
    print("\n== 生效规则 ==")
    print(f"  形态规则 {len(SHAPE_RULES)} 条（按凭据形状匹配，不含具体值）")
    print(f"  字面量清单 {len(lits)} 条（来自 {LOCAL_LIST}，该文件不进 git）")

    print("\n== 扫描历史 ==")
    hits = scan_history(rules)
    if not hits:
        print("  历史里没有匹配的敏感串，无需清理。")
        return 0
    show_hits(hits)
    print(f"  合计 {sum(len(v) for v in hits.values())} 处，"
          f"分布在 {len(hits)} 个提交")

    if args.dry_run or not args.yes:
        print("\n== 计划（未改动任何东西）==")
        print("  1) 把上述敏感串在所有提交里替换成占位符")
        print(f"  2) 删掉 refs/original/* 备份引用并 gc 回收旧对象")
        print(f"  3) 验证所有 ref 都搜不到后 force push 到 origin/{branch}")
        print("\n  确认后执行：python3 purge_history.py --yes")
        print("  ⚠️ 若这些凭据是你自己的，请先在平台侧吊销/重置。")
        return 0

    print("\n== 准备备份 ==")
    bak = os.path.join(ROOT, f".git-backup-before-purge-{os.getpid()}")
    try:
        shutil.copytree(os.path.join(ROOT, ".git"), bak)
        print(f"  已备份 .git 到 {bak}")
    except Exception as e:
        print(f"  备份失败（{e}），中止")
        return 1

    print("\n== 重写历史 ==")
    # filter-repo 会删掉 origin remote，所以 URL 必须先存下来再动手
    origin_url = _run(["git", "remote", "get-url", "origin"]).stdout.strip()
    if shutil.which("git-filter-repo") or _has_filter_repo_module():
        ok = run_filter_repo(rules)
        if ok and origin_url:
            _run(["git", "remote", "remove", "origin"])
            _run(["git", "remote", "add", "origin", origin_url])
            print(f"  已恢复 origin → {origin_url}")
    else:
        print("  没装 git-filter-repo，用 git filter-branch（慢一点，但 git 自带）")
        ok = run_filter_branch(rules)
    if not ok:
        print("  重写失败，可从备份恢复：")
        print(f"    rm -rf .git && mv {bak} .git")
        return 1

    print("\n== 清理备份引用并回收对象 ==")
    cleanup_refs_and_gc()

    print("\n== 验证 ==")
    left = scan_history(rules)
    if left:
        show_hits(left, "残留")
        print(f"  ❌ 仍有 {sum(len(v) for v in left.values())} 处残留")
        return 1
    print("  ✅ 历史已清理干净（所有 ref 都查过了）")

    if not args.no_push:
        print("\n== 强制推送 ==")
        print(f"  即将 force push 到 origin/{branch} —— 所有 commit SHA 会变")
        res = _run(["git", "push", "--force", "origin", branch])
        if res.returncode != 0:
            print(f"  ❌ 推送失败：{(res.stderr or res.stdout)[-800:]}")
            print(f"     历史已在本地改好，手动执行：git push --force origin {branch}")
            return 1
        print("  已推送")

    print("\n== 收尾 ==")
    print("  1. 到 GitHub 仓库页 →「安全和质量」→「密钥扫描」→ 把告警标成「已解决」")
    print(f"  2. 删掉本地备份：rm -rf {bak}")
    print(f"  3. 别的机器 clone 过的话：git fetch origin && git reset --hard origin/{branch}")
    return 0


def _has_filter_repo_module():
    return _run([sys.executable, "-c", "import git_filter_repo"]).returncode == 0


if __name__ == "__main__":
    sys.exit(main())
