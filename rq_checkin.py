#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RQrun 每日自动签到（领 RQ 币）
=================================
原理：RQrun 的签到功能本质上是一个 H5 页面（www.rq.run/user/SignIn/），
网页登录后用图形验证码校验即可完成签到，无需 APP。

流程：
  1) 用账号密码登录 www.rq.run（需过一次图形验证码）
  2) 打开签到页，建立 RQWEBSESSID 会话
  3) 拉取签到图形验证码 -> OCR 识别 -> 提交签到
  4) 识别错误自动重试；已签到、成功、失败分别给出状态码

用法：
  python3 rq_checkin.py                       # 读 config.json
  python3 rq_checkin.py --username a@b.c --password xxx --save
  python3 rq_checkin.py --cookie "RQWEBSESSID=xxxx"   # 微信登录用户走这条
  python3 rq_checkin.py --dry-run             # 只登录+看状态，不提交签到
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少依赖：请先执行  python3 -m pip install requests")

BASE = "https://www.rq.run"
LOGIN_PAGE = f"{BASE}/Home/Login/login.html"
SIGN_PAGE = f"{BASE}/user/SignIn/"
SIGN_CAPTCHA = f"{BASE}/User/SignIn/sign_verify_code"
SIGN_SUBMIT = f"{BASE}/User/SignIn/sign_in"
LOGIN_SUBMIT = f"{BASE}/Home/Login/password_login"

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / ".rq_state.json"
LOG_PATH = HERE / "checkin.log"

# 登录接口返回码（实测）
LOGIN_OK = 0          # 登录成功
LOGIN_FIELD = 422     # 字段/验证码不通过
LOGIN_BLOCK = 600     # 错误次数过多，需等待 5 分钟

# 签到接口返回码
ST_OK = 1          # 签到成功
ST_DONE = 10009    # 今日已签到
ST_CAPTCHA = 10011  # 验证码错误，可重试


class LoginBlocked(RuntimeError):
    """触发风控，必须停下来等冷却。"""


class LoginFailed(RuntimeError):
    """凭据或账号问题。"""


def cap_url() -> str:
    return f"{BASE}/Home/Tool/verify/rand/{random.random():.16f}"


def log(msg: str, quiet: bool = False) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if not quiet:
        print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# 样例统计：RQ 两个验证码都是 4 位纯数字，OCR 偶尔把 0 认成 o、1 认成 l
# 这里做一次常见形近字符归一化，剩下的靠重试兜底
_CONFUSE = str.maketrans(
    {"o": "0", "O": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "i": "1",
     "|": "1", "!": "1", "Z": "2", "z": "2", "S": "5", "s": "5", "G": "6",
     "b": "6", "B": "8", "g": "9", "q": "9", "T": "7", "A": "4"}
)


def normalize(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]", "", text or "").translate(_CONFUSE)


# --------------------------------------------------------------------------- OCR
class Solver:
    """图形验证码识别：优先 ddddocr，失败则退回人工输入。"""

    def __init__(self, manual: bool = False):
        self.manual = manual
        self._ocr = None

    def _engine(self):
        if self._ocr is None:
            import ddddocr  # 延迟导入，避免每次启动都加载模型

            self._ocr = ddddocr.DdddOcr(beta=True, show_ad=False)
        return self._ocr

    def solve(self, img: bytes, tag: str = "captcha") -> str:
        if not self.manual:
            try:
                text = self._engine().classification(img)
                text = normalize(text)
                if text:
                    return text
            except Exception as e:  # 模型缺失 / onnx 异常
                log(f"OCR 不可用（{type(e).__name__}: {e}），转人工输入")
                self.manual = True

        # 人工兜底
        p = Path(f"/tmp/rq_{tag}.png")
        p.write_bytes(img)
        print(f"\n验证码已保存：{p}", flush=True)
        if sys.platform == "darwin":
            os.system(f'open "{p}" >/dev/null 2>&1')
        return input("请输入验证码：").strip()


