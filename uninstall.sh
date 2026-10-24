#!/usr/bin/env bash
# 彻底卸载 NodeSeek 自动签到项目（含浏览器、systemd、cron、Docker 残留）
#
#   bash uninstall.sh              # 只扫描，列出将删除的一切 + 备份内容，不动任何文件
#   bash uninstall.sh --yes        # 执行卸载
#   bash uninstall.sh --yes --purge-system-deps   # 连系统级 apt 依赖一起卸（谨慎）
#   bash uninstall.sh --yes --with-swap          # 连 install_vps.sh 建的 1G swap 一起删
#
# 安全约定：
#   1. 默认只扫描；真删必须显式 --yes。
#   2. 卸载前自动备份 .env（账号密码配置）和 cookie（登录态）到
#      /root/nodeseek-backup-<日期>.tar.gz（权限 600），明天换机器直接用。
#   3. 删除项目目录时先整体移到临时目录再删，不会拿错路径。
#   4. 系统 apt 依赖默认保留（别的程序可能在用），要卸请加 --purge-system-deps。
#   5. swap 默认保留（是通用内存保险，不删也不影响）。

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YES=0
PURGE_DEPS=0
WITH_SWAP=0
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
    --purge-system-deps) PURGE_DEPS=1 ;;
    --with-swap) WITH_SWAP=1 ;;
    --help|-h) sed -n '2,16p' "$0"; exit 0 ;;
  esac
done

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

sz() { du -sm "$1" 2>/dev/null | awk '{print $1}'; }

# ============================================================ 扫描
step "扫描待卸载内容"

# 服务名有两个历史写法，两个都认
SVC_NAMES="nodeseek-agent nodeseek-login-agent"
SVC_FOUND=""
for s in $SVC_NAMES; do
  if systemctl list-unit-files 2>/dev/null | grep -q "^$s" \
     || [ -f "/etc/systemd/system/$s.service" ]; then
    SVC_FOUND="$SVC_FOUND $s.service"
  fi
done

echo "【项目本体】"
printf '  %8s  %s\n' "$(sz "$APP_DIR")MB" "$APP_DIR"
if [ -d "$APP_DIR/.venv" ]; then
  printf '  %8s  %s\n' "$(sz "$APP_DIR/.venv")MB" "$APP_DIR/.venv（Python 虚拟环境）"
fi
if [ -d "$APP_DIR/cookie/profiles" ]; then
  printf '  %8s  %s\n' "$(sz "$APP_DIR/cookie/profiles")MB" "$APP_DIR/cookie/profiles（浏览器指纹 profile）"
fi

echo
echo "【浏览器本体（缓存）】"
for d in "$HOME/.cache/camoufox" "$HOME/.cache/ms-playwright" "$HOME/.cache/ms-playwright-chromium" "$HOME/.cache/patchright" "/opt/camoufox" "/opt/playwright"; do
  [ -e "$d" ] && printf '  %8s  %s\n' "$(sz "$d")MB" "$d"
done

echo
echo "【apt 装的浏览器包】"
APT_BROWSERS=""
if command -v dpkg-query >/dev/null 2>&1; then
  APT_BROWSERS="$(dpkg-query -W -f='${binary:Package}\t${Installed-Size}\n' 2>/dev/null \
    | awk '/^(chromium|firefox|firefox-esr|chromium-driver|firefox-geckodriver|chromium-common)/ {print $1"\t"$2}')"
fi
if [ -n "$APT_BROWSERS" ]; then
  echo "$APT_BROWSERS" | while IFS=$'\t' read -r pkg kb; do
    [ -z "$pkg" ] && continue
    printf '  %8s  %s\n' "$((kb / 1024))MB" "$pkg"
  done
else
  echo "  （没有）"
fi

echo
echo "【pip 装的浏览器引擎（全局）】"
PIP_ENGINES=""
if command -v pip3 >/dev/null 2>&1; then
  PIP_ENGINES="$(pip3 list 2>/dev/null | awk 'NR>1 {print $1}' | grep -Ei '^(camoufox|patchright|playwright)$' | tr '\n' ' ')"
fi
[ -n "$PIP_ENGINES" ] && echo "  $PIP_ENGINES" || echo "  （没有）"

echo
echo "【systemd 服务】"
if [ -n "$SVC_FOUND" ]; then
  for s in $SVC_FOUND; do echo "  $s"; done
else
  echo "  （没装）"
fi

echo
echo "【定时任务】"
CRON_HITS="$(crontab -l 2>/dev/null | grep -i "nodeseek\|multi_site_sign" || true)"
if [ -n "$CRON_HITS" ]; then
  echo "$CRON_HITS" | sed 's/^/  /'
else
  echo "  （没有）"
