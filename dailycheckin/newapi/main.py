from __future__ import annotations

import requests

from dailycheckin import CheckIn


class NewApi(CheckIn):
    """New API / One API 系中转站的每日签到

    每个配置项代表一个站点, 字段:
        name          站点展示名 (可选)
        base_url      站点根地址, 如 https://kktoken.cc
        access_token  站点个人设置里生成的访问令牌, 不是 sk- 开头的 API 令牌
        user_id       站点用户 ID, 新版 New API 必填, 否则返回
                      "Unauthorized, New-Api-User header not provided"
                      取法: 站内控制台执行 JSON.parse(localStorage.getItem('user')).id
        checkin_path  签到接口路径 (可选, 默认 /api/user/checkin)
    """

    name = "AI 中转站"

    def __init__(self, check_item: dict):
        self.check_item = check_item

    @staticmethod
    def build_headers(access_token: str, user_id: str | None) -> dict:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        }
        if user_id:
            headers["New-Api-User"] = str(user_id)
        return headers

    @staticmethod
    def quota_to_usd(base_url: str, quota: float) -> str:
        """按站点自身的 quota_per_unit 折算成美元, 取不到就返回原始额度"""
        try:
            resp = requests.get(f"{base_url}/api/status", timeout=10)
            per_unit = float((resp.json().get("data") or {}).get("quota_per_unit") or 500000)
            return f"{quota / per_unit:.4f} 美元"
        except Exception:
            return f"{int(quota)} 额度"

    def main(self) -> str:
        site = str(self.check_item.get("name") or "").strip() or "未命名站点"
        base = str(self.check_item.get("base_url") or "").strip().rstrip("/")
        token = str(self.check_item.get("access_token") or "").strip()
        user_id = str(self.check_item.get("user_id") or "").strip() or None
        path = str(self.check_item.get("checkin_path") or "/api/user/checkin")

        if not base or not token:
            return f"{site}: 缺少 base_url 或 access_token"

        url = f"{base}{path}"
        headers = self.build_headers(token, user_id)
        try:
            resp = requests.post(url, headers=headers, timeout=20)
            if resp.status_code in (404, 405):
                resp = requests.get(url, headers=headers, timeout=20)
        except Exception as e:
            return f"{site}: 请求异常 {e}"

        try:
            data = resp.json()
        except ValueError:
            snippet = resp.text.strip()[:60].replace("\n", " ")
            return f"{site}: 非 JSON 响应(HTTP {resp.status_code}) {snippet}"

        success = bool(data.get("success"))
        message = str(data.get("message") or "").strip() or ("成功" if success else "失败")

        extra = ""
        payload = data.get("data")
        if isinstance(payload, dict):
            quota = payload.get("quota")
            if isinstance(quota, (int, float)) and quota:
                extra = f"，+{self.quota_to_usd(base, float(quota))}"

        if "New-Api-User" in message:
            return f"{site}: 缺少 user_id，请填站点用户 ID（{message}）"
        if not success and any(word in message for word in ("已签到", "已经签到", "重复签到")):
            return f"{site}: 今日已签到{extra}"
        return f"{site}: {message}{extra}"
