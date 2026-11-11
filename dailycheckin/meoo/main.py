"""秒悟 Meoo（meoo.com）每日登录积分。

和 modelscope 一样，它**没有签到按钮，也没有签到接口**：
积分走的是「当天首次带登录态调用 entitlement/init」这个后端副作用，
服务端按天去重，重复调用返回 ``dailyLoginSkipReason = ALREADY_GRANTED_TODAY``。

因此这里的「打卡」= 请求一次 ``GET /api/mc/entitlement/init``，
响应自带对账字段（是否发放 / 发放数量 / 发放后余额），不需要额外查表。

注意：调用该接口本身就会触发发放，所以本插件**不提供 check_only()**——
不存在"只读不触发"的读法。要巡检只能用 Bark 里的历史记录反推。
"""

from __future__ import annotations

import requests

from dailycheckin import CheckIn

BASE = "https://meoo.com"
ENTITLEMENT_URL = f"{BASE}/api/mc/entitlement/init"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


class Meoo(CheckIn):
    name = "秒悟Meoo积分"

    def __init__(self, check_item):
        self.check_item = check_item

    # ------------------------------------------------------------------ #
    def main(self):
        session = self._build_session()
        payload = self._api(session, ENTITLEMENT_URL)
        data = payload.get("data") or {}

        amount = data.get("dailyLoginGrantAmount") or 0
        balance = data.get("dailyLoginBalanceAfter")
        skip_reason = data.get("dailyLoginSkipReason")

        msg = [
            {"name": "帐号类型", "value": data.get("loginType") or "未知"},
            {"name": "套餐", "value": data.get("currentPlanCode") or "未知"},
        ]

        if data.get("dailyLoginGranted"):
            verdict = f"+{amount} 已到账"
        elif skip_reason == "ALREADY_GRANTED_TODAY":
            verdict = "+0（今日已发放过，重复调用不重复加分）"
        elif skip_reason:
            verdict = f"+0（未发放，原因 {skip_reason}）"
        else:
            verdict = "+0（未触发发放，登录态可能已失效）"

        msg.append({"name": "本次打卡", "value": verdict})
        if balance is not None:
            msg.append({"name": "积分余额", "value": f"{balance}"})

        return "\n".join([f"{one.get('name')}: {one.get('value')}" for one in msg])

    # ------------------------------------------------------------------ #
    def _build_session(self) -> requests.Session:
        cookie_str = (self.check_item.get("cookie") or "").strip()
        if not cookie_str:
            raise RuntimeError("缺少 cookie：请在 config.json 的 MEEO 里填 meoo.com 的 Cookie 整串")

        session = requests.Session()
        session.headers.update(
            {
                "user-agent": USER_AGENT,
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "referer": f"{BASE}/",
            }
        )
        proxy = self.check_item.get("proxy", "")
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})

        for raw in cookie_str.split(";"):
            item = raw.strip()
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            session.cookies.set(key.strip(), value.strip(), domain=".meoo.com")
        return session

    @staticmethod
    def _api(session: requests.Session, url: str) -> dict:
        resp = session.get(url, timeout=30)
        if resp.status_code in (401, 403):
            raise RuntimeError(f"HTTP {resp.status_code}：登录态已失效，请重新获取 cookie")
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}：{resp.text[:150]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"响应不是 JSON：{resp.text[:150]}") from exc

        if str(payload.get("errCode") or "") == "NEED_LOGIN":
            raise RuntimeError("NEED_LOGIN：登录态已失效，请重新获取 cookie")
        if not payload.get("success"):
            detail = (payload.get("error") or {}).get("message") or payload.get("errCode")
            raise RuntimeError(f"接口返回失败：{detail or str(payload)[:150]}")
        return payload


if __name__ == "__main__":
    import json
    import os

    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(_root, "config.json"), encoding="utf-8") as f:
        datas = json.loads(f.read())
    _check_item = datas.get("MEOO", [])[0]
    print(Meoo(check_item=_check_item).main())
