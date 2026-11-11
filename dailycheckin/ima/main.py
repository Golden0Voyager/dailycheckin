"""腾讯 ima 每日登录算力（ima.qq.com）。

三个接口串成一条链，和常见「带 Cookie 签个到」完全不同：

1. ``POST /cgi-bin/auth_login/refresh`` 用长期 refreshToken 换 2 小时 access token
2. ``POST /cgi-bin/daily_login_activity/get_activity_info`` 拿到一周七格状态
3. ``POST /cgi-bin/daily_login_activity/check_in`` 按 ``type`` 领奖（1=日签，2=满签）

两个容易踩的坑：

- **access token 只活 2 小时**，所以每次都要先 refresh，不能把 token 存进配置。
- **满签奖励不能用本地日历推断「今天是周日」**：服务端某一格 ``status=3`` 才是可领的
  唯一信号。周中漏签后补签会让满签格提前解锁，按星期几判断必然错过。

``refreshToken`` 是轮换型令牌。本插件跑在 Actions 里，配置来自 Secret 且只读，
所以**刻意不回写轮换结果**——只把「该轮换了」这件事报出来，让人去更新 Secret。
在本机跑（launchd 版）时才有落盘能力，两者分工不同，不要混。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from dailycheckin import CheckIn

BASE = "https://ima.qq.com"
REFRESH_URL = BASE + "/cgi-bin/auth_login/refresh"
ACTIVITY_INFO_URL = BASE + "/cgi-bin/daily_login_activity/get_activity_info"
CHECK_IN_URL = BASE + "/cgi-bin/daily_login_activity/check_in"
CHECKIN_PAGE = BASE + "/copilot-token-daily-checkin/pc"

# 对齐 ima 内置浏览器 H5 签到页的请求头形态。服务端不严格校验，
# 但缺少 Origin / Referer 时更容易撞上网关拦截页。
BASE_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": BASE,
    "Referer": CHECKIN_PAGE,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "from_browser_ima": "1",
    "extension_version": "5.17.2",
}

CHECKIN_DAILY = 1
CHECKIN_WEEKLY_BONUS = 2

STATUS_CLAIMED = 2
STATUS_AVAILABLE = 3
STATUS_TEXT = {0: "未知", 1: "已过期", 2: "已领取", 3: "可领取", 4: "未解锁"}

CODE_OK = 0
CODE_NOT_FULL_WEEK = 1001
CODE_ALREADY_CHECKED_IN = 1002
CODE_TRANSIENT = -1  # 限流 / 网络异常归一化码

BONUS_TITLE = "满签奖励"
MAX_RETRIES = 2
RETRY_DELAY = 20

REQUIRED_FIELDS = ("ima_uid", "uid_type", "ima_guid", "refresh_token")


class AuthExpiredError(RuntimeError):
    """refreshToken 已失效，需要重新微信扫码授权。"""


# ────────────────────────────── 签名与请求 ──────────────────────────────
def _to_int32(value):
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def bkn(token):
    """H5 页面 JS 的 djb2 变体，输出 x-ima-bkn 头。"""
    result = 5381
    for ch in token:
        result += _to_int32(_to_int32(result) << 5) + ord(ch)
    return _to_int32(result) & 0x7FFFFFFF


def post_json(url, body, headers, timeout=20):
    """POST 并永远返回 dict，不把异常抛给调用方。

    服务端连续拒绝后会回 HTTP 403 + 空 body；这类响应必须能接住，
    否则一次「试领失败」就会打断整个签到流程。
    """
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
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
        data = json.loads(raw)
    except ValueError:
        return {"code": CODE_TRANSIENT, "msg": f"HTTP {status} 非 JSON 响应：{raw[:120]}"}
    if not isinstance(data, dict):
        return {"code": CODE_TRANSIENT, "msg": f"响应格式异常：{type(data).__name__}"}
    data.setdefault("http_status", status)
    return data


def post_resilient(url, body, headers):
    """只对归一化码 -1（限流/网络）做有限重试，业务码立即返回。

    轮换型凭证与签到接口都不适合无脑重试，业务拒绝必须原样交给裁决层。
    """
    for _ in range(MAX_RETRIES + 1):
        resp = post_json(url, body, headers)
        if resp.get("code") != CODE_TRANSIENT:
            return resp
    return resp


def build_cookie(item, token):
    return "; ".join(
        [
            "IMA-UID={}".format(item["ima_uid"]),
            f"IMA-TOKEN={token}",
            "IMA-REFRESH-TOKEN={}".format(item["refresh_token"]),
            "UID-TYPE={}".format(item["uid_type"]),
            "IMA-GUID={}".format(item["ima_guid"]),
            "PLATFORM=H5",
        ]
    )


def auth_headers(cookie, token=None):
    headers = dict(BASE_HEADERS)
    headers["x-ima-cookie"] = cookie
    if token:
        headers["x-ima-bkn"] = str(bkn(token))
    return headers


def refresh_access_token(item):
    """用长期凭证换 2 小时 token。只打这一个确定端点，绝不做候选扫描。

    轮换型令牌的规矩：拿它扫候选 URL，第一个成功就轮换了令牌，
    后续请求会把响应覆盖掉，结果是自己把自己踢下线。
    """
    device_cookie = "IMA-GUID={}; PLATFORM=H5".format(item["ima_guid"])
    body = {"userId": item["ima_uid"], "refreshToken": item["refresh_token"], "tokenType": -1}
    data = post_json(REFRESH_URL, body, auth_headers(device_cookie))
    token = data.get("token")
    if data.get("code") != CODE_OK or not token:
        raise AuthExpiredError(
            "刷新登录失败 code={} msg={}（refreshToken 已失效，需重新微信扫码）".format(
                data.get("code"), data.get("msg")
            )
        )
    rotated = data.get("refreshToken") or data.get("refresh_token")
    if rotated and rotated != item.get("refresh_token"):
        # Actions 里配置只读，无法落盘；明说出来让人去更新 Secret，不要假装成功。
        raise AuthExpiredError("refreshToken 已被服务端轮换且无法写回 Secret，请在配置里更新新的 refresh_token")
    return token


def find_item(info, top):
    for one in info.get("infos") or []:
        if one.get("top") == top:
            return one
    return None


def reward_of(resp):
    if not isinstance(resp, dict):
        return None
    return resp.get("reward_received") or resp.get("rewardReceived")


def week_summary(info):
    marks = {1: "已过期", 2: "已领", 3: "可领", 4: "未解锁"}
    return "  ".join(
        "{}:{}".format(one.get("top") or "?", marks.get(one.get("status"), "?")) for one in info.get("infos") or []
    )


# ────────────────────────────── 插件本体 ──────────────────────────────
class Ima(CheckIn):
    name = "腾讯ima算力"

    def __init__(self, check_item):
        self.check_item = check_item

    def _headers(self):
        missing = [key for key in REQUIRED_FIELDS if not self.check_item.get(key)]
        if missing:
            raise RuntimeError("IMA 配置缺少字段：{}".format("、".join(missing)))
        token = refresh_access_token(self.check_item)
        return auth_headers(build_cookie(self.check_item, token), token)

    def _activity(self, headers):
        info = post_resilient(ACTIVITY_INFO_URL, {}, headers)
        if info.get("code") != CODE_OK:
            raise RuntimeError("查询活动失败 code={} msg={}".format(info.get("code"), info.get("msg")))
        if find_item(info, "今日") is None:
            raise RuntimeError("接口未返回「今日」条目，活动可能已改版，请人工核对客户端")
        return info

    def check_only(self):
        """只读自检：刷新 token + 拉活动信息，不触发任何领取。"""
        info = self._activity(self._headers())
        today = find_item(info, "今日") or {}
        bonus = find_item(info, BONUS_TITLE) or {}
        return {
            "checkin_days": info.get("checkin_days"),
            "total_reward_points": info.get("total_reward_points"),
            "today_status": STATUS_TEXT.get(today.get("status"), "未知"),
            "bonus_status": STATUS_TEXT.get(bonus.get("status"), "未知"),
            "week": week_summary(info),
        }

    def main(self):
        headers = self._headers()
        info = self._activity(headers)
        points_before = info.get("total_reward_points")
        today = find_item(info, "今日")
        status = today.get("status")

        lines = []
        claimed_daily = False
        if status == STATUS_AVAILABLE:
            resp = post_resilient(CHECK_IN_URL, {"type": CHECKIN_DAILY}, headers)
            code = resp.get("code")
            if code == CODE_OK:
                claimed_daily = True
                reward = reward_of(resp)
                lines.append(f"每日签到：+{reward} 算力" if reward else "每日签到：成功")
            elif code == CODE_ALREADY_CHECKED_IN:
                lines.append("每日签到：今日已签（幂等放行）")
            else:
                raise RuntimeError("每日签到被拒 code={} msg={}".format(code, resp.get("msg")))
        elif status == STATUS_CLAIMED:
            lines.append("每日签到：今天已签到")
        else:
            raise RuntimeError(
                "今日格状态异常（status={} {}），需人工核查".format(status, STATUS_TEXT.get(status, "未知"))
            )

        # 领取后必须重拉：既核对到账，也拿到刚解锁的满签格
        info = self._activity(headers)
        if claimed_daily:
            after = info.get("total_reward_points")
            if isinstance(points_before, int) and isinstance(after, int):
                verdict = "已增加" if after > points_before else "累计算力未变化（可能统计延迟）"
                lines.append(f"到账核对：{verdict}")

        # 满签奖励只看服务端 status=3，不按本地日历锚定周日
        bonus = find_item(info, BONUS_TITLE)
        bonus_status = bonus.get("status") if bonus else None
        if bonus_status == STATUS_AVAILABLE:
            resp = post_resilient(CHECK_IN_URL, {"type": CHECKIN_WEEKLY_BONUS}, headers)
            code = resp.get("code")
            if code == CODE_OK:
                reward = reward_of(resp)
                lines.append(f"满签奖励：+{reward} 算力" if reward else "满签奖励：已领取")
                info = self._activity(headers)
            elif code == CODE_NOT_FULL_WEEK:
                lines.append("满签奖励：本周未签满")
            elif code == CODE_ALREADY_CHECKED_IN:
                lines.append("满签奖励：本期已领取")
            else:
                raise RuntimeError("满签奖励试领未成功 code={} msg={}".format(code, resp.get("msg")))
        elif bonus_status == STATUS_CLAIMED:
            lines.append("满签奖励：本期已领取")

        lines.append("累计：已签到 {} 天，共 {} 算力".format(info.get("checkin_days"), info.get("total_reward_points")))
        lines.append(f"本周进度：{week_summary(info)}")
        return "\n".join(lines)


if __name__ == "__main__":
    import os

    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(_root, "config.json"), encoding="utf-8") as f:
        _datas = json.loads(f.read())
    print(Ima(check_item=_datas.get("IMA", [])[0]).main())
