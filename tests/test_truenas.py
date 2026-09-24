import unittest
from pathlib import Path

from ugreen_fan.truenas import ALERT_CLASS, clear_alert, raise_alert, register, unregister


class FakeMidclt:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, method, *params, job=False):
        self.calls.append((method, params, job))
        return self.responses.get(method)

    def methods(self):
        return [m for m, _, _ in self.calls]


OURS = {"klass": ALERT_CLASS, "args": {"error": "[ugreen-fan] old reason"}}
DOCKER = {"klass": ALERT_CLASS, "args": {"error": "Docker service could not be started"}}
OTHER = {"klass": "PoolUpgraded", "args": "tank"}


class RaiseAlertTest(unittest.TestCase):
    def test_creates_when_none_exist(self):
        call = FakeMidclt({"alert.list": [OTHER]})
        raise_alert("service down", call)
        self.assertEqual(call.calls[-1], ("alert.oneshot_create",
                                          (ALERT_CLASS, {"error": "[ugreen-fan] service down"}), True))
        self.assertNotIn("alert.oneshot_delete", call.methods())

    def test_same_reason_is_not_duplicated(self):
        call = FakeMidclt({"alert.list": [OURS]})
        raise_alert("old reason", call)
        self.assertEqual(call.methods(), ["alert.list"])

    def test_new_reason_replaces_ours(self):
        call = FakeMidclt({"alert.list": [OURS]})
        raise_alert("new reason", call)
        self.assertEqual(call.methods(), ["alert.list", "alert.oneshot_delete", "alert.oneshot_create"])

    def test_foreign_alert_is_never_deleted(self):
        call = FakeMidclt({"alert.list": [OURS, DOCKER]})
        raise_alert("new reason", call)
        self.assertEqual(call.methods(), ["alert.list", "alert.oneshot_create"])


class ClearAlertTest(unittest.TestCase):
    def test_clears_only_ours(self):
        call = FakeMidclt({"alert.list": [OURS, OTHER]})
        clear_alert(call)
        self.assertEqual(call.calls[-1], ("alert.oneshot_delete", (ALERT_CLASS, None), True))

    def test_keeps_class_when_foreign_present(self):
        call = FakeMidclt({"alert.list": [OURS, DOCKER]})
        clear_alert(call)
        self.assertEqual(call.methods(), ["alert.list"])

    def test_nothing_to_clear(self):
        call = FakeMidclt({"alert.list": [DOCKER]})
        clear_alert(call)
        self.assertEqual(call.methods(), ["alert.list"])


class RegisterTest(unittest.TestCase):
    def test_creates_init_script_and_cron_job(self):
        call = FakeMidclt({"initshutdownscript.query": [], "cronjob.query": []})
        register(Path("/mnt/tank/apps/ugreen-fan"), call)
        init = next(p[0] for m, p, _ in call.calls if m == "initshutdownscript.create")
        cron = next(p[0] for m, p, _ in call.calls if m == "cronjob.create")
        self.assertEqual(init["command"], "/mnt/tank/apps/ugreen-fan/bin/ugreen-fan load")
        self.assertEqual((init["when"], init["type"], init["comment"]), ("POSTINIT", "COMMAND", "ugreen-fan"))
        self.assertEqual(cron["command"], "/mnt/tank/apps/ugreen-fan/bin/ugreen-fan check")
        self.assertEqual((cron["stdout"], cron["stderr"], cron["user"]), (False, False, "root"))
        self.assertEqual(cron["schedule"]["minute"], "7")

    def test_is_idempotent(self):
        call = FakeMidclt({"initshutdownscript.query": [{"id": 4}], "cronjob.query": [{"id": 9}]})
        register(Path("/repo"), call)
        self.assertEqual(call.methods(), ["initshutdownscript.query", "cronjob.query"])

    def test_unregister_deletes_tagged_entries(self):
        call = FakeMidclt({"initshutdownscript.query": [{"id": 4}], "cronjob.query": [{"id": 9}]})
        unregister(call)
        self.assertIn(("initshutdownscript.delete", (4,), False), call.calls)
        self.assertIn(("cronjob.delete", (9,), False), call.calls)
