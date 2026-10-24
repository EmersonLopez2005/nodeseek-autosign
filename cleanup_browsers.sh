#!/usr/bin/env bash
# 扫描并清理这台机器上「为了测试装的一堆浏览器」。
#
#   bash cleanup_browsers.sh                 # 只扫描，列出清单和能省多少空间（不动任何文件）
#   bash cleanup_browsers.sh --yes           # 确认无误，执行清理
#   bash cleanup_browsers.sh --keep-engine=camoufox
#
# 安全约定：
#   1. 默认只扫描；要真删必须显式加 --yes。
#   2. 只会删下面白名单里明确列出的路径，绝不碰 /usr、/etc、/var 等系统目录。
#   3. apt 装的浏览器一律用 apt-get remove 卸载（让包管理器自己收尾），
#      不用 rm 去删系统文件。
#   4. 永远保留：当前在用的引擎 + 项目 cookie/profiles/（删了要重做邮箱验证）。

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"

YES=0
KEEP_ENGINE="camoufox"
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
    --keep-engine=*) KEEP_ENGINE="${a#*=}" ;;
    --help|-h)
      sed -n '2,16p' "$0"; exit 0 ;;
  esac
done

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

size_of() {  # 目录算体积，文件算大小；不存在返回 0
  local p="$1"
  [ -e "$p" ] || { echo 0; return; }
  if [ -d "$p" ]; then du -sm "$p" 2>/dev/null | awk '{print $1}'
  else du -sm "$p" 2>/dev/null | awk '{print $1}'; fi
}
mb() { echo "$1MB"; }

# ============================================================ 扫描
step "扫描浏览器相关占用"

# --- 1. 浏览器本体缓存目录 ---
DIRS_TO_CHECK=(
  "$HOME/.cache/camoufox"
  "$HOME/.cache/ms-playwright"
  "$HOME/.cache/ms-playwright-chromium"
  "$HOME/.cache/patchright"
  "/opt/camoufox"
  "/opt/playwright"
  "$APP_DIR/agent/data/profiles"
  "$APP_DIR/cookie/profiles"
)
echo "【浏览器本体 / profile 目录】"
FOUND_DIRS=()
for d in "${DIRS_TO_CHECK[@]}"; do
  if [ -e "$d" ]; then
    s=$(size_of "$d")
    printf '  %8s  %s\n' "$(mb "$s")" "$d"
    FOUND_DIRS+=("$d")
  fi
