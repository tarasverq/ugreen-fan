import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakesys import FakeSysfs
from ugreen_fan.config import Source
from ugreen_fan.hwmon import SensorError
from ugreen_fan.readings import read_source

CURVE = ((40.0, 100), (50.0, 200))


def disks() -> Source:
    return Source("disks", "drivetemp", 1, (1.0, 80.0), CURVE)


class ReadSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sys = FakeSysfs(self.root)
        module = self.root / "module" / "drivetemp"
        module.mkdir(parents=True)
        patcher = mock.patch("ugreen_fan.readings.DRIVETEMP_MODULE", module)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.module = module

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_bay_labelled_by_ata_port(self):
        self.sys.add("drivetemp", {"temp1_input": 47000}, block="sdb", port="ata1")
        self.sys.add("drivetemp", {"temp1_input": 48000}, block="sda", port="ata3")
        self.assertEqual(read_source(disks(), self.root), {"bay1": 47.0, "bay3": 48.0})

    def test_hot_added_disk_is_picked_up(self):
        self.sys.add("drivetemp", {"temp1_input": 47000}, block="sda", port="ata1")
        self.assertEqual(read_source(disks(), self.root), {"bay1": 47.0})
        self.sys.add("drivetemp", {"temp1_input": 50000}, block="sdc", port="ata4")
        self.assertEqual(read_source(disks(), self.root), {"bay1": 47.0, "bay4": 50.0})

    def test_usb_disks_are_ignored(self):
        self.sys.add("drivetemp", {"temp1_input": 47000}, block="sda", port="ata1")
        self.sys.add("drivetemp", {"temp1_input": 70000}, block="sdc", port="usb3")
        self.assertEqual(read_source(disks(), self.root), {"bay1": 47.0})

    def test_no_disks_in_bays_contributes_nothing(self):
        self.sys.add("drivetemp", {"temp1_input": 47000}, block="sdc", port="usb3")
        self.assertEqual(read_source(disks(), self.root), {})

    def test_drivetemp_not_loaded_is_an_error(self):
        self.module.rmdir()
        with self.assertRaisesRegex(SensorError, "drivetemp module is not loaded"):
            read_source(disks(), self.root)

    def test_out_of_range(self):
        self.sys.add("drivetemp", {"temp1_input": 0}, block="sda", port="ata1")
        with self.assertRaisesRegex(SensorError, "outside valid range"):
            read_source(disks(), self.root)

    def test_chip_channel(self):
        self.sys.add("it8613", {"temp1_input": 49000, "temp2_input": 34000})
        source = Source("cpu", "it8613", 1, (1.0, 110.0), CURVE)
        self.assertEqual(read_source(source, self.root), {"it8613/temp1": 49.0})

    def test_chip_missing(self):
        source = Source("cpu", "it8613", 1, (1.0, 110.0), CURVE)
        with self.assertRaises(SensorError):
            read_source(source, self.root)
