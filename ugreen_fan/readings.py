"""Turning a configured source into validated temperature readings."""

from pathlib import Path

from .config import Source
from .hwmon import HWMON_ROOT, SensorError, drivetemp_ports, find_chip, read_temp

DRIVETEMP_MODULE = Path("/sys/module/drivetemp")


def read_source(source: Source, root: Path = HWMON_ROOT) -> dict[str, float]:
    if source.driver == "drivetemp":
        sensors = _bay_sensors(root)
    else:
        sensors = {f"{source.driver}/temp{source.channel}": find_chip(source.driver, root)}
    temps = {label: read_temp(hwmon, source.channel) for label, hwmon in sensors.items()}
    low, high = source.valid
    for label, temp in temps.items():
        if not low <= temp <= high:
            raise SensorError(f"{label}: {temp:.1f} °C is outside valid range {low:g}..{high:g}")
    return temps


def _bay_sensors(root: Path) -> dict[str, Path]:
    """Every SATA drive, labelled by its ATA port; USB disks have no ATA port and are skipped.

    Empty bays are fine (the source then adds nothing), but a missing drivetemp module
    would hide hot disks, so that is an error.
    """
    if not DRIVETEMP_MODULE.exists():
        raise SensorError("drivetemp module is not loaded, disk temperatures are unknown")
    ports = drivetemp_ports(root)
    return {f"bay{port.removeprefix('ata')}": hwmon for port, hwmon in sorted(ports.items())}
