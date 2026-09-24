import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ugreen_fan import cli
from ugreen_fan.config import ConfigError


class FailsafeCommandTest(unittest.TestCase):
    def test_failsafe_does_not_read_config(self):
        with mock.patch.object(cli, "load_config", side_effect=ConfigError("broken")) as load, \
             mock.patch.object(cli, "find_chip", return_value="/hwmon") as find, \
             mock.patch.object(cli.Fan, "set_manual") as set_manual:
            self.assertEqual(cli.main(["failsafe", "--chip", "it8613", "--pwm", "3"]), 0)
        load.assert_not_called()
        find.assert_called_once_with("it8613")
        set_manual.assert_called_once_with(255)


class CheckCommandTest(unittest.TestCase):
    def run_check(self, problem):
        with mock.patch.object(cli, "load_config", return_value=mock.Mock(truenas_alert=True, interval=10)), \
             mock.patch.object(cli, "_probe_problem", return_value=problem), \
             mock.patch.object(cli, "raise_alert") as raise_alert, \
             mock.patch.object(cli, "clear_alert") as clear_alert:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["check"])
        return code, out.getvalue(), raise_alert, clear_alert

    def test_healthy_is_silent_and_clears(self):
        code, out, raise_alert, clear_alert = self.run_check(None)
        self.assertEqual((code, out), (0, ""))
        clear_alert.assert_called_once()
        raise_alert.assert_not_called()

    def test_problem_prints_alerts_and_fails(self):
        code, out, raise_alert, clear_alert = self.run_check("service down")
        self.assertEqual(code, 1)
        self.assertIn("service down", out)
        raise_alert.assert_called_once_with("service down")
        clear_alert.assert_not_called()

    def test_broken_config_is_reported_on_stdout(self):
        with mock.patch.object(cli, "load_config", side_effect=ConfigError("bad curve")), \
             mock.patch.object(cli, "raise_alert") as raise_alert:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["check"])
        self.assertEqual(code, 1)
        self.assertIn("bad curve", out.getvalue())
        raise_alert.assert_called_once()


class RunCommandTest(unittest.TestCase):
    def test_refuses_when_config_moved_to_another_channel(self):
        config = mock.Mock(chip="it8613", pwm=2)
        with mock.patch.object(cli, "load_config", return_value=config), \
             mock.patch.object(cli, "run") as run:
            self.assertEqual(cli.main(["run", "--chip", "it8613", "--pwm", "3"]), 1)
        run.assert_not_called()

    def test_runs_when_channel_matches(self):
        config = mock.Mock(chip="it8613", pwm=3)
        with mock.patch.object(cli, "load_config", return_value=config), \
             mock.patch.object(cli, "run", return_value=0) as run:
            self.assertEqual(cli.main(["run", "--chip", "it8613", "--pwm", "3"]), 0)
        run.assert_called_once()


class UninstallCommandTest(unittest.TestCase):
    def test_unregisters_even_if_restore_fails(self):
        with mock.patch.object(cli, "load_config"), \
             mock.patch.object(cli, "unregister") as unregister, \
             mock.patch.object(cli, "restore", side_effect=cli.SetupError("bios_pwm missing")):
            self.assertEqual(cli.main(["uninstall"]), 1)
        unregister.assert_called_once()
