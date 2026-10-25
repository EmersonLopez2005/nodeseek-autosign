#!/usr/bin/env bash
# 每日签到的 cron 包装脚本。
#
# 为什么不让 crontab 直接调 python：
#   1) crontab 的命令行对引号/百分号极其敏感（% 在 crontab 里是换行符），
#      任何复杂逻辑写进去极易出错；
#   2) flock 拿不到锁时**静默跳过**，日志里一个字节都没有 ——
#      这是最难查的故障形态（cron 触发了，但脚本没跑，也没日志）；
#   3) 没有心跳，无法区分「cron 根本没触发」和「触发了但脚本崩了」；
#   4) **环境变量不能和调度字段写在同一行** —— cron 解析器遇到 '=' 会把
#      整行当成「环境变量赋值」，这一行一个 job 都不生成，到点永不触发，
#      而且不报任何错。环境变量必须在这里 export，而不是塞进 crontab。
#
# 用法：bash run_sign.sh          （正常跑，输出写进 logs/sign.log）
#       bash run_sign.sh --now    （忽略锁，立刻跑一次）

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

# ---- 环境：自己补齐，不依赖 crontab 里的变量行 ----
# cron 的默认 PATH 很短（通常只有 /usr/bin:/bin），缺 /usr/local/bin
# 会找不到 flock / Xvfb / camoufox；HOME 也可能不是预期的目录
# （profile、Xvfb、camoufox 缓存都靠它）。
export HOME="${HOME:-/root}"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"

LOG_DIR="$APP_DIR/logs"
LOG_FILE="$LOG_DIR/sign.log"
HEARTBEAT="$LOG_DIR/last_run.txt"
LOCK_FILE="${NS_SIGN_LOCK:-/tmp/nodeseek-sign.lock}"

mkdir -p "$LOG_DIR"

ts() { date -Is; }

# ---------------------------------------------------------------- 选解释器
PY=""
for c in "$APP_DIR/.venv/bin/python" "$APP_DIR/venv/bin/python" \
         "$(command -v python3 2>/dev/null || true)" \
         "$(command -v python 2>/dev/null || true)"; do
  if [ -n "$c" ] && [ -x "$c" ]; then PY="$c"; break; fi
done

{
  echo ""
  echo "================ [$(ts)] 定时签到开始 ================"
} >> "$LOG_FILE"

if [ -z "$PY" ]; then
  echo "[$(ts)] ✗ 找不到 python 解释器（试过 .venv/bin/python / venv/bin/python / python3）" >> "$LOG_FILE"
  printf 'last_run=%s\nstatus=no_python\n' "$(ts)" > "$HEARTBEAT"
  exit 127
fi

# ---------------------------------------------------------------- 文件锁
# 拿不到锁 = 上一轮还在跑。要**留下痕迹**，绝不静默跳过。
if [ "${1:-}" != "--now" ]; then
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE" || true
    if ! flock -n 9; then
      echo "[$(ts)] ⚠️ 上一轮还在运行（锁被占用），本轮跳过" >> "$LOG_FILE"
      printf 'last_run=%s\nstatus=skipped_locked\n' "$(ts)" > "$HEARTBEAT"
      echo "================ [$(ts)] 结束（跳过：锁被占用） ================" >> "$LOG_FILE"
      exit 0
    fi
  fi
fi

# 先写一次「运行中」心跳：万一 python 卡死/被 kill，也能看出它确实起过
printf 'last_run=%s\nstatus=running\n' "$(ts)" > "$HEARTBEAT"

# ---------------------------------------------------------------- 真跑
"$PY" multi_site_sign_cookie_only.py >> "$LOG_FILE" 2>&1
rc=$?

if [ "$rc" -eq 0 ]; then
  st="ok"
else
  st="failed"
fi

printf 'last_run=%s\nexit_code=%s\nstatus=%s\n' "$(ts)" "$rc" "$st" > "$HEARTBEAT"

{
  echo ""
  echo "================ [$(ts)] 定时签到结束（退出码 $rc） ================"
} >> "$LOG_FILE"

exit "$rc"
