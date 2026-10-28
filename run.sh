#!/bin/bash
# dailycheckin 每日签到的 launchd 包装脚本
#
# 作用：
#   1. 用"当天 stamp 文件"做幂等判断，避免 RunAtLoad 重复执行
#   2. 让 launchd 的 ProgramArguments 只依赖 /bin/bash，不绑定任何解释器路径
#      （踩过的坑：ProgramArguments 直指 Homebrew 符号链接，brew 升级后 78: EX_CONFIG 且无日志）
#
# 触发路径：每天 08:30（StartCalendarInterval）+ 登录/开机（RunAtLoad）

set -uo pipefail

# 仓库根目录由脚本自身位置推导,不再写死绝对路径 —— 项目整体搬家后无需再改这里
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO/.venv/bin/python"
STATE_DIR="$HOME/.local/state/dailycheckin"
STAMP="$STATE_DIR/last_run"
TODAY="$(date +%Y-%m-%d)"
TS="$(date '+%Y-%m-%d %H:%M:%S')"

mkdir -p "$STATE_DIR"

if [ ! -x "$PY" ]; then
    echo "[$TS] 致命错误：解释器不存在或不可执行 $PY"
    exit 78
fi

if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$TODAY" ]; then
    echo "[$TS] 今天已执行过（stamp=${TODAY}），跳过"
    exit 0
fi

cd "$REPO" || {
    echo "[$TS] 致命错误：无法进入 $REPO"
    exit 78
}

echo "[$TS] ===== 开始执行签到 ====="
"$PY" -m dailycheckin.main 2>&1
RC=$?

if [ "$RC" -eq 0 ]; then
    echo "$TODAY" > "$STAMP"
    echo "[$TS] ===== 执行完成 rc=0，已写入 stamp ====="
else
    echo "[$TS] ===== 执行失败 rc=${RC}，不写 stamp，下次触发会重试 ====="
fi

exit "$RC"
