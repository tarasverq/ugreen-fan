import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakesys import FakeSysfs
from ugreen_fan.hwmon import Fan
from ugreen_fan import module
from ugreen_fan.module import (SetupError, check_model, insert_module, is_loaded, module_file,
                               save_bios_pwm, unit_text)


class ModuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path

    def test_supported_model(self):
        dmi = self.write("product_name", "DXP4800\n")
        self.assertEqual(check_model(("DXP4800",), False, dmi), "DXP4800")

    def test_unsupported_model(self):
        dmi = self.write("product_name", "DXP8800 Plus\n")
        with self.assertRaisesRegex(SetupError, "--force"):
            check_model(("DXP4800",), False, dmi)

    def test_unsupported_model_forced(self):
        dmi = self.write("product_name", "DXP8800 Plus\n")
        self.assertEqual(check_model(("DXP4800",), True, dmi), "DXP8800 Plus")

    def test_module_file_per_kernel(self):
        self.assertEqual(module_file(Path("/repo"), "6.12.33-production+truenas"),
                         Path("/repo/modules/6.12.33-production+truenas/it87.ko"))

    def test_is_loaded(self):
        modules = self.write("modules", "drivetemp 16384 0 - Live 0x0\nit87 65536 0 - Live 0x0\n")
        self.assertTrue(is_loaded(modules))

    def test_is_not_loaded_prefix_match(self):
        modules = self.write("modules", "it87_wdt 16384 0 - Live 0x0\n")
        self.assertFalse(is_loaded(modules))

    def test_unit_failsafe_does_not_need_config(self):
        text = unit_text(Path("/mnt/tank/apps/ugreen-fan"), "it8613", 3)
        self.assertIn('ExecStart="/mnt/tank/apps/ugreen-fan/bin/ugreen-fan" run --chip it8613 --pwm 3', text)
        self.assertIn('ExecStopPost="/mnt/tank/apps/ugreen-fan/bin/ugreen-fan" failsafe --chip it8613 --pwm 3', text)
        for line in ("Type=notify", "Restart=always", "WatchdogSec=30"):
            self.assertIn(line, text)

    def test_save_bios_pwm_only_in_auto_mode_and_once(self):
        sys = FakeSysfs(self.dir / "hwmon")
        chip = sys.add("it8613", {"pwm3": 51, "pwm3_enable": 2})
        fan = Fan(chip, 3, 3)
        saved = self.dir / "run" / "bios_pwm"
        save_bios_pwm(fan, saved)
        self.assertEqual(saved.read_text().strip(), "51")
        fan.set_manual(200)
        save_bios_pwm(fan, saved)
        self.assertEqual(saved.read_text().strip(), "51")

    def test_save_bios_pwm_skips_manual_mode(self):
        sys = FakeSysfs(self.dir / "hwmon")
        chip = sys.add("it8613", {"pwm3": 200, "pwm3_enable": 1})
        saved = self.dir / "bios_pwm"
        save_bios_pwm(Fan(chip, 3, 3), saved)
        self.assertFalse(saved.exists())


class InsertModuleTest(unittest.TestCase):
    def test_loads_dependencies_before_insmod(self):
        # insmod does not resolve dependencies: it87 needs hwmon-vid (found after a reboot)
        with mock.patch.object(module, "_output", return_value="hwmon-vid\n") as output, \
             mock.patch.object(module, "_run") as run:
            insert_module(Path("/repo/it87.ko"), "ignore_resource_conflict=1")
        output.assert_called_once_with("modinfo", "-F", "depends", "/repo/it87.ko")
        self.assertEqual(run.call_args_list, [
            mock.call("modprobe", "hwmon-vid"),
            mock.call("insmod", "/repo/it87.ko", "ignore_resource_conflict=1"),
        ])

    def test_no_dependencies(self):
        with mock.patch.object(module, "_output", return_value="\n"), \
             mock.patch.object(module, "_run") as run:
            insert_module(Path("/repo/it87.ko"), "")
        self.assertEqual(run.call_args_list, [mock.call("insmod", "/repo/it87.ko")])
