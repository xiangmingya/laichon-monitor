import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from laichon_monitor import (
    APIError, clear_api_error, configured_accounts, inspect_order,
    main, post_api, query_months, report_api_error, run, send_notification,
)


NOW = 1791708000


def detail(powers=(19.9, 19.6), gap=300, age=0, number="test-order"):
    start = NOW - age - 3600
    return {
        "order_no": number, "order_status": 3,
        "charging_details": {
            # 故意倒序，确保代码按时间而非 JSON 键插入顺序取最新采样。
            "3600": {"real_power": str(powers[1])},
            "startTimestamp": str(start),
            str(3600 - gap): {"real_power": str(powers[0])},
        },
    }


class MonitorTests(unittest.TestCase):
    def test_two_distinct_low_samples(self):
        self.assertTrue(inspect_order(detail(), {}, 20, NOW)[0])

    def test_threshold_is_strict_and_high_sample_resets(self):
        for powers in ((20, 19), (19, 20), (25, 19), (19, 25)):
            self.assertFalse(inspect_order(detail(powers), {}, 20, NOW)[0])

    def test_stale_future_and_duplicate_samples(self):
        self.assertFalse(inspect_order(detail(age=901), {}, 20, NOW)[0])
        self.assertFalse(inspect_order(detail(age=-121), {}, 20, NOW)[0])
        self.assertFalse(inspect_order(detail(), {"last_sample": NOW, "threshold": 20}, 20, NOW)[0])

    def test_too_close_or_missing_intermediate_samples(self):
        for gap in (10, 239, 421, 900):
            self.assertFalse(inspect_order(detail(gap=gap), {}, 20, NOW)[0])

    def test_startup_and_completed_orders(self):
        payload = detail()
        payload["charging_details"]["startTimestamp"] = str(NOW - 300)
        payload["charging_details"]["300"] = payload["charging_details"].pop("3600")
        payload["charging_details"]["0"] = payload["charging_details"].pop("3300")
        self.assertFalse(inspect_order(payload, {}, 20, NOW)[0])
        payload["order_status"] = 7
        self.assertFalse(inspect_order(payload, {}, 20, NOW)[0])

    def test_invalid_power_does_not_become_zero(self):
        for value in (None, "NaN", "inf", "-1"):
            payload = detail()
            payload["charging_details"]["3600"]["real_power"] = value
            with self.assertRaises((ValueError, TypeError)):
                inspect_order(payload, {}, 20, NOW)

    def test_year_rollover_uses_china_timezone(self):
        # UTC 2025-12-31 16:00，即北京时间 2026-01-01。
        self.assertEqual(query_months(1767196800), ["2026-01", "2025-12"])

    def test_once_per_order_and_failed_notification_retries(self):
        attempts = []
        payloads = []
        def call(endpoint, payload):
            payloads.append((endpoint, payload))
            if endpoint == "list":
                return {"list": [
                    {"order_no": "test-order", "order_status": 3},
                    {"order_no": "completed", "order_status": 7},
                ]}
            return detail()
        def notify(title, content):
            attempts.append(content)
            if len(attempts) == 1:
                raise RuntimeError("模拟通知失败")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertEqual(run(call, notify, path, 20, NOW), 1)
            self.assertFalse(path.exists())
            self.assertEqual(run(call, notify, path, 20, NOW), 0)
            self.assertTrue(json.loads(path.read_text())["test-order"]["notified"])
            self.assertEqual(run(call, notify, path, 20, NOW), 0)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(len([x for x in payloads if x[0] == "detail"]), 3)

    def test_multiple_orders_are_independent(self):
        sent = []
        def call(endpoint, payload):
            if endpoint == "list":
                return {"list": [{"order_no": number, "order_status": 3} for number in ("a", "b")]}
            return detail(number=payload["order_no"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertEqual(run(call, lambda title, body: sent.append(body), path, 20, NOW), 0)
            self.assertEqual(len(sent), 2)

    def test_bad_list_or_detail_cannot_notify(self):
        sent = []
        def call(endpoint, payload):
            if endpoint == "list":
                return {"list": [{"order_no": "test-order", "order_status": 3}]}
            return {"order_no": "wrong-order"}
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(run(call, lambda *args: sent.append(args), Path(directory) / "state.json", 20, NOW), 1)
            self.assertEqual(sent, [])


class AccountAndFailureTests(unittest.TestCase):
    def test_single_account_still_works_and_second_uses_own_credentials(self):
        with patch.dict(os.environ, {"LAICHON_TOKEN": "one", "LAICHON_OPENID": "id-one"}, clear=True):
            self.assertEqual(configured_accounts(), [("", "我的车", "one", "id-one")])
            os.environ.update(LAICHON_TOKEN_2="two", LAICHON_OPENID_2="id-two")
            self.assertEqual(configured_accounts()[1], ("_2", "老婆的车", "two", "id-two"))
            del os.environ["LAICHON_OPENID_2"]
            with self.assertRaises(RuntimeError):
                configured_accounts()

    def test_api_failures_are_safe_and_identifiable(self):
        with patch("laichon_monitor.urlopen", side_effect=HTTPError("private-url", 401, "secret", {}, None)):
            with self.assertRaises(APIError) as caught:
                post_api("list", {}, "token", "openid")
            self.assertIn("401", str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))
        with patch("laichon_monitor.urlopen", return_value=io.BytesIO(b'{"code":1001}')):
            with self.assertRaises(APIError):
                post_api("list", {}, "token", "openid")

    def test_error_alert_once_rearms_after_recovery_and_preserves_orders(self):
        with tempfile.TemporaryDirectory() as directory, patch("laichon_monitor.send_notification") as notify:
            path = Path(directory) / "state.json"
            path.write_text('{"old-order":{"notified":true}}')
            error = APIError("list 接口 HTTP 401")
            report_api_error(path, "我的车", error)
            report_api_error(path, "我的车", error)
            self.assertEqual(notify.call_count, 1)
            clear_api_error(path)
            report_api_error(path, "我的车", error)
            self.assertEqual(notify.call_count, 2)
            self.assertTrue(json.loads(path.read_text())["old-order"]["notified"])

    def test_error_push_failure_is_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with patch("laichon_monitor.send_notification", side_effect=RuntimeError("offline")):
                with self.assertRaises(RuntimeError):
                    report_api_error(path, "我的车", APIError("timeout"))
            self.assertFalse(path.exists())
            with patch("laichon_monitor.send_notification") as notify:
                report_api_error(path, "我的车", APIError("timeout"))
                self.assertEqual(notify.call_count, 1)

    def test_expired_primary_does_not_block_wife_and_alerts_are_deduplicated(self):
        calls = []
        def api(endpoint, payload, token, openid):
            calls.append((token, openid, endpoint))
            if token == "expired":
                raise APIError("list 接口 HTTP 401")
            if endpoint == "list":
                return {"list": [{"order_no": "wife-order", "order_status": 3}]}
            return detail(number="wife-order")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "LAICHON_TOKEN": "expired", "LAICHON_OPENID": "id-one",
            "LAICHON_TOKEN_2": "valid", "LAICHON_OPENID_2": "id-two",
            "SHOWDOC_PUSH_URL": "https://push.showdoc.com.cn/server/api/push/test-token",
            "LAICHON_STATE_FILE": str(Path(directory) / "state.json"),
        }, clear=True), patch("laichon_monitor.sys.argv", ["laichon_monitor.py"]), patch(
            "laichon_monitor.time.time", return_value=NOW
        ), patch("laichon_monitor.post_api", side_effect=api), patch(
            "laichon_monitor.send_notification"
        ) as notify:
            self.assertEqual(main(), 1)
            self.assertEqual(notify.call_count, 2)
            titles = [call.args[0] for call in notify.call_args_list]
            self.assertIn("我的车：莱充接口请求异常", titles)
            self.assertIn("老婆的车：莱充低功率提醒", titles)
            self.assertIn(("valid", "id-two", "detail"), calls)
            self.assertTrue((Path(directory) / "state_2.json").exists())
            self.assertEqual(main(), 1)
            self.assertEqual(notify.call_count, 2)

    def test_detail_api_failure_is_reported_to_account_handler(self):
        def api(endpoint, payload):
            if endpoint == "list":
                return {"list": [{"order_no": "test-order", "order_status": 3}]}
            raise APIError("detail 接口 HTTP 401")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(APIError):
                run(api, lambda *args: None, Path(directory) / "state.json", 20, NOW)