fi

echo
echo "【Docker 残留】"
if command -v docker >/dev/null 2>&1; then
  docker ps -a --format '  容器 {{.Names}} ({{.Image}})' 2>/dev/null | grep -i nodeseek || true
  docker images --format '  镜像 {{.Repository}}:{{.Tag}} ({{.Size}})' 2>/dev/null | grep -i nodeseek || true
else
  echo "  （没装 docker）"
fi

echo
echo "【备份内容（会保留下来）】"
BAK_ITEMS=""
[ -f "$APP_DIR/.env" ] && BAK_ITEMS="$BAK_ITEMS .env"
[ -d "$APP_DIR/cookie" ] && BAK_ITEMS="$BAK_ITEMS cookie/"
[ -n "$BAK_ITEMS" ] && echo "  $BAK_ITEMS" || echo "  （没有可备份的）"

echo
echo "【系统依赖（默认不动）】"
DEPS="xvfb libgtk-3-0 libdbus-glib-1-2 libxt6 libgl1 libgl1-mesa-dri libegl1 libgles2 fonts-noto-cjk fonts-wqy-zenhei"
if [ "$PURGE_DEPS" = "1" ]; then
  echo "  将卸载：$DEPS"
else
  echo "  $DEPS"
  echo "  （要卸请加 --purge-system-deps）"
fi

if [ "$YES" != "1" ]; then
  echo
  ylw "  以上是计划，没有删任何东西。"
  ylw "  确认后执行：bash uninstall.sh --yes"
  exit 0
fi

# ============================================================ 备份
step "备份配置与登录态"
STAMP="$(date +%Y%m%d-%H%M%S)"
# 备份目录：优先项目同级（一定能写），否则退回 HOME
BAK_DIR="$(dirname "$APP_DIR")"
[ -w "$BAK_DIR" ] || BAK_DIR="$HOME"
BAK="$BAK_DIR/nodeseek-backup-$STAMP.tar.gz"
BAK_LIST=""
[ -f "$APP_DIR/.env" ] && BAK_LIST="$BAK_LIST .env"
[ -d "$APP_DIR/cookie" ] && BAK_LIST="$BAK_LIST cookie"
if [ -n "$BAK_LIST" ]; then
  if tar -czf "$BAK" -C "$APP_DIR" $BAK_LIST 2>/dev/null; then
    chmod 600 "$BAK"
    grn "  已备份到 $BAK"
    ylw "  ⚠️ 里面是明文密码 + 登录 cookie，别放公开位置；明天在新机器上："
    echo "     tar -xzf $BAK -C /path/to/nodeseek-autosign/"
  else
    red "  备份失败！中止卸载（避免丢配置）"
    red "  手动检查：tar -czf $BAK -C $APP_DIR $BAK_LIST"
    exit 1
  fi
else
  ylw "  没有 .env / cookie 可备份"
fi

# ============================================================ 卸载
step "停止服务"
if [ -n "$SVC_FOUND" ]; then
  for s in $SVC_FOUND; do
    systemctl disable --now "$s" >/dev/null 2>&1
    rm -f "/etc/systemd/system/$s"
    grn "  已停用并移除 $s"
  done
  systemctl daemon-reload >/dev/null 2>&1
else
  echo "  （无 systemd 服务）"
fi

step "清理定时任务"
if crontab -l 2>/dev/null | grep -qi "nodeseek\|multi_site_sign"; then
  crontab -l 2>/dev/null | grep -iv "nodeseek\|multi_site_sign" | crontab - 2>/dev/null \
    && grn "  已移除本项目的 crontab 条目" || ylw "  crontab 修改失败，手动查：crontab -l"
else
  echo "  （无相关条目）"
fi

step "清理 Docker 残留"
if command -v docker >/dev/null 2>&1; then
  docker ps -aq --filter "name=nodeseek" | xargs -r docker rm -f >/dev/null 2>&1
  docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -i nodeseek | xargs -r docker rmi -f >/dev/null 2>&1
  echo "  已清理"
fi

step "清理全局 pip 浏览器引擎"
if [ -n "$PIP_ENGINES" ]; then
  for pkg in $PIP_ENGINES; do
    pip3 uninstall -y "$pkg" >/dev/null 2>&1 && grn "  已卸包：$pkg" || ylw "  卸包失败：$pkg"
  done
else
  echo "  （无）"
fi

