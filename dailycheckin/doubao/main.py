"""豆包（doubao.com）每日签到额度。

这家的签到入口藏在一个激励活动里，日常入口是 ``/pc/agent-explore`` 页面的任务卡片：

1. ``POST /alice/ug/campaign/v3/detail/`` 拉活动详情（含今日是否已签、里程碑进度）
2. ``POST /alice/ug/campaign/landing/checkin`` 签到，body ``{"scene": <活动组>}``
3. ``POST /alice/ug/campaign/item/reward/claim/`` 领里程碑奖励

判读要点，都是实测踩出来的：

- 签到响应里的 ``idempotent=true`` 表示服务端当天已处理过，**不是失败**；
  外层若把它当异常就会天天误报。
- 里程碑 ``task_status``：1 未达成 / 2 可领取 / 3 已完成。只领 2。
- 领取响应可能带 ``data.biz_error``（「确认中」），这是正常中间态，
  **不能判失败**，下一趟再确认即可。
- 额度 30 天有效，不像短期魔粒那样 24 小时过期，所以可以放心用余额对账。

``app_id`` / 活动组编码属于前端常量，会随版本变。变更时 detail 接口会返回
非鉴权类错误码，本插件会把它当异常抛出，不会静默吞成「登录态失效」。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from dailycheckin import CheckIn

BASE = "https://www.doubao.com"
DETAIL_URL = BASE + "/alice/ug/campaign/v3/detail/"
CHECKIN_URL = BASE + "/alice/ug/campaign/landing/checkin"
CLAIM_URL = BASE + "/alice/ug/campaign/item/reward/claim/"

AID = "497858"  # 豆包网页版 aid
GROUP_CODE = "pc_incentive_task_mvp_v3"  # 活动组编码，同时作为签到 scene
SIGNIN_TASK_CODE = "daily_signin"

STATUS_LOCKED = 1
STATUS_CLAIMABLE = 2
STATUS_DONE = 3
STATUS_TEXT = {0: "未知", 1: "未达成", 2: "可领取", 3: "已完成"}

CODE_OK = 0
CODE_TRANSIENT = -1  # 网络异常 / 空响应 / 非 JSON，归一化码
CODE_NO_SESSION = 2  # 会话无效（未登录或 Cookie 已过期）
CODE_LOGIN_EXPIRED = 710012001
AUTH_CODES = (CODE_NO_SESSION, CODE_LOGIN_EXPIRED)

MAX_RETRIES = 2
MAX_CLAIM_ROUNDS = 5

BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
    "Origin": BASE,
    "Referer": BASE + "/pc/agent-explore",
}


class NeedAuthError(RuntimeError):
    """Cookie 失效，需要重新登录豆包并更新配置。"""


# ────────────────────────────── 请求 ──────────────────────────────
def _query(aid=AID):
    return urllib.parse.urlencode(
        {
            "aid": aid,
            "real_aid": aid,
            "device_platform": "web",
            "doubao_device_platform": "web",
            "language": "zh",
            "pkg_type": "release_version",
        }
    )


def api_url(path):
    return f"{path}?{_query()}"


def post_json(url, body, cookie_header="", timeout=20):
    """POST 并永远返回 dict，网络/解析失败归一化为 CODE_TRANSIENT。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = dict(BASE_HEADERS)
    if cookie_header:
        headers["Cookie"] = cookie_header
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
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

    if not raw.strip():
        return {"code": CODE_TRANSIENT, "msg": f"HTTP {status} 空响应（可能被限流）"}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"code": CODE_TRANSIENT, "msg": f"HTTP {status} 非 JSON 响应：{raw[:120]}"}
    if not isinstance(parsed, dict):
        return {"code": CODE_TRANSIENT, "msg": f"响应格式异常：{type(parsed).__name__}"}
    parsed.setdefault("http_status", status)
    return parsed


def post_resilient(url, body, cookie_header):
    for _ in range(MAX_RETRIES + 1):
        resp = post_json(url, body, cookie_header)
        if resp.get("code") != CODE_TRANSIENT:
            return resp
    return resp


# ────────────────────────────── 活动数据解析 ──────────────────────────────
def find_signin_task(detail):
    for task in detail.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if task.get("task_kind") == SIGNIN_TASK_CODE or task.get("campaign_code") == SIGNIN_TASK_CODE:
            return task
    return None


def signin_state(detail):
    """只信服务端字段，不从日期文案推断。"""
    task = find_signin_task(detail)
    if not task:
        return None
    info = task.get("daily_checkin") or {}
    checked = info.get("checked_in_today")
    if checked is None:
        checked = task.get("task_status") == STATUS_DONE
    return {
        "checked_in_today": bool(checked),
        "continuous_days": info.get("continuous_days"),
        "total_days": info.get("total_days"),
    }


def total_amount(detail):
    progress = detail.get("progress") or {}
    value = progress.get("total_amount")
    return value if isinstance(value, (int, float)) else None


def milestones(detail):
    out = []
    for item in detail.get("milestones") or []:
        if not isinstance(item, dict):
            continue
        rewards = item.get("reward_summaries") or []
        amount = rewards[0].get("reward_amount") if rewards and isinstance(rewards[0], dict) else None
        out.append(
            {
                "code": item.get("campaign_code"),
                "threshold": item.get("milestone_threshold"),
                "reward": amount,
                "task_status": item.get("task_status"),
            }
        )
    return out


def claimable_milestones(detail):
    return [m for m in milestones(detail) if m.get("task_status") == STATUS_CLAIMABLE and m.get("code")]


