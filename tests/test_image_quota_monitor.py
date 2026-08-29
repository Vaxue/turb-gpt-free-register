import unittest
import sys
import types
from unittest.mock import patch

from core import image_quota_monitor as monitor


class ImageQuotaMonitorTests(unittest.TestCase):
    def test_extract_quota_nested_payload(self):
        self.assertEqual(monitor._extract_quota({"data": {"remaining": "0.009"}}), 0.009)
        self.assertEqual(monitor._extract_quota({"accounts": [{"balance": 2}, {"balance": 3}]}), 5.0)
        self.assertIsNone(monitor._extract_quota({"data": {"status": "ok"}}))

    def test_scaled_workers_tracks_cpu(self):
        class Cfg:
            IMAGE_API_REGISTER_WORKERS_MIN = 1
            IMAGE_API_REGISTER_WORKERS_MAX = 8
            IMAGE_API_CPU_IDLE_PERCENT = 35
            IMAGE_API_CPU_BUSY_PERCENT = 80
        self.assertEqual(monitor._scaled_workers(Cfg, 10), 8)
        self.assertEqual(monitor._scaled_workers(Cfg, 90), 1)
        self.assertGreater(monitor._scaled_workers(Cfg, 50), 1)

    def test_schedule_live_checks_rotates_accounts_and_queues_batch(self):
        class Cfg:
            IMAGE_API_LIVE_CHECK_BATCH_SIZE = 2

        rows = [
            {"id": 1, "email": "a@example.com", "live_check_status": "live"},
            {"id": 2, "email": "b@example.com", "live_check_status": ""},
            {"id": 3, "email": "c@example.com", "live_check_status": "live"},
        ]
        fake_live = types.ModuleType("core.live_check_service")
        fake_live.enqueue_account_live_check = lambda **kwargs: {"accepted": True, **kwargs}
        with patch.object(monitor, "_cfg", return_value=Cfg), \
             patch("core.db.list_accounts", return_value=rows), \
             patch.dict(sys.modules, {"core.live_check_service": fake_live}), \
             patch.object(fake_live, "enqueue_account_live_check", side_effect=lambda **kwargs: {"accepted": True, **kwargs}) as enqueue:
            monitor._state.update({"live_cursor": 0, "last_live_check_at": None})
            result = monitor.schedule_live_checks()
            self.assertEqual(result["queued"], 2)
            self.assertEqual([c.kwargs["email"] for c in enqueue.call_args_list], ["a@example.com", "b@example.com"])
            result = monitor.schedule_live_checks()
            self.assertEqual(result["queued"], 2)
            self.assertEqual(
                [c.kwargs["email"] for c in enqueue.call_args_list[-2:]],
                ["c@example.com", "a@example.com"],
            )

    def test_disabled_mail_delete_does_not_request(self):
        with patch("core.mail_admin.requests.post") as post:
            result = __import__("core.mail_admin", fromlist=["delete_email"]).delete_email("tmp@mail.apisaver.com")
            self.assertTrue(result.get("skipped"))
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