done
[ ${#FOUND_DIRS[@]} -eq 0 ] && echo "  （没有）"

# --- 2. apt 装的浏览器 ---
echo
echo "【apt 安装的浏览器包】"
APT_BROWSERS=""
if command -v dpkg-query >/dev/null 2>&1; then
  APT_BROWSERS="$(dpkg-query -W -f='${binary:Package}\t${Installed-Size}\n' 2>/dev/null \
    | awk '/^(chromium|firefox|firefox-esr|chromium-driver|firefox-geckodriver|chromium-common|chromium-sandbox)/ {print $1"\t"$2}')"
fi
if [ -n "$APT_BROWSERS" ]; then
  echo "$APT_BROWSERS" | while IFS=$'\t' read -r pkg kb; do
    [ -z "$pkg" ] && continue
    printf '  %8s  %s\n' "$(mb $((kb / 1024)))" "$pkg"
  done
else
  echo "  （没有）"
fi

# --- 3. pip 装的浏览器引擎 ---
echo
echo "【pip 装的浏览器引擎包】"
PIP_ENGINES=""
if [ -x "$PY" ]; then
  PIP_ENGINES="$("$PY" -m pip list 2>/dev/null | awk 'NR>1 {print $1"\t"$2}' | grep -Ei '^(camoufox|patchright|playwright)\b')"
elif command -v pip3 >/dev/null 2>&1; then
  PIP_ENGINES="$(pip3 list 2>/dev/null | awk 'NR>1 {print $1"\t"$2}' | grep -Ei '^(camoufox|patchright|playwright)\b')"
fi
if [ -n "$PIP_ENGINES" ]; then
  echo "$PIP_ENGINES" | while IFS=$'\t' read -r pkg ver; do
    [ -z "$pkg" ] && continue
    printf '  %8s  %s %s\n' "-" "$pkg" "${ver:-}"
  done
else
  echo "  （没有）"
fi

# --- 4. Docker 镜像 ---
echo
echo "【Docker 里的本项目镜像】"
if command -v docker >/dev/null 2>&1; then
  docker images --format '  {{.Size}}\t{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -i nodeseek || echo "  （没有）"
else
  echo "  （没装 docker）"
fi

# --- 5. 项目里的排查残留 ---
echo
echo "【排查残留（截图/HTML，可随时删）】"
DBG_COUNT=$(find "$APP_DIR" -maxdepth 3 -name 'debug_*.png' -o -maxdepth 3 -name 'debug_*.html' 2>/dev/null | wc -l)
if [ "$DBG_COUNT" -gt 0 ]; then
  printf '  %8s  %s 个 debug_* 排查文件（data/ 下）\n' "-" "$DBG_COUNT"
else
  echo "  （没有）"
fi

# ============================================================ 清理
step "清理计划"
PLAN=()

# 目录：ms-playwright 缓存（patchright/playwright 下载的浏览器）
for d in "$HOME/.cache/ms-playwright" "$HOME/.cache/ms-playwright-chromium" "$HOME/.cache/patchright" "/opt/playwright"; do
  [ -e "$d" ] && PLAN+=("目录|$d|$(size_of "$d")MB|不用的浏览器本体缓存")
done

# 目录：camoufox（只有明确说不用它才删）
if [ "$KEEP_ENGINE" != "camoufox" ] && [ -e "$HOME/.cache/camoufox" ]; then
  PLAN+=("目录|$HOME/.cache/camoufox|$(size_of "$HOME/.cache/camoufox")MB|Camoufox 本体（你指定了不保留）")
fi

# 目录：agent 自己的 profile（青龙侧的 cookie/profiles 永远保留）
if [ -d "$APP_DIR/agent/data/profiles" ] && [ "$KEEP_ENGINE" != "camoufox" ]; then
  PLAN+=("目录|$APP_DIR/agent/data/profiles|$(size_of "$APP_DIR/agent/data/profiles")MB|agent 服务的 profile")
fi

# apt 包
if [ -n "$APT_BROWSERS" ]; then
  echo "$APT_BROWSERS" | while IFS=$'\t' read -r pkg kb; do
    [ -z "$pkg" ] && continue
    echo "$pkg"
  done
fi

# pip 包：卸掉不用的引擎
if [ -n "$PIP_ENGINES" ]; then
  while IFS=$'\t' read -r pkg ver; do
    [ -z "$pkg" ] && continue
    if [ "$pkg" != "$KEEP_ENGINE" ]; then
      PLAN+=("pip|$pkg|包|用不上的引擎（保留 $KEEP_ENGINE）")
    fi
  done <<< "$PIP_ENGINES"
fi

# 排查残留
if [ "$DBG_COUNT" -gt 0 ]; then
  PLAN+=("find|$APP_DIR|- ${DBG_COUNT} 个 debug_* 文件|排查残留截图/HTML")
fi

if [ ${#PLAN[@]} -eq 0 ]; then
  grn "  没有可清理的东西，你机器很干净。"
  exit 0
fi

for item in "${PLAN[@]}"; do
  IFS='|' read -r kind target size note <<< "$item"
  case "$kind" in
    目录) printf '  [删目录] %8s  %s\n        └ %s\n' "$size" "$target" "$note" ;;
    pip)  printf '  [卸包]   %8s  %s\n        └ %s\n' "$size" "$target" "$note" ;;
    find) printf '  [删文件] %8s  %s\n        └ %s\n' "$size" "$target" "$note" ;;
  esac
done

echo
echo "  保留：$KEEP_ENGINE（当前在用）"
echo "  保留：$APP_DIR/cookie/profiles（删了要重新做邮箱验证）"

if [ "$YES" != "1" ]; then
  echo
  ylw "  以上是计划，没有删任何东西。"
  ylw "  确认无误后加 --yes 重跑：bash cleanup_browsers.sh --yes"
  exit 0
fi

# ============================================================ 执行
step "执行清理"
for item in "${PLAN[@]}"; do
  IFS='|' read -r kind target size note <<< "$item"
  case "$kind" in
    目录)
      # 安全阀：只允许删这几个前缀下的路径
      case "$target" in
        "$HOME"/.cache/*|"$APP_DIR"/agent/data/*) ;;
        *) red "  跳过（不在白名单）：$target"; continue ;;
      esac
      if rm -rf "$target"; then grn "  已删目录：$target（$size）"; else red "  删不掉：$target"; fi
      ;;
    pip)
      if [ -x "$PY" ]; then "$PY" -m pip uninstall -y "$target" >/dev/null 2>&1 && grn "  已卸包：$target" || ylw "  卸包失败：$target"
      elif command -v pip3 >/dev/null 2>&1; then pip3 uninstall -y "$target" >/dev/null 2>&1 && grn "  已卸包：$target" || ylw "  卸包失败：$target"; fi
      ;;
    find)
      find "$APP_DIR" -maxdepth 3 \( -name 'debug_*.png' -o -name 'debug_*.html' \) -delete 2>/dev/null
      grn "  已清理排查残留文件"
      ;;
  esac
done

# apt 装的浏览器：用包管理器卸载
if [ -n "$APT_BROWSERS" ] && command -v apt-get >/dev/null 2>&1; then
  APT_REMOVE="$(echo "$APT_BROWSERS" | cut -f1 | tr '\n' ' ')"
  echo
  ylw "  apt 浏览器包（chromium/firefox 等）需要确认后卸载："
  echo "    apt-get remove --purge $APT_REMOVE"
  ylw "  没自动执行——你自己确认上面这些包确实用不上再跑这条。"
  ylw "  （Camoufox 自带 Firefox，不依赖系统 firefox 包；留着也不占运行时资源）"
fi

step "完成"
grn "  清理结束。跑一下体检确认环境还正常："
echo "    cd $APP_DIR && .venv/bin/python check_env.py"
echo
ylw "  如果你装了 systemd agent 服务，重启一下让它读新的 .env："
echo "    systemctl restart nodeseek-agent"
