#!/bin/bash
# 华为云 CodeArts 签到 —— launchd 包装脚本
# 用 /bin/bash 入口，运行时解析 Python，避免 launchd 缓存 Homebrew 符号链接导致 EX_CONFIG 78
# 输出单行 JSON 到 stdout（launchd StandardOutPath），错误到 stderr
cd /Users/hainingyu/code/auto_checkin/dailycheckin || exit 4
PY="/Users/hainingyu/code/auto_checkin/dailycheckin/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" codearts_checkin.py --json