def progress_text(detail):
    progress = detail.get("progress") or {}
    total = total_amount(detail)
    parts = []
    if progress.get("continuous_days") is not None:
        parts.append("连续 {} 天".format(progress.get("continuous_days")))
    if total is not None:
        parts.append(f"累计 {total} 点额度")
    # 里程碑列表不保证按阈值升序，必须自己挑「下一个未达成的最小阈值」，
    # 否则会报出「距 5000 点还差 4800」这种没用的话。
    if isinstance(total, (int, float)):
        pending = [
            m["threshold"]
            for m in milestones(detail)
            if m.get("task_status") != STATUS_DONE
            and isinstance(m.get("threshold"), (int, float))
            and m["threshold"] > total
        ]
        if pending:
            nxt = min(pending)
            parts.append(f"距 {nxt} 点还差 {nxt - total}")
    return "，".join(parts)


# ────────────────────────────── 插件本体 ──────────────────────────────
class Doubao(CheckIn):
    name = "豆包额度"

    def __init__(self, check_item):
        self.check_item = check_item

    def _cookie(self):
        cookie = (self.check_item.get("cookie") or "").strip()
        if not cookie:
            raise RuntimeError("DOUBAO 配置缺少 cookie：需豆包网页版登录态整串")
        return cookie

    def _detail(self, cookie):
        resp = post_resilient(api_url(DETAIL_URL), {"campaign_group_code": GROUP_CODE}, cookie)
        code = resp.get("code")
        if code in AUTH_CODES:
            # 带上服务端原文：会话无效可能来自 Cookie 过期、也可能来自异地/机房 IP 风控，
            # 两者的处置方式完全不同，不能只报一个 code 让人去重登。
            raise NeedAuthError(
                "登录态失效 code={} http={} msg={}（同一 Cookie 在住宅 IP 可用则为环境风控）".format(
                    code, resp.get("http_status"), resp.get("msg") or "无返回信息"
                )
            )
        if code != CODE_OK or not isinstance(resp.get("data"), dict):
            raise RuntimeError("拉取活动详情失败 code={} msg={}（活动接口可能已改版）".format(code, resp.get("msg")))
        return resp["data"]

    def check_only(self):
        """只读自检：拉详情，不签到、不领奖。"""
        detail = self._detail(self._cookie())
        state = signin_state(detail) or {}
        return {
            "checked_in_today": state.get("checked_in_today"),
            "continuous_days": state.get("continuous_days"),
            "total_amount": total_amount(detail),
            "claimable": [m["threshold"] for m in claimable_milestones(detail)],
            "progress": progress_text(detail),
        }

    def _claim(self, cookie, detail, lines):
        """领完所有 task_status=2 的里程碑；biz_error（确认中）留待下一趟。"""
        tried = set()
        for _ in range(MAX_CLAIM_ROUNDS):
            pending = [m for m in claimable_milestones(detail) if m["code"] not in tried]
            if not pending:
                return detail
            target = pending[0]
            tried.add(target["code"])
            resp = post_resilient(
                api_url(CLAIM_URL),
                {"campaign_group_code": GROUP_CODE, "campaign_item_code": target["code"]},
                cookie,
            )
            code = resp.get("code")
            if code in AUTH_CODES:
                raise NeedAuthError(f"领取奖励时登录态失效 code={code}")
            if code != CODE_OK:
                lines.append("里程碑 {} 点：领取被拒 code={}".format(target["threshold"], code))
                return detail
            data = resp.get("data") or {}
            biz = data.get("biz_error") if isinstance(data, dict) else None
            if biz:
                lines.append(
                    "里程碑 {} 点：{}（下一趟再确认）".format(target["threshold"], biz.get("message") or "确认中")
                )
                return detail
            lines.append("里程碑 {} 点：已领取 +{} 额度".format(target["threshold"], target.get("reward") or 0))
            detail = self._detail(cookie)
        return detail

    def main(self):
        cookie = self._cookie()
        detail = self._detail(cookie)
        state = signin_state(detail) or {}
        if not state:
            raise RuntimeError("活动详情里没有找到「每日签到」任务，活动可能已改版")

        lines = []
        total_before = total_amount(detail)
        if state.get("checked_in_today"):
            lines.append("每日签到：今天已签到")
        else:
            resp = post_resilient(api_url(CHECKIN_URL), {"scene": GROUP_CODE}, cookie)
            code = resp.get("code")
            if code in AUTH_CODES:
                raise NeedAuthError(f"签到时登录态失效 code={code}")
            if code != CODE_OK:
                raise RuntimeError("签到被拒 code={} msg={}".format(code, resp.get("msg")))
            data = resp.get("data") or {}
            if data.get("idempotent") is True or data.get("checked_in") is False:
                lines.append("每日签到：今日已签（服务端幂等放行）")
            else:
                reward = data.get("reward_amount") or 0
                lines.append(f"每日签到：成功 +{reward} 额度")

        # 复核：重拉详情，既确认到账，也拿到刚解锁的里程碑
        detail = self._detail(cookie)
        total_after = total_amount(detail)
        if (
            isinstance(total_before, (int, float))
            and isinstance(total_after, (int, float))
            and total_after < total_before
        ):
            lines.append("到账核对：累计额度减少（可能有额度到期，非签到失败）")

        detail = self._claim(cookie, detail, lines)

        state = signin_state(detail) or {}
        lines.append(
            "累计：连续 {} 天，已签 {} 天，共 {} 点额度".format(
                state.get("continuous_days"), state.get("total_days"), total_amount(detail)
            )
        )
        lines.append(f"进度：{progress_text(detail)}")
        return "\n".join(lines)


if __name__ == "__main__":
    import os

    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(_root, "config.json"), encoding="utf-8") as f:
        _datas = json.loads(f.read())
    print(Doubao(check_item=_datas.get("DOUBAO", [])[0]).main())