# --------------------------------------------------------------------------- 核心
class RQChecker:
    def __init__(self, solver: Solver, verbose: bool = True):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": DESKTOP_UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": BASE,
            }
        )
        self.solver = solver
        self.verbose = verbose
        self.load_cookies()

    # -- 会话持久化 ---------------------------------------------------------
    def load_cookies(self) -> None:
        if not STATE_PATH.exists():
            return
        try:
            data = json.loads(STATE_PATH.read_text())
            for k, v in data.get("cookies", {}).items():
                self.s.cookies.set(k, v, domain=".rq.run")
        except Exception as e:
            log(f"读取本地会话失败（忽略）：{e}")

    def save_cookies(self) -> None:
        data = {"cookies": {c.name: c.value for c in self.s.cookies}, "saved_at": int(time.time())}
        STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        try:
            STATE_PATH.chmod(0o600)
        except OSError:
            pass

    # -- 登录态探测 ---------------------------------------------------------
    def logged_in(self) -> bool:
        try:
            r = self.s.get(SIGN_PAGE, timeout=20, allow_redirects=True)
        except requests.RequestException as e:
            log(f"探测登录态失败（{type(e).__name__}），按未登录处理")
            return False
        if "/Home/Login/login" in r.url or "/home/login" in r.url:
            return False
        self.sign_page_html = r.text
        return r.status_code == 200 and "RQWEBSESSID" in self.s.cookies

    # -- 登录 ---------------------------------------------------------------
    def login(self, username: str, password: str, tries: int = 6) -> bool:
        for i in range(1, tries + 1):
            img = self.s.get(cap_url(), timeout=20, headers={"Referer": LOGIN_PAGE}).content
            code = self.solver.solve(img, "login")
            r = self.s.post(
                LOGIN_SUBMIT,
                data={"username": username, "password": password, "login_code": code, "remember": "1"},
                headers={"Referer": LOGIN_PAGE, "X-Requested-With": "XMLHttpRequest"},
                timeout=25,
            )
            try:
                j = r.json()
            except ValueError:
                log(f"登录第 {i} 次：返回非 JSON（HTTP {r.status_code}），重试")
                continue

            status, err = j.get("status"), str(j.get("error") or "")

            # 以「能否打开签到页」作为唯一成功判据，比猜 status 靠谱
            if self.logged_in():
                log(f"登录成功（第 {i} 次尝试）")
                return True

            if status == LOGIN_BLOCK or "错误次数过多" in err:
                raise LoginBlocked(f"触发风控：{err}。请等 5 分钟后再试，不要连续重试。")
            if "验证码" in err:
                log(f"登录第 {i} 次：图形验证码未通过，重试")
                time.sleep(0.8)
                continue
            raise LoginFailed(f"登录失败：{err or j}（status={status}）")

        raise LoginFailed(f"连续 {tries} 次验证码识别失败，登录未完成")

    # -- 状态解析 -----------------------------------------------------------
    def sign_state(self) -> dict:
        """从签到页解析当前状态：连续天数、累计天数、今日是否已签。"""
        info: dict = {}
        try:
            r = self.s.get(SIGN_PAGE, timeout=20)
            h = r.text
        except requests.RequestException:
            return info
        m = re.search(r"我已经连续签到<span>(\d+)</span>天", h)
        if m:
            info["streak"] = int(m.group(1))
        info["signed_today"] = "今日已签到" in h or "Jy-sgined" in h
        m = re.search(r'var my_info = JSON\.parse\(\'(.*?)\'\);', h, re.S)
        if m:
            try:
                me = json.loads(m.group(1).replace("\\/", "/"))
                if me.get("sign_num"):
                    info["total_days"] = int(me["sign_num"])
                if me.get("full_name"):
                    info["nickname"] = me["full_name"]
            except Exception:
                pass
        return info

    # -- 签到 ---------------------------------------------------------------
    def checkin(self, tries: int = 12) -> tuple[int, str]:
        for i in range(1, tries + 1):
            img = self.s.get(
                SIGN_CAPTCHA, timeout=20, headers={"Referer": SIGN_PAGE}
            )
            if "image" not in img.headers.get("content-type", ""):
                if "/Home/Login/login" in img.url:
                    return -1, "会话已失效"
                return -1, f"验证码接口异常（HTTP {img.status_code}）"

            code = self.solver.solve(img.content, "sign")
            r = self.s.post(
                SIGN_SUBMIT,
                data={"codes": code},
                headers={"Referer": SIGN_PAGE, "X-Requested-With": "XMLHttpRequest"},
                timeout=25,
            )
            try:
                j = r.json()
            except ValueError:
                log(f"签到第 {i} 次：返回非 JSON（HTTP {r.status_code}）")
                continue

            raw = j.get("status")
            try:
                status = int(raw)
            except (TypeError, ValueError):
                return -1, f"接口返回异常：{j}"

            if status == ST_OK:
                return status, "签到成功，RQ 币已到账"
            if status == ST_DONE:
                return status, "今日已签到，无需重复"
            if status == ST_CAPTCHA:
                log(f"签到第 {i} 次：验证码识别错误（10011），重试")
                time.sleep(0.6)
                continue
            if status == LOGIN_BLOCK or "错误次数过多" in str(j.get("error") or ""):
                return -3, f"触发风控：{j.get('error')}（等 5 分钟再试）"
            return status, f"签到失败，服务端状态码 {status}（{j.get('error') or j.get('msg') or ''}）原始返回：{j}"

        return -2, f"验证码连续识别失败 {tries} 次，本次签到未完成"


