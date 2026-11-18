#!/usr/bin/env python3
"""华为云 CodeArts（码道）每日签到 —— AK/SK 签名直连网关，无需浏览器 Cookie。

协议来自 dsh-free-model / dsh-checkin-all（MIT），四步全部打同一网关：
  GET  /snap-manager/v1/statistics/plugin      账户信息（是否积分计费账户）
  GET  /v1/ops/delivery?channel=IDE            活动列表，找 type=USER_LOGIN
  POST /v1/ops/claim   {campaignId, channel}   领取
  POST /v1/ops/confirm {campaignId}            确认（claim 返回 id 才做）

凭据：AK/SK + security_token（STS 临时凭据，会过期，过期需本机重新浏览器授权）。
本机凭据文件：~/.codearts/credentials.json（0600），由 codearts_auth.py 写入。
Actions：环境变量 CODEARTS_CREDENTIAL（Secret 注入的 JSON 字符串）。

用法：
  python3 codearts_checkin.py            # 签到（幂等：claimable + status 判定）
  python3 codearts_checkin.py --dry-run  # 只读：只查资格与活动状态，不领取
  python3 codearts_checkin.py --status   # 只读：查凭据过期时间与账户信息

退出码：0 成功/今日已签；2 凭据缺失；3 凭据过期需重新授权；4 其他失败。
安全：绝不打印 AK/SK/token 原值；只读模式不发任何 POST。
"""

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
from urllib.parse import urlsplit

try:
    import requests
except ImportError:
    print("CODEARTS_FAIL: 缺少 requests 库", file=sys.stderr)
    sys.exit(4)

BASE = "https://snap-access.cn-north-4.myhuaweicloud.com"
PACKAGE_INFO_PATH = "/snap-manager/v1/statistics/plugin"
OPS_DELIVERY_PATH = "/v1/ops/delivery"
OPS_CLAIM_PATH = "/v1/ops/claim"
OPS_CONFIRM_PATH = "/v1/ops/confirm"
OPS_CHANNEL = "IDE"
DAILY_LOGIN_TYPE = "USER_LOGIN"
CLAIMED_STATUSES = {"CLAIMED", "CONFIRMED", "CONSUMED"}
EXTRA_HEADERS = {"Agent-Type": "PromptCenter", "X-Language": "zh-cn"}
TIMEOUT = 30
CRED_PATH = os.path.expanduser("~/.codearts/credentials.json")
TOKEN_EXPIRED_RE = re.compile(r"security token has expired|token.*expire|APIG\.0602", re.I)


class CodeArtsError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def load_credential():
    """优先环境变量（Actions），否则本机凭据文件。只返回字典，不打印。"""
    raw = os.environ.get("CODEARTS_CREDENTIAL", "").strip()
    if not raw:
        if not os.path.exists(CRED_PATH):
            return None
        try:
            with open(CRED_PATH, encoding="utf-8") as f:
                cred = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise CodeArtsError("CODEARTS_BAD_CREDENTIAL", f"凭据文件读取失败: {type(e).__name__}")
    else:
        try:
            cred = json.loads(raw)
        except json.JSONDecodeError:
            raise CodeArtsError("CODEARTS_BAD_CREDENTIAL", "Secret CODEARTS_CREDENTIAL 不是合法 JSON")
    if not isinstance(cred, dict) or not cred.get("access_key_id") or not cred.get("secret_access_key"):
        return None
    return cred


