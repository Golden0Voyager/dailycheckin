"""WPS 灵犀（lingxi.kdocs.cn）每日签到智点。

三个接口，纯 Cookie 鉴权，没有签名参数，也没有 refreshToken 轮换：

1. ``GET  /api/public/v1/tasks`` —— 任务面板，``daily_check_in`` 的 status 是
   ``claimed`` / ``available``；``extra.check_in_days[]`` 是 7 天一轮的周期表
2. ``POST /api/public/v1/tasks/daily_check_in/claim?date=YYYY-MM-DD`` —— 领奖
3. ``GET  /api/public/v1/credits/balance`` —— 智点余额（旁路，失败不阻断）

几个实测口径，照抄别的站点会踩坑：

- **幂等性没有被验证过**：``status=claimed`` 时再 POST 就是碰风控。所以必须先查
  ``/tasks``，只有 ``available`` 才能领；本插件绝不盲打 claim，也不对 claim 做重试。
- **``date`` 必须按北京时间**：Actions runner 是 UTC，直接用本地日会在 00:00–08:00
  领到「昨天」的签到。
- **``reward_amount`` 是今日额度，``total_claimed_credits`` 是 7 天周期累计**。
  连签第 3 天累计返回 300，拿它当当天战果会虚报 3 倍。当天额优先读
  ``extra.today_reward``，再退 ``reward_amount``。
- 奖励 100 智点/天，第 7 天 200；断签会重置周期，所以「电脑没开机也跑」很重要。
- **顶层 ``status`` 不总是 ``claimed`` / ``available``**：2026-11-14 实测服务端会返回
  ``incomplete``（疑为「7 天周期未完成」的聚合语义），此时 ``extra.check_in_days`` 里
  ``today_day`` 对应那一格才是准确的当日状态。见 :func:`resolve_status`。

凭据是 ``wps_sid`` 为核心的 Cookie 整串，实测有效期约 364 天、接口不会自动续期。
过期时从浏览器重新导出并更新 Secret 即可，不存在轮换问题，多通道并行是安全的。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from dailycheckin import CheckIn

BASE = "https://lingxi.kdocs.cn"
TASKS_URL = BASE + "/api/public/v1/tasks"
CLAIM_URL = BASE + "/api/public/v1/tasks/daily_check_in/claim"
BALANCE_URL = BASE + "/api/public/v1/credits/balance"

TASK_KEY = "daily_check_in"
STATUS_CLAIMED = "claimed"
STATUS_AVAILABLE = "available"
STATUS_TEXT = {STATUS_CLAIMED: "今日已签", STATUS_AVAILABLE: "可领取"}

# 已知「顶层是周期聚合态、当日状态要看格子」的取值白名单。
# 只放实测确认过的别名：服务端若冒出 suspended / banned 之类新状态，
# 一律保守拒绝，绝不因为格子恰好是 available 就误发 POST。
AGGREGATE_STATUSES = ("incomplete",)

CST = timezone(timedelta(hours=8))
CODE_TRANSIENT = -1  # 网络异常 / 空响应 / 非 JSON，归一化码
MAX_READ_RETRIES = 2

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE + "/",
    "Origin": BASE,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
}


class NeedAuthError(RuntimeError):
    """Cookie 失效（401/403），需要重新导出 WPS 会话并更新 Secret。"""


def today_cst():
    """服务端按北京时间判日界，Actions runner 是 UTC，不能直接用本地日。"""
    return datetime.now(CST).strftime("%Y-%m-%d")


# ────────────────────────────── 请求 ──────────────────────────────
def request_json(url, cookie, method="GET", timeout=20):
    """发出请求并永远返回 dict；网络/解析问题归一化为 CODE_TRANSIENT。"""
    headers = dict(BASE_HEADERS)
    headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers, method=method)
    status, raw = 0, ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return {"code": CODE_TRANSIENT, "msg": f"网络错误：{exc.reason}"}
    except OSError as exc:
        return {"code": CODE_TRANSIENT, "msg": f"请求失败：{exc}"}

    if status in (401, 403):
        raise NeedAuthError(f"灵犀会话已失效（HTTP {status}），请重新导出 wps_sid 并更新 Secret")
    if not raw.strip():
        return {"code": CODE_TRANSIENT, "msg": f"HTTP {status} 空响应"}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"code": CODE_TRANSIENT, "msg": f"HTTP {status} 非 JSON 响应：{raw[:120]}"}
    if not isinstance(parsed, dict):
        return {"code": CODE_TRANSIENT, "msg": f"响应格式异常：{type(parsed).__name__}"}
    parsed.setdefault("http_status", status)
    return parsed


def read_resilient(url, cookie):
    """只读接口可以有限重试；写接口绝不能用这个（幂等未验证）。"""
    resp = {}
    for _ in range(MAX_READ_RETRIES + 1):
        resp = request_json(url, cookie)
        if resp.get("code") != CODE_TRANSIENT:
            return resp
    return resp


# ────────────────────────────── 任务面板解析 ──────────────────────────────
def find_daily_task(tasks):
    for task in tasks or []:
        if isinstance(task, dict) and task.get("task_key") == TASK_KEY:
            return task
    return None


def resolve_status(task):
    """归一当日签到状态，返回 ``(status, source)``。

    服务端 2026-11-14 起会把顶层 ``status`` 返回成 ``incomplete`` —— 这是
    「7 天周期未完成」的聚合语义，不代表当日不可领。真正准确的当日状态在
    ``extra.check_in_days`` 里 ``extra.today_day`` 对应的那一格。

    判定优先级（幂等边界不变，只有明确 ``available`` 才允许 POST）：

    1. 顶层是 ``claimed`` / ``available`` → 直接采信，历史行为不变。
    2. 顶层是 :data:`AGGREGATE_STATUSES` 里的已知聚合态 → 回退到当日格子；
       格子明确 ``available`` / ``claimed`` 才采信。
    3. 顶层是其它未知值（``suspended`` / ``banned`` / 拼错……）→ **不回退**，
       原样返回，交给调用方保守拒绝。宁可漏签也不碰风控。
    4. 格子缺失、``today_day`` 缺失或格子状态也是未知值 → 原样返回顶层未知值。

    ``source`` 只用于日志说明判定依据，不参与判定。
    """
    top = task.get("status")
    if top in (STATUS_CLAIMED, STATUS_AVAILABLE):
        return top, "顶层 status"
    if top not in AGGREGATE_STATUSES:
        return top, "未知顶层状态（非白名单，不回退）"

    extra = task.get("extra") or {}
    today_day = extra.get("today_day")
    days = extra.get("check_in_days") or []
    cell = None
    if isinstance(today_day, int) and not isinstance(today_day, bool):
        for day in days:
            if isinstance(day, dict) and day.get("day") == today_day:
                cell = day
                break
        if cell is None and 1 <= today_day <= len(days):
            candidate = days[today_day - 1]
            if isinstance(candidate, dict):
                cell = candidate

    if cell:
        cell_status = cell.get("status")
        if cell_status in (STATUS_CLAIMED, STATUS_AVAILABLE):
            return cell_status, f"当日格子（today_day={today_day}）"
    return top, "未知状态（顶层与当日格子均无法确认）"


def today_reward(task):
    """当日额度优先读服务端直接字段，不拿周期累计推算。"""
    extra = task.get("extra") or {}
    for value in (extra.get("today_reward"), task.get("reward_amount")):
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    # 兜底：按已签天数定位本轮第 N 天的 reward
    days = extra.get("check_in_days") or []
    streak = sum(1 for day in days if day.get("status") == STATUS_CLAIMED)
    if 0 <= streak < len(days) and isinstance(days[streak].get("reward"), (int, float)):
        return int(days[streak]["reward"])
    return 100


def progress_text(task):
    extra = task.get("extra") or {}
    parts = []
    if extra.get("consecutive_days") is not None:
        parts.append(f"连续 {extra['consecutive_days']} 天")
    if extra.get("today_day") is not None:
        parts.append(f"本轮第 {extra['today_day']}/7 天")
    days = extra.get("check_in_days") or []
    upcoming = [
        d
        for d in days
        if d.get("status") != STATUS_CLAIMED and isinstance(d.get("reward"), (int, float)) and d["reward"] > 100
    ]
    if upcoming:
        parts.append(f"第 {upcoming[0].get('day')} 天可领 {upcoming[0].get('reward')} 智点")
    return "，".join(parts)


def balance_of(cookie):
    """余额是旁路信息，拿不到不影响签到判定。"""
    try:
        resp = read_resilient(BALANCE_URL, cookie)
    except NeedAuthError:
        return None
    data = resp.get("data") or {}
    for key in ("total_balance", "total_bonus"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


# ────────────────────────────── 插件本体 ──────────────────────────────
class Lingxi(CheckIn):
    name = "WPS灵犀签到"

    def __init__(self, check_item):
        self.check_item = check_item

    def _cookie(self):
        cookie = (self.check_item.get("cookie") or "").strip()
        if not cookie:
            raise RuntimeError("LINGXI 配置缺少 cookie：需 WPS 会话整串（含 wps_sid）")
        if "wps_sid" not in cookie:
            raise RuntimeError("LINGXI cookie 里没有 wps_sid，请重新从浏览器导出完整会话")
        return cookie

    def _tasks(self, cookie):
        resp = read_resilient(TASKS_URL, cookie)
        code = resp.get("code")
        if code == CODE_TRANSIENT:
            raise RuntimeError(f"拉取任务面板失败：{resp.get('msg')}")
        if resp.get("result") != "ok":
            raise RuntimeError(
                f"任务面板返回异常 HTTP {resp.get('http_status')} result={resp.get('result')} "
                f"msg={resp.get('message') or resp.get('msg')}"
            )
        data = resp.get("data") or {}
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            raise RuntimeError("任务面板缺少 data.tasks，接口可能已改版")
        task = find_daily_task(tasks)
        if not task:
            raise RuntimeError("任务面板里没有 daily_check_in 任务，签到入口可能已下线")
        return task, data

    def check_only(self):
        """只读自检：查任务面板与余额，不触发任何领取。"""
        cookie = self._cookie()
        task, data = self._tasks(cookie)
        extra = task.get("extra") or {}
        effective, source = resolve_status(task)
        return {
            "status": task.get("status"),
            "effective_status": effective,
            "status_source": source,
            "status_text": STATUS_TEXT.get(effective, "未知"),
            "today_reward": today_reward(task),
            "consecutive_days": extra.get("consecutive_days"),
            "today_day": extra.get("today_day"),
            "cycle_total_claimed": data.get("total_claimed_credits"),
            "balance": balance_of(cookie),
            "progress": progress_text(task),
        }

    def main(self):
        cookie = self._cookie()
        task, data = self._tasks(cookie)
        status, source = resolve_status(task)
        reward = today_reward(task)
        lines = []

        # 顶层是未知值时才需要说明回退依据，正常路径不往 Bark 里加噪声。
        if source != "顶层 status":
            lines.append(f"状态判定：顶层 {task.get('status')} → 按{source}取 {status}")

        if status == STATUS_CLAIMED:
            lines.append("每日签到：今天已签到")
        elif status == STATUS_AVAILABLE:
            # 幂等未验证：唯一一次 POST，不重试、不并发，date 按北京时间。
            url = f"{CLAIM_URL}?date={today_cst()}"
            resp = request_json(url, cookie, method="POST")
            code = resp.get("code")
            if code == CODE_TRANSIENT:
                raise RuntimeError(f"签到请求失败：{resp.get('msg')}（服务端可能已受理，下趟先查再决定）")
            # 只认 result=ok 这一个成功信号。接口实测返回 {"result":"ok","data":...}；
            # 少一个字段宁可判失败，也不能把「可能被风控拒绝」谎报成签到成功。
            if resp.get("result") != "ok":
                raise RuntimeError(
                    f"签到被拒 HTTP {resp.get('http_status')} result={resp.get('result')} "
                    f"code={resp.get('code')} msg={resp.get('message') or resp.get('msg')}"
                )
            lines.append(f"每日签到：成功 +{reward} 智点")

            # 回查：领取返回 ok 不代表状态真的翻转，必须重拉面板确认。
            # 顶层可能仍是 incomplete（周期未完成），所以回查同样走 resolve_status，
            # 否则会误判 UNCONFIRMED，下一趟重复 POST 反而碰风控。
            task, data = self._tasks(cookie)
            confirmed, confirmed_source = resolve_status(task)
            if confirmed != STATUS_CLAIMED:
                raise RuntimeError("签到已受理但状态未翻转为 claimed（UNCONFIRMED），请人工核对，勿连续重跑")
            lines.append(f"到账核对：状态已翻转为已签到（依据{confirmed_source}）")
        else:
            raise RuntimeError(f"签到状态异常（status={status}），为避免碰风控，本趟不发起领取")

        lines.append(f"周期进度：{progress_text(task)}")
        balance = balance_of(cookie)
        if balance is not None:
            lines.append(f"智点余额：{balance}")
        lines.append(f"本轮累计已领：{data.get('total_claimed_credits')} 智点（7 天周期累计，非当日额）")
        return "\n".join(lines)


if __name__ == "__main__":
    import os

    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(_root, "config.json"), encoding="utf-8") as f:
        _datas = json.loads(f.read())
    print(Lingxi(check_item=_datas.get("LINGXI", [])[0]).main())
