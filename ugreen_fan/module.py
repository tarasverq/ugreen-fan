"""Kernel module loading, BIOS hand-back and the transient systemd unit."""

import logging
import os
import subprocess
import time
from pathlib import Path

from .config import Config
from .hwmon import HWMON_ROOT, Fan, SensorError, find_chip, find_hwmon

MODULE = "it87"
RUN_DIR = Path("/run/ugreen-fan")
STATE_FILE = RUN_DIR / "state.json"
BIOS_PWM_FILE = RUN_DIR / "bios_pwm"
UNIT_NAME = "ugreen-fan.service"
UNIT_PATH = Path("/run/systemd/system") / UNIT_NAME
DMI_PRODUCT = Path("/sys/class/dmi/id/product_name")
PROC_MODULES = Path("/proc/modules")
CHIP_TIMEOUT = 5.0

log = logging.getLogger(__name__)


class SetupError(Exception):
    pass


def check_model(supported: tuple[str, ...], force: bool, dmi_path: Path = DMI_PRODUCT) -> str:
    model = dmi_path.read_text().strip()
    if model not in supported and not force:
        raise SetupError(f"model '{model}' is not in supported_models {list(supported)}; "
                         "adjust config.toml or pass --force")
    return model


def module_file(repo: Path, release: str) -> Path:
    return repo / "modules" / release / f"{MODULE}.ko"


def is_loaded(proc_modules: Path = PROC_MODULES) -> bool:
    return any(line.split(" ", 1)[0] == MODULE for line in proc_modules.read_text().splitlines())


def service_active() -> bool:
    result = subprocess.run(["systemctl", "is-active", "--quiet", UNIT_NAME], check=False)
    return result.returncode == 0


def unit_text(repo: Path, chip: str, pwm: int) -> str:
    command = f'"{repo / "bin" / "ugreen-fan"}"'
    # run and failsafe must agree on the channel even if config.toml is edited later
    channel = f"--chip {chip} --pwm {pwm}"
    return (
        "[Unit]\n"
        "Description=UGREEN fan regulator\n"
        "After=local-fs.target\n"
        "\n"
        "[Service]\n"
        "Type=notify\n"
        f"ExecStart={command} run {channel}\n"
        f"ExecStopPost={command} failsafe {channel}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "WatchdogSec=30\n"
    )


def save_bios_pwm(fan: Fan, path: Path = BIOS_PWM_FILE) -> None:
    if path.exists() or fan.enable() != 2:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{fan.duty()}\n")


def _output(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise SetupError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _run(*args: str) -> None:
    _output(*args)


def insert_module(ko: Path, params: str) -> None:
    """insmod does not resolve dependencies (it87 needs hwmon-vid), so modprobe them first."""
    depends = _output("modinfo", "-F", "depends", str(ko)).strip()
    for dependency in filter(None, depends.split(",")):
        _run("modprobe", dependency)
    _run("insmod", str(ko), *params.split())


def _wait_for_chip(name: str, root: Path = HWMON_ROOT) -> Path:
    deadline = time.monotonic() + CHIP_TIMEOUT
    while not find_hwmon(name, root):
        if time.monotonic() > deadline:
            raise SetupError(f"hwmon '{name}' did not appear after loading {MODULE}")
        time.sleep(0.2)
    return find_chip(name, root)


def load(config: Config, repo: Path, force: bool) -> None:
    model = check_model(config.supported_models, force)
    release = os.uname().release
    ko = module_file(repo, release)
    if not ko.exists():
        raise SetupError(f"{ko} not found: module not built for kernel {release}, run build.sh")
    if not is_loaded():
        insert_module(ko, config.module_params)
        log.info("Loaded %s on %s", ko, model)
    fan = Fan(_wait_for_chip(config.chip), config.pwm, config.fan)
    save_bios_pwm(fan)
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(unit_text(repo, config.chip, config.pwm))
    _run("systemctl", "daemon-reload")
    _run("systemctl", "restart", UNIT_NAME)
    log.info("Started %s", UNIT_NAME)


def restore(config: Config) -> None:
    loaded = is_loaded()
    if loaded and not BIOS_PWM_FILE.exists():
        raise SetupError(f"{BIOS_PWM_FILE} is missing, cannot restore the BIOS curve; "
                         "reboot to let the BIOS re-initialise the fan controller")
    if UNIT_PATH.exists():
        _run("systemctl", "stop", UNIT_NAME)
        UNIT_PATH.unlink()
        _run("systemctl", "daemon-reload")
    if loaded:
        bios_pwm = int(BIOS_PWM_FILE.read_text())
        try:
            Fan(find_chip(config.chip), config.pwm, config.fan).set_auto(bios_pwm)
        except SensorError as e:
            raise SetupError(f"could not hand the fan back to BIOS: {e}") from e
        _run("rmmod", MODULE)
        BIOS_PWM_FILE.unlink()
        log.info("Fan returned to BIOS control (start PWM %d), %s unloaded", bios_pwm, MODULE)
