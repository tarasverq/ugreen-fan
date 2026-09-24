import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakesys import FakeSysfs
from ugreen_fan.config import Config, Source
from ugreen_fan.daemon import Regulator, StallGuard, State, read_state, write_state
from ugreen_fan.hwmon import Fan

DISK_CURVE = ((40.0, 100), (50.0, 200))
CPU_CURVE = ((60.0, 51), (90.0, 255))


def make_config(min_pwm: int = 51) -> Config:
    return Config(
        supported_models=("DXP4800",), module_params="", chip="it8613", pwm=3, fan=3,
        interval=10, hysteresis=2, min_pwm=min_pwm, truenas_alert=True,
        sources=(
            Source("disks", "drivetemp", 1, (1.0, 80.0), DISK_CURVE),
            Source("cpu", "it8613", 1, (1.0, 110.0), CPU_CURVE),
        ),
    )


class RegulatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sys = FakeSysfs(self.root)
        self.chip = self.sys.add("it8613", {"pwm3": 51, "pwm3_enable": 2, "fan3_input": 900,
                                            "temp1_input": 50000})
        self.sda = self.sys.add("drivetemp", {"temp1_input": 45000}, block="sda", port="ata1")
        self.fan = Fan(self.chip, 3, 3)
        self.regulator = Regulator(make_config(), self.fan, self.root, clock=lambda: 1000.0)
        module = self.root / "module" / "drivetemp"
        module.mkdir(parents=True)
        patcher = mock.patch("ugreen_fan.readings.DRIVETEMP_MODULE", module)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def set_disk(self, celsius: float):
        (self.sda / "temp1_input").write_text(f"{int(celsius * 1000)}\n")

    def pwm(self) -> str:
        return self.sys.read(self.chip, "pwm3")

    def test_first_step_starts_from_failsafe_and_uses_shifted_curve(self):
        state = self.regulator.step()
        self.assertEqual(state.mode, "normal")
        self.assertEqual(state.pwm, 170)  # disks: curve(45 + 2)
        self.assertEqual(self.pwm(), "170")
        self.assertEqual(self.sys.read(self.chip, "pwm3_enable"), "1")
        self.assertEqual(state.temps, {"bay1": 45.0, "it8613/temp1": 50.0})
        self.assertEqual(state.rpm, 900)
        self.assertEqual(state.updated, 1000.0)

    def test_hotter_source_wins(self):
        (self.chip / "temp1_input").write_text("90000\n")
        self.assertEqual(self.regulator.step().pwm, 255)

    def test_min_pwm_floor(self):
        self.set_disk(30)
        regulator = Regulator(make_config(min_pwm=120), self.fan, self.root)
        regulator.step()
        self.assertEqual(regulator.step().pwm, 120)

    def test_hysteresis_holds_then_falls(self):
        self.regulator.step()                       # 170
        self.set_disk(45)
        self.assertEqual(self.regulator.step().pwm, 170)
        self.set_disk(42)
        self.assertEqual(self.regulator.step().pwm, 140)

    def test_unreadable_disk_goes_failsafe(self):
        self.regulator.step()
        (self.sda / "temp1_input").unlink()
        state = self.regulator.step()
        self.assertEqual((state.mode, state.pwm, self.pwm()), ("failsafe", 255, "255"))
        self.assertIn("temp1_input", state.reason)

    def test_invalid_reading_goes_failsafe(self):
        self.set_disk(0)
        self.assertEqual(self.regulator.step().mode, "failsafe")
        self.assertEqual(self.pwm(), "255")

    def test_recovers_after_failsafe(self):
        self.set_disk(0)
        self.regulator.step()
        self.set_disk(45)
        state = self.regulator.step()
        self.assertEqual((state.mode, state.pwm), ("normal", 170))

    def test_no_disks_follows_cpu_curve(self):
        self.regulator.step()
        for entry in (self.sda / "device" / "block" / "sda", self.sda / "device" / "block"):
            entry.rmdir()
        (self.sda / "device").unlink()
        (self.chip / "temp1_input").write_text("75000\n")
        state = self.regulator.step()
        self.assertEqual((state.mode, state.temps), ("normal", {"it8613/temp1": 75.0}))
        self.assertEqual(state.pwm, 153)  # cpu rises: curve(75); disks add nothing

    def test_missing_tachometer_is_not_fatal(self):
        (self.chip / "fan3_input").unlink()
        state = self.regulator.step()
        self.assertEqual((state.mode, state.rpm), ("normal", None))


class StateFileTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "state.json"
            write_state(State("normal", None, 128, 1000, {"sda": 45.0}, 1.5), path)
            self.assertEqual(read_state(path), {"mode": "normal", "reason": None, "pwm": 128,
                                                "rpm": 1000, "temps": {"sda": 45.0}, "updated": 1.5})

    def test_missing_file(self):
        self.assertIsNone(read_state(Path("/nonexistent/state.json")))

    def test_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{")
            self.assertIsNone(read_state(path))


class StallGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sys = FakeSysfs(Path(self.tmp.name))
        self.chip = self.sys.add("it8613", {"pwm3": 100, "pwm3_enable": 1})
        self.now = 0.0
        self.guard = StallGuard(Fan(self.chip, 3, 3), limit=20, clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def test_quiet_while_steps_complete(self):
        self.now = 19
        self.assertFalse(self.guard.poll())
        self.assertEqual(self.sys.read(self.chip, "pwm3"), "100")

    def test_trips_once_when_step_hangs(self):
        self.now = 21
        self.assertTrue(self.guard.poll())
        self.assertEqual(self.sys.read(self.chip, "pwm3"), "255")
        (self.chip / "pwm3").write_text("100\n")
        self.now = 40
        self.assertFalse(self.guard.poll())
        self.assertEqual(self.sys.read(self.chip, "pwm3"), "100")

    def test_beat_rearms(self):
        self.now = 21
        self.guard.poll()
        self.guard.beat()
        self.now = 30
        self.assertFalse(self.guard.poll())
        self.now = 42
        self.assertTrue(self.guard.poll())