# --------------------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="RQrun 自动签到")
    ap.add_argument("--username", help="RQ 账号（邮箱/手机号）")
    ap.add_argument("--password", help="RQ 登录密码")
    ap.add_argument("--cookie", help='已有网页会话，如 "RQWEBSESSID=xxxx"')
    ap.add_argument("--from-chrome", action="store_true", help="自动从本机 Chrome 读取 RQWEBSESSID 会话")
    ap.add_argument("--save", action="store_true", help="把账号密码写入 config.json")
    ap.add_argument("--dry-run", action="store_true", help="只登录并查看状态，不提交签到")
    ap.add_argument("--manual", action="store_true", help="强制人工输入验证码（不装 OCR 时用）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args()

    cfg = {}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}")

    username = args.username or os.getenv("RQ_USERNAME") or cfg.get("username")
    password = args.password or os.getenv("RQ_PASSWORD") or cfg.get("password")
    cookie = args.cookie or os.getenv("RQ_COOKIE") or cfg.get("cookie")

    if args.from_chrome:
        try:
            from get_rq_cookie import chrome_key, profiles, read_rq

            k = chrome_key()
            ck = {}
            for p in profiles():
                ck.update(read_rq(p, k))
            if ck.get("RQWEBSESSID"):
                cookie = "; ".join(f"{a}={b}" for a, b in ck.items())
                log(f"已从 Chrome 读取会话（RQWEBSESSID={ck['RQWEBSESSID'][:8]}…）")
            else:
                log("Chrome 里没有可用的 RQ 会话")
        except Exception as e:
            log(f"从 Chrome 读取会话失败（{type(e).__name__}: {e}）")

    if args.save and username and password:
        CONFIG_PATH.write_text(
            json.dumps({"username": username, "password": password}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        CONFIG_PATH.chmod(0o600)
        log(f"已保存账号到 {CONFIG_PATH}")

    if not cookie and not (username and password):
        print("缺少凭据：请用 --username/--password，或 --cookie，或先在 config.json 里配置。", file=sys.stderr)
        return 2

    checker = RQChecker(Solver(manual=args.manual))

    if cookie:
        for part in cookie.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                checker.s.cookies.set(k.strip(), v.strip(), domain=".rq.run")

    ok_login = checker.logged_in()
    if ok_login:
        log("复用已有网页会话，无需重新登录")
    else:
        if not (username and password):
            log("会话已失效且未配置账号密码，请用 --cookie 重新提供会话")
            return 3
        try:
            checker.login(username, password)
        except LoginBlocked as e:
            log(str(e))
            return 5
        except LoginFailed as e:
            log(str(e))
            return 4
        if not checker.logged_in():
            log("登录后仍无法打开签到页，请检查账号状态")
            return 4
        checker.save_cookies()

    if args.dry_run:
        html = getattr(checker, "sign_page_html", "") or ""
        (HERE / "sign_page.html").write_text(html, encoding="utf-8", errors="ignore")
        has_sign = "sign_in" in html or "sign_verify_code" in html
        log(f"dry-run：已登录，签到页 {'已加载' if has_sign else '已获取'}（{len(html)} 字节，已存 sign_page.html），未提交签到")
        if args.json:
            print(json.dumps({"ok": True, "stage": "dry-run", "page_ok": has_sign}, ensure_ascii=False))
        return 0

    status, msg = checker.checkin()
    state = checker.sign_state()
    extra = ""
    if state.get("streak") is not None:
        extra = f"，连续签到 {state['streak']} 天"
    if state.get("total_days"):
        extra += f"，累计 {state['total_days']} 天"
    log(f"结果：{msg}（status={status}）{extra}")
    checker.save_cookies()

    if args.json:
        out = {"ok": status in (ST_OK, ST_DONE), "status": status, "message": msg}
        out.update(state)
        print(json.dumps(out, ensure_ascii=False))
        return 0 if status in (ST_OK, ST_DONE) else 1
    return 0 if status in (ST_OK, ST_DONE) else 1


if __name__ == "__main__":
    sys.exit(main())
