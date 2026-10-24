#!/usr/bin/env bash
# 清除 git 历史里的密钥。
#
#   bash purge_secrets.sh --dry-run    # 只看会改什么，不动历史（默认）
#   bash purge_secrets.sh --yes        # 真的重写历史 + force push
#
# ⚠️ 重写历史会改掉所有 commit 的 SHA，force push 后别人如果 clone 过旧历史会冲突。
#    这个仓库只有自己用，改写代价很低。
#
# 前提：先确认凭据已在平台侧吊销/重置（删代码不等于换密钥）。
#
# 原理：用 git-filter-repo 把所有提交里的敏感串替换成占位符；
# 没有 filter-repo 就用 filter-branch（git 自带，够用）。

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

DRY=1
for a in "$@"; do
  case "$a" in
    --yes|-y) DRY=0 ;;
    --dry-run) DRY=1 ;;
    --help|-h) sed -n '2,15p' "$0"; exit 0 ;;
  esac
done

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

# 要抹掉的敏感串（空格分隔）。每条都会被替换成 [REDACTED]
SECRETS=(
  "[REDACTED_TG_TOKEN]"
  "[REDACTED]"
)

step "检查当前状态"
git rev-parse --git-dir >/dev/null 2>&1 || { red "  这不是 git 仓库"; exit 1; }
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "  当前分支：$BRANCH"
echo "  远程：$(git remote get-url origin 2>/dev/null || echo '(无)')"

step "定位含敏感串的提交"
FOUND=""
for s in "${SECRETS[@]}"; do
  HITS="$(git log --all --oneline -S "$s" 2>/dev/null | wc -l)"
  if [ "$HITS" -gt 0 ]; then
    echo "  ${s:0:14}… 出现在 $HITS 个提交"
    git log --all --oneline -S "$s" | sed 's/^/      /'
    FOUND="yes"
  else
    echo "  ${s:0:14}… 未发现"
  fi
done
if [ -z "$FOUND" ]; then
  grn "  历史里没有敏感串，无需清理。"
  exit 0
fi

# 生成替换表达式
EXPR=""
for s in "${SECRETS[@]}"; do
  [ -n "$EXPR" ] && EXPR="$EXPR|"
  EXPR="$EXPR${s//\\/\\\\}"
done

if [ "$DRY" = "1" ]; then
  step "计划（dry-run，未改动任何东西）"
  cat <<EOF
  1) 用 git-filter-repo 把历史里的这些串替换成 [REDACTED]
  2) 强制推送到 origin/$BRANCH（会改写所有 commit SHA）
  3) 其他 clone 过本仓库的机器需要重新 clone 或 git fetch + reset

  确认后执行：bash purge_secrets.sh --yes
EOF
  ylw "  ⚠️ 执行前请确认该凭据已在平台侧吊销/重置。"
  exit 0
fi

step "准备备份"
BAK="$APP_DIR/.git-backup-before-purge-$(date +%Y%m%d-%H%M%S)"
cp -a .git "$BAK" && grn "  已备份 .git 到 $BAK" || { red "  备份失败，中止"; exit 1; }

step "重写历史"
if command -v git-filter-repo >/dev/null 2>&1 || python3 -c "import git_filter_repo" >/dev/null 2>&1; then
  ylw "  用 git-filter-repo"
  FILTER_BRANCH="--replace-text" FILTER_EXPR="$EXPR" \
    git filter-repo --force --replace-text <(echo "s/$EXPR/[REDACTED]/g") 2>&1 | tail -5
  # filter-repo 会移除 origin，需要重加
  git remote add origin "$(git remote get-url origin 2>/dev/null || echo '')" 2>/dev/null || true
else
  ylw "  没装 git-filter-repo，用 git filter-branch（慢一点，但 git 自带）"
  # 只处理真正含敏感串的文件，避免每个 commit 都遍历全部文件（快 10 倍以上）
  git filter-branch --force --tree-filter '
    for f in $(git ls-files); do
      [ -f "$f" ] || continue
      if grep -q -e "[REDACTED_TG_TOKEN]" -e "[REDACTED]" "$f" 2>/dev/null; then
        sed -i -e "s/[REDACTED_TG_TOKEN]/[REDACTED]/g" \
               -e "s/[REDACTED]/[REDACTED]/g" "$f"
      fi
    done
  ' --tag-name-filter cat -- --all 2>&1 | tail -3
fi

step "清理备份引用并回收对象"
# filter-branch 会留 refs/original/* 指向旧历史 —— 不删掉的话 token 还在仓库里，
# 验证会报「仍有残留」，而且这些对象照样能被 clone 拿到。
git for-each-ref --format='%(refname)' refs/original/ 2>/dev/null | while read -r r; do
  [ -n "$r" ] && git update-ref -d "$r" && echo "  已删备份引用 $r"
done
rm -rf .git/refs/original 2>/dev/null || true
# 让旧对象彻底不可达
git reflog expire --expire=now --all >/dev/null 2>&1 || true
git gc --prune=now --aggressive >/dev/null 2>&1 || true
grn "  已清理备份引用并回收对象"

step "验证"
LEFT=0
for s in "${SECRETS[@]}"; do
  # 只查所有 ref（含远程跟踪），不再被 refs/original 干扰
  C="$(git log --all --oneline -S "$s" 2>/dev/null | wc -l)"
  LEFT=$((LEFT + C))
done
# 再直接搜一遍所有可达对象，双保险
if git grep -I -n -F "${SECRETS[0]}" $(git rev-list --all 2>/dev/null) >/dev/null 2>&1; then
  LEFT=$((LEFT + 1))
fi
if [ "$LEFT" -eq 0 ]; then
  grn "  ✅ 历史已清理干净（所有 ref 都查过了）"
else
  red "  ❌ 仍有 $LEFT 处残留，检查上一步输出"
  exit 1
fi

step "强制推送"
if [ "$DRY" = "0" ]; then
  ylw "  即将 force push 到 origin/$BRANCH —— 所有 commit SHA 会变"
  if git push --force origin "$BRANCH" 2>&1 | tail -5; then
    grn "  已推送"
  else
    red "  ❌ 推送失败（上面是原因）。历史已在本地改好，"
    red "     修好远程问题后手动执行：git push --force origin $BRANCH"
    exit 1
  fi
fi

step "收尾"
cat <<EOF
  1. 到 GitHub 仓库页面 →「安全和质量」→「密钥扫描」→ 把告警标成「已解决」
  2. 删掉本地备份：rm -rf $BAK
  3. 如果别的机器 clone 过本仓库，重新 clone 或执行：
       git fetch origin && git reset --hard origin/$BRANCH
EOF
