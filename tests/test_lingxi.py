"""灵犀插件的纯离线单测：全程 mock，不联网、不读 config.json、不发任何写请求。

重点守住一条边界：幂等性未被服务端验证过，status != available 时绝不能 POST claim。
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dailycheckin.lingxi import main as L  # noqa: N812

COOKIE = "wps_sid=unit-test-only; kso_sid=x; uid=123456"


def task(status="claimed", today_reward=100, streak=1, today_day=1):
    days = [
        {"day": i, "reward": 200 if i == 7 else 100, "status": "claimed" if i <= streak else "locked"}
        for i in range(1, 8)
    ]
    return {
        "task_key": L.TASK_KEY,
        "reward_amount": 100,
        "status": status,
        "extra": {
            "check_in_days": days,
            "consecutive_days": streak,
            "today_day": today_day,
            "today_reward": today_reward,
        },
    }


def tasks_response(status="claimed", total=100, **kwargs):
    return {
        "result": "ok",
        "http_status": 200,
        "data": {"tasks": [task(status, **kwargs)], "total_claimed_credits": total},
    }


class Driver:
    """按 URL 分发的可控假请求，记录每一次调用。"""

    def __init__(self, before=None, after=None, claim=None, balance=None):
        self.before = before or tasks_response()
        self.after = after or tasks_response()
        self.claim = claim if claim is not None else {"result": "ok", "http_status": 200, "data": {}}
        self.balance = (
            balance
            if balance is not None
            else {
                "result": "ok",
                "http_status": 200,
                "data": {"total_balance": 900},
            }
        )
        self.calls = []
        self._claimed = False

    def __call__(self, url, cookie, method="GET", timeout=20):
        self.calls.append((method, url))
        if url == L.TASKS_URL:
            return self.after if self._claimed else self.before
        if url.startswith(L.CLAIM_URL):
            self._claimed = True
            return self.claim
        if url == L.BALANCE_URL:
            return self.balance
        raise AssertionError(f"意外请求: {method} {url}")

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def run_main(driver=None, cookie=COOKIE):
    driver = driver or Driver()
    with mock.patch.object(L, "request_json", side_effect=driver):
        text = L.Lingxi({"cookie": cookie}).main()
    return text, driver


# ────────────────────────────── 解析层 ──────────────────────────────
class TestParsing(unittest.TestCase):
    def test_today_cst_uses_utc_plus_8(self):
        got = L.today_cst()
        expected = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        self.assertEqual(got, expected)
        self.assertEqual(len(got), 10)

    def test_today_reward_prefers_server_field(self):
        self.assertEqual(L.today_reward(task(today_reward=200)), 200)

    def test_today_reward_never_uses_cycle_total(self):
        # total_claimed_credits 是周期累计，压根不传给 today_reward
        self.assertEqual(L.today_reward(task(today_reward=100)), 100)

    def test_today_reward_falls_back_to_day_table(self):
        t = task(streak=6)
        t["extra"].pop("today_reward")
        t.pop("reward_amount")
        self.assertEqual(L.today_reward(t), 200)  # 第 7 天

    def test_today_reward_default_when_all_missing(self):
        t = {"task_key": L.TASK_KEY, "status": "available"}
        self.assertEqual(L.today_reward(t), 100)

    def test_progress_mentions_seventh_day_bonus(self):
        text = L.progress_text(task())
        self.assertIn("连续 1 天", text)
        self.assertIn("第 1/7 天", text)
        self.assertIn("第 7 天可领 200 智点", text)

    def test_find_daily_task(self):
        self.assertIsNotNone(L.find_daily_task([task()]))
        self.assertIsNone(L.find_daily_task([{"task_key": "other"}]))


# ────────────────────────────── 主流程 ──────────────────────────────
class TestMain(unittest.TestCase):
    def test_claimed_never_posts(self):
        text, driver = run_main(Driver(before=tasks_response("claimed")))
        self.assertEqual(driver.posts(), [])
        self.assertIn("今天已签到", text)
        self.assertIn("智点余额：900", text)

    def test_available_posts_once_with_beijing_date(self):
        driver = Driver(before=tasks_response("available"), after=tasks_response("claimed"))
        text, driver = run_main(driver)
        self.assertEqual(len(driver.posts()), 1)
        method, url = driver.posts()[0]
        self.assertEqual(method, "POST")
        self.assertIn(f"date={L.today_cst()}", url)
        self.assertIn("成功 +100 智点", text)
        self.assertIn("状态已翻转", text)

    def test_claim_rejected_raises_and_does_not_verify(self):
        driver = Driver(
            before=tasks_response("available"),
            claim={"result": "forbidden", "code": 403, "message": "风控", "http_status": 200},
        )
        with mock.patch.object(L, "request_json", side_effect=driver):
            with self.assertRaisesRegex(RuntimeError, "签到被拒"):
                L.Lingxi({"cookie": COOKIE}).main()
        self.assertEqual(len(driver.posts()), 1)

    def test_transient_claim_is_not_false_success(self):
        driver = Driver(
            before=tasks_response("available"),
            claim={"code": L.CODE_TRANSIENT, "msg": "网络超时"},
        )
        with mock.patch.object(L, "request_json", side_effect=driver):
            with self.assertRaisesRegex(RuntimeError, "签到请求失败"):
                L.Lingxi({"cookie": COOKIE}).main()

    def test_unconfirmed_raises_instead_of_reporting_success(self):
        driver = Driver(before=tasks_response("available"), after=tasks_response("available"))
        with mock.patch.object(L, "request_json", side_effect=driver):
            with self.assertRaisesRegex(RuntimeError, "UNCONFIRMED"):
                L.Lingxi({"cookie": COOKIE}).main()

    def test_unknown_status_does_not_post(self):
        driver = Driver(before=tasks_response("locked"))
        with mock.patch.object(L, "request_json", side_effect=driver):
            with self.assertRaisesRegex(RuntimeError, "签到状态异常"):
                L.Lingxi({"cookie": COOKIE}).main()
        self.assertEqual(driver.posts(), [])

    def test_task_panel_error_raises(self):
        driver = Driver(before={"result": "error", "http_status": 500, "message": "网关"})
        with mock.patch.object(L, "request_json", side_effect=driver):
            with self.assertRaisesRegex(RuntimeError, "任务面板返回异常"):
                L.Lingxi({"cookie": COOKIE}).main()
        self.assertEqual(driver.posts(), [])

    def test_missing_daily_task_raises(self):
        driver = Driver(before={"result": "ok", "http_status": 200, "data": {"tasks": []}})
        with mock.patch.object(L, "request_json", side_effect=driver):
            with self.assertRaisesRegex(RuntimeError, "daily_check_in"):
                L.Lingxi({"cookie": COOKIE}).main()

    def test_auth_failure_propagates(self):
        def boom(url, cookie, method="GET", timeout=20):
            raise L.NeedAuthError("灵犀会话已失效")

        with mock.patch.object(L, "request_json", side_effect=boom):
            with self.assertRaises(L.NeedAuthError):
                L.Lingxi({"cookie": COOKIE}).main()

    def test_cookie_validation(self):
        with self.assertRaisesRegex(RuntimeError, "缺少 cookie"):
            L.Lingxi({}).main()
        with self.assertRaisesRegex(RuntimeError, "wps_sid"):
            L.Lingxi({"cookie": "uid=123"}).main()

    def test_balance_failure_does_not_break_signin(self):
        driver = Driver(
            before=tasks_response("claimed"),
            balance={"code": L.CODE_TRANSIENT, "msg": "超时"},
        )
        text, _ = run_main(driver)
        self.assertIn("今天已签到", text)
        self.assertNotIn("智点余额", text)

    def test_check_only_is_read_only(self):
        driver = Driver(before=tasks_response("claimed"))
        with mock.patch.object(L, "request_json", side_effect=driver):
            out = L.Lingxi({"cookie": COOKIE}).check_only()
        self.assertEqual(driver.posts(), [])
        self.assertEqual(out["status"], "claimed")
        self.assertEqual(out["today_reward"], 100)
        self.assertEqual(out["balance"], 900)
        self.assertEqual(out["cycle_total_claimed"], 100)


if __name__ == "__main__":
    unittest.main()