step "删除浏览器本体"
for d in "$HOME/.cache/camoufox" "$HOME/.cache/ms-playwright" "$HOME/.cache/ms-playwright-chromium" "$HOME/.cache/patchright" "/opt/camoufox" "/opt/playwright"; do
  if [ -e "$d" ]; then
    s=$(sz "$d")
    # 安全阀：只允许删 ~/.cache 与 /opt 下的浏览器目录
    case "$d" in
      "$HOME"/.cache/*|/opt/camoufox|/opt/playwright)
        if rm -rf "$d" 2>/dev/null && [ ! -e "$d" ]; then
          grn "  已删 $d（${s}MB）"
        else
          red "  删不掉 $d（权限不足？用 sudo 重跑）"
        fi
        ;;
      *) ylw "  跳过（路径不在白名单）：$d" ;;
    esac
  fi
done

step "删除项目目录"
if [ "$APP_DIR" = "/" ] || [ "$APP_DIR" = "/root" ] || [ -z "$APP_DIR" ]; then
  red "  路径异常，中止：$APP_DIR"
  exit 1
fi
TMP="/tmp/nodeseek-uninstall-$(date +%s)"
if mv "$APP_DIR" "$TMP" 2>/dev/null; then
  grn "  已移出原位：$TMP"
  if rm -rf "$TMP" 2>/dev/null && [ ! -e "$TMP" ]; then
    grn "  已删除项目目录（含 .venv / cookie / profile）"
  else
    red "  目录已移走但删除失败，请手动删：rm -rf $TMP"
  fi
else
  # mv 失败（跨挂载点 / 权限）：直接原地删
  if rm -rf "$APP_DIR" 2>/dev/null && [ ! -e "$APP_DIR" ]; then
    grn "  已删除项目目录（含 .venv / cookie / profile）"
  else
    red "  删除项目目录失败，请手动处理：$APP_DIR"
    red "  （可能权限不足，用 sudo 跑一次本脚本）"
  fi
fi

step "可选：apt 浏览器包"
if [ -n "$APT_BROWSERS" ]; then
  APT_REMOVE="$(echo "$APT_BROWSERS" | cut -f1 | tr '\n' ' ')"
  DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq --purge $APT_REMOVE >/dev/null 2>&1
  grn "  已卸载：$APT_REMOVE"
else
  echo "  （无）"
fi

step "可选：系统依赖"
if [ "$PURGE_DEPS" = "1" ]; then
  PKGS=""
  for p in $DEPS; do apt-cache show "$p" >/dev/null 2>&1 && PKGS="$PKGS $p"; done
  if [ -n "$PKGS" ]; then
    DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq --purge $PKGS >/dev/null 2>&1 || true
    apt-get autoremove -y -qq >/dev/null 2>&1 || true
    grn "  已卸载系统依赖"
  fi
else
  echo "  跳过（要卸请加 --purge-system-deps）"
fi

if [ "$WITH_SWAP" = "1" ] && [ -f /swapfile ]; then
  swapoff /swapfile 2>/dev/null
  rm -f /swapfile
  sed -i '\|^/swapfile|d' /etc/fstab 2>/dev/null
  grn "  已删除 /swapfile 并清理 fstab"
fi

# ============================================================ 验证
step "验证是否清干净"
LEFTOVER=""
for s in $SVC_NAMES; do
  systemctl list-unit-files 2>/dev/null | grep -q "^$s" && LEFTOVER="$LEFTOVER $s.service"
done
[ -d "$APP_DIR" ] && LEFTOVER="$LEFTOVER $APP_DIR"
for d in "$HOME/.cache/camoufox" "$HOME/.cache/ms-playwright" "$HOME/.cache/patchright" "/opt/camoufox"; do
  [ -e "$d" ] && LEFTOVER="$LEFTOVER $d"
done
crontab -l 2>/dev/null | grep -qi "nodeseek\|multi_site_sign" && LEFTOVER="$LEFTOVER crontab"
docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qi nodeseek && LEFTOVER="$LEFTOVER docker"

if [ -n "$LEFTOVER" ]; then
  ylw "  仍有残留：$LEFTOVER"
  ylw "  手动确认后处理（服务: systemctl disable --now <name> && rm /etc/systemd/system/<name>）"
else
  grn "  干净了：无服务、无项目目录、无浏览器缓存、无定时任务、无容器"
fi

step "卸载完成"
grn "  这台机器已经清理完毕。"
echo
echo "  备份：$BAK"
echo "  ⚠️ 备份里是明文密码 + 登录 cookie，scp 走之后记得删掉原文件："
echo "    scp $BAK <新机器>:/root/    # 传走"
echo "    rm -f $BAK                 # 传走后删掉本机副本"
echo
echo "  明天在新机器上："
echo "    git clone https://github.com/EmersonLopez2005/nodeseek-autosign.git"
echo "    cd nodeseek-autosign && bash install_vps.sh"
echo "    tar -xzf $BAK -C \$PWD/      # 恢复 .env 和 cookie"
echo "    .venv/bin/python check_env.py"
