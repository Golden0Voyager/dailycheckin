"""魔搭社区（ModelScope）每日登录魔粒。

和别的站点不一样：**魔粒没有签到接口，也没有签到按钮。**
它走的是「当天首次带登录态访问站点」这个后端会话副作用 —— 服务端自己记账 +200
（已绑阿里云账号再 +50），按天去重。所以「签到」这个动作本身就是打开页面，
本插件只做：带 cookie 访问一次用量页 → 读规则表核对到底有没有到账。

规则表 ``/openapi/v1/magicubes/earn/rules`` 每条规则都带 ``today_earned_amount``，
额度变化也从这里读，不写死 200 / 50。
"""

from __future__ import annotations

import requests

from dailycheckin import CheckIn

BASE = "https://www.modelscope.cn"
TRIGGER_URL = f"{BASE}/magicube/usage"
ME_URL = f"{BASE}/openapi/v1/users/me"
RULES_URL = f"{BASE}/openapi/v1/magicubes/earn/rules"
BALANCE_URL = f"{BASE}/openapi/v1/magicubes/balance"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

FROM_CHROME = "from-chrome"


class Modelscope(CheckIn):
    name = "魔搭社区魔粒"

    def __init__(self, check_item):
        self.check_item = check_item

    # ------------------------------------------------------------------ #
    def main(self):
        """正常打卡：访问站点触发发放，再核对增量。"""
        return self._run(visit=True)

    def check_only(self):
        """只读自检：不访问站点、不触发发放，只看今天到底到账了多少。

        给「签到巡检」用 —— 判断依据是规则表里的 today_earned_amount，
        而不是只看 Actions 是不是绿的（绿只代表没抛异常）。
        """
        return self._run(visit=False)

    def _run(self, visit: bool):
        session = self._build_session()

        profile = self._api(session, ME_URL).get("data") or {}
        account = profile.get("username") or "未知"
        nickname = profile.get("nickname") or ""
        who = f"{account}（{nickname}）" if nickname else account

        before = self._daily_snapshot(session)

        if visit:
            # 这一步就是「签到」本身：带登录态访问站点，后端自动发放当日额度。
            resp = session.get(TRIGGER_URL, timeout=30, allow_redirects=True)
            if resp.status_code != 200:
                raise RuntimeError(f"访问用量页失败 HTTP {resp.status_code}，cookie 可能过期")
            after = self._daily_snapshot(session)
            delta = after["earned"] - before["earned"]
        else:
            after = before
            delta = 0

        balance = (self._api(session, BALANCE_URL).get("data") or {}).get("available_balance")

        msg = [{"name": "帐号信息", "value": who}]
        for rule in after["rules"]:
            amount = rule.get("amount") or 0
            got = rule.get("today_earned_amount") or 0
            msg.append(
                {"name": rule.get("title") or rule.get("rule_key"), "value": f"{got}/{amount} 魔粒"}
            )

        if visit:
            if delta > 0:
                verdict = f"+{delta} 已到账"
            elif after["earned"] > 0:
                verdict = "+0（今日已发放过，重复访问不会重复加分）"
            else:
                verdict = "+0（未触发发放，检查登录态是否仍有效）"
            msg.append({"name": "本次打卡", "value": verdict})
        else:
            msg.append({"name": "自检", "value": self._self_check_verdict(after)})

        msg.append({"name": "今日登录合计", "value": f"{after['earned']}/{after['cap']} 魔粒"})
        if balance is not None:
            msg.append({"name": "魔粒余额", "value": f"{balance}"})

        return "\n".join([f"{one.get('name')}: {one.get('value')}" for one in msg])

    @staticmethod
    def _self_check_verdict(snapshot: dict) -> str:
        earned, cap = snapshot["earned"], snapshot["cap"]
        if cap and earned >= cap:
            return "正常，今日额度已拿满"
        if earned > 0:
            return f"部分到账 {earned}/{cap}，未拿满"
        return "异常：今日 0 发放，登录态可能已失效或工作流未跑"

    # ------------------------------------------------------------------ #
    def _build_session(self) -> requests.Session:
        cookie_str = (self.check_item.get("cookie") or "").strip()
        if not cookie_str or cookie_str == FROM_CHROME:
            cookie_str = self._cookie_from_chrome()

        session = requests.Session()
        session.headers.update(
            {
                "user-agent": USER_AGENT,
                "accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "accept-language": "zh-CN,zh;q=0.9",
                "referer": f"{BASE}/",
            }
        )
        proxy = self.check_item.get("proxy", "")
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})

        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            session.cookies.set(key.strip(), value.strip(), domain=".modelscope.cn")
        return session

    @staticmethod
    def _cookie_from_chrome() -> str:
        try:
            from .chrome import CookieError, cookie_from_chrome
        except Exception as exc:  # pragma: no cover - 只在异常环境触发
            raise RuntimeError(f"加载 Chrome cookie 读取器失败：{exc}") from exc
        try:
            cookie = cookie_from_chrome()
        except CookieError as exc:
            raise RuntimeError(f"{exc}；请在 config.json 里直接填 cookie 字符串") from exc
        print("已从本机 Chrome 读取 modelscope 登录态")
        return cookie

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
        if not payload.get("success"):
            raise RuntimeError(f"接口返回失败：{str(payload)[:150]}")
        return payload

    def _daily_snapshot(self, session: requests.Session) -> dict:
        """只取「每日发放」这一类的规则（登录 200 / 阿里云 50）。"""
        data = self._api(session, RULES_URL).get("data") or []
        daily = [
            one for one in data if isinstance(one, dict) and one.get("category_key") == "daily"
        ]
        earned = sum(int(one.get("today_earned_amount") or 0) for one in daily)
        cap = sum(
            int(one.get("amount") or 0) * int(one.get("daily_cap") or 0) for one in daily
        )
        return {"rules": daily, "earned": earned, "cap": cap}


if __name__ == "__main__":
    import json
    import os

    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(_root, "config.json"), encoding="utf-8") as f:
        datas = json.loads(f.read())
    _check_item = datas.get("MODELSCOPE", [])[0]
    print(Modelscope(check_item=_check_item).main())