def expires_in_days(cred):
    """返回凭据剩余天数；无法解析返回 None。"""
    exp = cred.get("expires_at") or cred.get("expiration") or ""
    if not exp:
        return None
    try:
        if isinstance(exp, (int, float)):
            ts = exp / 1000 if exp > 1e11 else exp
            expdt = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        else:
            s = str(exp).replace("Z", "+00:00")
            expdt = dt.datetime.fromisoformat(s)
            if expdt.tzinfo is None:
                expdt = expdt.replace(tzinfo=dt.timezone.utc)
        return (expdt - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400
    except (ValueError, TypeError, OverflowError):
        return None


def sdk_date():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sign_request(ak, sk, security_token, method, url, body=None):
    """华为云 SDK-HMAC-SHA256 签名。两个坑：签名 URI 补尾斜杠但发送不带；
    host 参与签名但不要手动塞进请求头。"""
    u = urlsplit(url)
    uri = u.path
    if not uri.endswith("/"):
        uri += "/"
    query = u.query  # 不含前导 ?
    date = sdk_date()
    payload = b"" if body is None else body.encode("utf-8")
    payload_hash = hashlib.sha256(payload).hexdigest()

    signed = {
        "host": u.netloc,
        "x-sdk-date": date,
        "x-sdk-content-sha256": payload_hash,
        "x-security-token": security_token or "",
    }
    if method.upper() != "GET":
        signed["content-type"] = "application/json"

    keys = sorted(signed)
    canonical = "\n".join([
        method.upper(),
        uri,
        query,
        "\n".join(f"{k}:{signed[k]}" for k in keys),
        "",
        ";".join(keys),
        payload_hash,
    ])
    string_to_sign = "\n".join([
        "SDK-HMAC-SHA256",
        date,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = f"SDK-HMAC-SHA256 Access={ak},SignedHeaders={';'.join(keys)},Signature={signature}"

    headers = {k: v for k, v in signed.items() if k != "host"}
    headers["Authorization"] = authorization
    headers.update(EXTRA_HEADERS)  # 不参与签名的附加头，签名后补上
    return headers


def signed_request(cred, method, path, body=None):
    ak = cred.get("access_key_id")
    sk = cred.get("secret_access_key")
    if not ak or not sk:
        raise CodeArtsError("CODEARTS_NO_CREDENTIAL", "凭据缺少 AK/SK")
    url = BASE + path
    headers = sign_request(ak, sk, cred.get("security_token"), method, url, body)
    try:
        if method.upper() == "GET":
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        else:
            resp = requests.post(url, headers=headers, data=payload_bytes(body), timeout=TIMEOUT)
    except requests.RequestException as e:
        raise CodeArtsError("CODEARTS_NETWORK", f"网络请求失败: {type(e).__name__}")

    text = resp.text or ""
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        data = None
    if not (200 <= resp.status_code < 300):
        detail = ""
        if isinstance(data, dict):
            detail = f"{data.get('error_code', '')} {data.get('error_msg', '')}".strip()
        detail = detail or text[:160] or "无内容"
        raise CodeArtsError("CODEARTS_HTTP_ERROR", f"HTTP {resp.status_code}: {detail}")
    if not isinstance(data, dict):
        raise CodeArtsError("CODEARTS_BAD_RESPONSE", "返回无法解析为 JSON 对象")
    code = data.get("code")
    try:
        code_num = int(code)
    except (TypeError, ValueError):
        code_num = 0
    if code_num != 0:
        raise CodeArtsError("CODEARTS_API_ERROR", str(data.get("message") or data.get("msg") or f"业务码 {code}"))
    return data.get("data")


def payload_bytes(body):
    return b"" if body is None else body.encode("utf-8")


def fetch_info(cred):
    data = signed_request(cred, "GET", PACKAGE_INFO_PATH) or {}
    pkg = data.get("package") or {}
    is_credit = pkg.get("is_credit_package") is True or str(pkg.get("is_credit_package")) == "true"
    is_token = pkg.get("is_token_package") is True or str(pkg.get("is_token_package")) == "true"
    metrics = data.get("metrics") or []
    remaining = 0
    for m in metrics:
        if isinstance(m, dict) and m.get("name") == "usageTotalPackageCredit":
            remaining = int(m.get("package_credit_remain") or 0)
    if not remaining:
        remaining = sum(int(m.get("package_credit_remain") or 0) for m in metrics if isinstance(m, dict))
    return {
        "is_credit": bool(is_credit),
        "is_token": bool(is_token),
        "remaining": remaining,
        "package_name": pkg.get("package_name_cn") or "",
    }


def fetch_activities(cred):
    data = signed_request(cred, "GET", f"{OPS_DELIVERY_PATH}?channel={OPS_CHANNEL}") or {}
    items = data.get("items")
    if not isinstance(items, list):
        raise CodeArtsError("CODEARTS_BAD_RESPONSE", "活动列表缺少 items")
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        amount = it.get("benefitAmount", it.get("amount", it.get("creditAmount", 0)))
        try:
            amount = int(amount or 0)
        except (TypeError, ValueError):
            amount = 0
        cid = it.get("campaignId")
        out.append({
            "campaignId": "" if cid is None else str(cid),
            "type": str(it.get("type") or ""),
            "title": it.get("title") or "",
            "claimable": it.get("claimable") is True,
            "status": str(it.get("status") or ""),
            "amount": amount,
        })
    return out


def do_checkin(cred, dry_run=False):
    """返回 {state, ...}。state: signed / already / inactive / dry。

    资格判据说明：社区原版用 statistics/plugin 的 is_credit_package 做门槛，
    但实测该接口对本账号返回空 {}，而 ops/delivery 活动列表里 USER_LOGIN 活动
    齐全且可领 —— 活动列表本身就是签到资格的真实来源，故去掉硬门槛，
    只凭活动列表判断。statistics/plugin 仍调用一次取积分余额用于展示。
    """
    try:
        info = fetch_info(cred)
    except CodeArtsError:
        info = {"is_credit": True, "is_token": False, "remaining": 0, "package_name": ""}
    activities = fetch_activities(cred)
    daily = [a for a in activities if a["type"] == DAILY_LOGIN_TYPE]
    if not daily:
        return {"state": "inactive", "message": "未找到每日签到活动(USER_LOGIN)", "info": info}
    # 同一个账号下有多个 USER_LOGIN 活动（签到礼/报名礼/100分礼）。
    # 只挑 claimable=True 的领取：已 CONFIRMED 的不重复 POST，未解锁的不碰。
    claimable = [a for a in daily if a["claimable"] and a["campaignId"]]
    if dry_run:
        act = claimable[0] if claimable else daily[0]
        return {"state": "dry", "activity": act, "info": info,
                "claimable_count": len(claimable)}
    if not claimable:
        if any(a["status"] in CLAIMED_STATUSES for a in daily):
            return {"state": "already", "amount": sum(a["amount"] for a in daily), "info": info}
        return {"state": "inactive",
                "message": f"当前无可领活动(status={','.join(a['status'] or '空' for a in daily)})", "info": info}

    total_credit = 0
    claimed_titles = []
    for act in claimable:
        claim = signed_request(
            cred, "POST", OPS_CLAIM_PATH,
            json.dumps({"campaignId": act["campaignId"], "channel": OPS_CHANNEL}, separators=(",", ":")),
        ) or {}
        benefit_id = claim.get("id") if isinstance(claim, dict) else None
        if benefit_id is not None:
            # confirm 失败不影响领取结果，忽略异常
            try:
                signed_request(
                    cred, "POST", OPS_CONFIRM_PATH,
                    json.dumps({"campaignId": act["campaignId"]}, separators=(",", ":")),
                )
            except CodeArtsError:
                pass
        credit = 0
        if isinstance(claim, dict):
            for key in ("benefitAmount", "credit", "credits", "creditAmount", "amount"):
                if claim.get(key):
                    try:
                        credit = int(claim[key])
                        break
                    except (TypeError, ValueError):
                        pass
        total_credit += credit or act["amount"]
        claimed_titles.append(act["title"])
    return {"state": "signed", "credit": total_credit, "titles": claimed_titles, "info": info}


def main():
    ap = argparse.ArgumentParser(description="华为云 CodeArts 每日签到（AK/SK 签名）")
    ap.add_argument("--dry-run", action="store_true", help="只读：查资格与活动状态，不领取")
    ap.add_argument("--status", action="store_true", help="只读：查凭据有效期与账户信息")
    args = ap.parse_args()

    try:
        cred = load_credential()
    except CodeArtsError as e:
        print(f"CODEARTS_FAIL: {e}", file=sys.stderr)
        sys.exit(4)
    if not cred:
        print("CODEARTS_FAIL: 未找到凭据。本机跑 codearts_auth.py 授权；Actions 需设 Secret CODEARTS_CREDENTIAL", file=sys.stderr)
        sys.exit(2)

    days = expires_in_days(cred)
    if days is not None and days < 0:
        print(f"CODEARTS_TOKEN_EXPIRED: 凭据已于 {abs(days):.1f} 天前过期，需本机重新浏览器授权", file=sys.stderr)
        sys.exit(3)

    try:
        if args.status:
            info = fetch_info(cred)
            activities = fetch_activities(cred)
            daily = [a for a in activities if a["type"] == DAILY_LOGIN_TYPE]
            status_out = {
                "credential": "已配置",
                "expires_in_days": None if days is None else round(days, 1),
                "daily_activities": [
                    {"title": a["title"], "claimable": a["claimable"], "status": a["status"]}
                    for a in daily
                ],
                "claimable_count": sum(1 for a in daily if a["claimable"]),
                "signed_today": bool(daily) and not any(a["claimable"] for a in daily),
            }
            if days is not None and 0 <= days <= 3:
                status_out["warning"] = f"凭据还剩 {days:.1f} 天过期，尽快本机重新授权并更新 Secret"
            print(json.dumps(status_out, ensure_ascii=False))
            return
        result = do_checkin(cred, dry_run=args.dry_run)
    except CodeArtsError as e:
        if TOKEN_EXPIRED_RE.search(str(e)):
            print(f"CODEARTS_TOKEN_EXPIRED: 华为云临时凭据已失效，需本机重新浏览器授权（{e}）", file=sys.stderr)
            sys.exit(3)
        print(f"CODEARTS_FAIL: [{e.code}] {e}", file=sys.stderr)
        sys.exit(4)

    out = {"state": result["state"]}
    if result["state"] == "signed":
        out["credit"] = result["credit"]
        out["message"] = f"签到成功，+{result['credit']} 积分"
    elif result["state"] == "already":
        out["message"] = "今日已签到"
    elif result["state"] == "dry":
        act = result["activity"]
        out.update({
            "claimable": act["claimable"],
            "campaign_status": act["status"],
            "amount": act["amount"],
            "title": act["title"],
            "message": "只读查询：可领取" if act["claimable"] else f"只读查询：不可领取(status={act['status'] or '空'})",
        })
    else:
        out["message"] = result.get("message", "无签到活动")
    out["remaining_credit"] = result["info"]["remaining"]
    if days is not None and 0 <= days <= 7:
        out["warning"] = f"凭据还剩 {days:.1f} 天过期，尽快本机重新授权"
    print("CODEARTS_RESULT: " + json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