class ShowDocTests(unittest.TestCase):
    URL = "https://push.showdoc.com.cn/server/api/push/test-token"

    def test_form_body_and_success_response(self):
        with patch.dict(os.environ, {"SHOWDOC_PUSH_URL": self.URL}), patch(
            "laichon_monitor.urlopen", return_value=io.BytesIO(b'{"error_code":0}')
        ) as request:
            send_notification("低功率提醒", "第一行\n第二行")
            argument = request.call_args.args[0]
            self.assertEqual(argument.get_method(), "POST")
            self.assertEqual(argument.full_url, self.URL)
            self.assertEqual(parse_qs(argument.data.decode()), {
                "title": ["低功率提醒"], "content": ["第一行\n第二行"]
            })

    def test_business_error_and_invalid_json_are_failures(self):
        for body in (b'{"error_code":10103}', b'{}', b'[]', b'not-json'):
            with patch.dict(os.environ, {"SHOWDOC_PUSH_URL": self.URL}), patch(
                "laichon_monitor.urlopen", return_value=io.BytesIO(body)
            ), self.assertRaises(RuntimeError):
                send_notification("test", "test")

    def test_transport_error_does_not_expose_push_token(self):
        for error in (URLError(self.URL), HTTPError(self.URL, 403, self.URL, {}, None)):
            with patch.dict(os.environ, {"SHOWDOC_PUSH_URL": self.URL}), patch(
                "laichon_monitor.urlopen", side_effect=error
            ), self.assertRaises(RuntimeError) as caught:
                send_notification("test", "test")
            self.assertNotIn("test-token", str(caught.exception))

    def test_missing_or_wrong_address_is_not_requested(self):
        for url in ("", "http://push.showdoc.com.cn/server/api/push/test-token", "https://example.com/push/test-token"):
            with patch.dict(os.environ, {"SHOWDOC_PUSH_URL": url}), patch(
                "laichon_monitor.urlopen"
            ) as request, self.assertRaises(RuntimeError):
                send_notification("test", "test")
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
