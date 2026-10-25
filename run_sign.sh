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
#       bash run_sign.sh --dryrun （只检查环境与依赖，不实际签到）

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

DRYRUN=0
[ "${1:-}" = "--dryrun" ] && DRYRUN=1

# 预演模式：**不写 sign.log、不写心跳**。
# 它只是给人看的诊断输出，混进签到日志里会让人分不清哪段是预演、哪段是真签到。
if [ "$DRYRUN" != "1" ]; then
  {
    echo ""
    echo "================ [$(ts)] 定时签到开始 ================"
  } >> "$LOG_FILE"
fi

if [ -z "$PY" ]; then
  if [ "$DRYRUN" != "1" ]; then
    echo "[$(ts)] ✗ 找不到 python 解释器（试过 .venv/bin/python / venv/bin/python / python3）" >> "$LOG_FILE"
    printf 'last_run=%s\nstatus=no_python\n' "$(ts)" > "$HEARTBEAT"
  fi
  exit 127
fi

# ---------------------------------------------------------------- 预演模式
# 只检查「cron 环境里能不能跑通」，不实际发起签到请求。
# 用途：手动跑好好的、cron 里跑失败时，用它来定位是环境问题还是逻辑问题。
if [ "$DRYRUN" = "1" ]; then
  echo "[$(ts)] ===== 预演模式（不实际签到，不写日志）====="
  echo "[$(ts)] 解释器     : $PY"
  echo "[$(ts)] HOME       : ${HOME:-（未设置！）}"
  echo "[$(ts)] PATH       : $PATH"
  echo "[$(ts)] DISPLAY    : ${DISPLAY:-（未设置）}"
  echo "[$(ts)] 工作目录   : $APP_DIR"
  echo "[$(ts)] 当前用户   : $(id -un 2>/dev/null || echo '?')"

  FAIL=0
  check() {
    if eval "$2"; then
      echo "[$(ts)]   ✅ $1"
    else
      echo "[$(ts)]   ❌ $1"
      FAIL=1
    fi
  }
  echo "[$(ts)] --- 依赖检查 ---"
  check "工作目录存在"           "[ -d '$APP_DIR' ]"
  check ".env 存在"              "[ -f '$APP_DIR/.env' ]"
  check "解释器可执行"           "[ -x '$PY' ]"
  check "curl_cffi 已安装"       "'$PY' -c 'import curl_cffi' >/dev/null 2>&1"
  check "python-dotenv 已安装"   "'$PY' -c 'import dotenv' >/dev/null 2>&1"
  check "requests 已安装"        "'$PY' -c 'import requests' >/dev/null 2>&1"

  echo "[$(ts)] --- cookie 状态 ---"
  if [ -d "$APP_DIR/cookie" ]; then
    CNT=$(ls -1 "$APP_DIR/cookie" 2>/dev/null | grep -c '\.txt$' || true)
    echo "[$(ts)]   cookie 文件数: $CNT"
    if [ "${CNT:-0}" -gt 0 ]; then
      echo "[$(ts)]   ✅ 有现成 cookie —— 日常签到只发 HTTP 请求，不需要浏览器"
    else
      echo "[$(ts)]   ⚠️  没有 cookie —— 下次运行需要走浏览器登录（cron 里没法交互）"
    fi
  else
    echo "[$(ts)]   ⚠️  没有 cookie/ 目录"
  fi

  echo "[$(ts)] --- 浏览器能力（只在 cookie 失效续期时才会用到）---"
  if [ -n "${DISPLAY:-}" ]; then
    echo "[$(ts)]   ✅ 有 DISPLAY=$DISPLAY"
  elif command -v Xvfb >/dev/null 2>&1; then
    echo "[$(ts)]   ✅ 无 DISPLAY，但装了 Xvfb —— browser_engine 会自动拉起虚拟屏"
  else
    echo "[$(ts)]   ⚠️  无 DISPLAY 且没装 Xvfb —— 浏览器登录会失败（apt install -y xvfb）"
  fi

  echo "[$(ts)] --- 锁 ---"
  if command -v flock >/dev/null 2>&1; then
    echo "[$(ts)]   ✅ flock 可用：$(command -v flock)"
  else
    echo "[$(ts)]   ⚠️  没有 flock —— 并发时可能重复签到（apt install -y util-linux）"
  fi

  echo "[$(ts)] ===== 预演结束 ===="
  if [ "$FAIL" = "0" ]; then
    echo "[$(ts)] 结果：环境检查通过"
    exit 0
  fi
  echo "[$(ts)] 结果：环境检查**未通过**（见上面 ❌）"
  exit 1
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
