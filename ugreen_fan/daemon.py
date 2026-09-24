"""Control loop: temperatures → PWM, with failsafe, state file and systemd watchdog."""

import json
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .curve import next_pwm
from .hwmon import HWMON_ROOT, Fan, SensorError, find_chip
from .readings import read_source

FAILSAFE_PWM = 255
STALL_INTERVALS = 2  # a step taking longer than this many intervals is hung

log = logging.getLogger(__name__)


@dataclass
class State:
    mode: str
    reason: str | None
    pwm: int
    rpm: int | None
    temps: dict[str, float]
    updated: float


class Regulator:
    def __init__(self, config: Config, fan: Fan, root: Path = HWMON_ROOT,
                 clock: Callable[[], float] = time.time):
        self.config = config
        self.fan = fan
        self.root = root
        self.clock = clock
        self.levels = {source.name: FAILSAFE_PWM for source in config.sources}

    def step(self) -> State:
        try:
            temps, target = self._compute()
        except SensorError as e:
            self.levels = dict.fromkeys(self.levels, FAILSAFE_PWM)
            self.fan.set_manual(FAILSAFE_PWM)
            return State("failsafe", str(e), FAILSAFE_PWM, self._rpm(), {}, self.clock())
        self.fan.set_manual(target)
        return State("normal", None, target, self._rpm(), temps, self.clock())

    def _compute(self) -> tuple[dict[str, float], int]:
        temps: dict[str, float] = {}
        levels = dict(self.levels)
        for source in self.config.sources:
            readings = read_source(source, self.root)
            if not readings:
                levels[source.name] = 0
                continue
            levels[source.name] = next_pwm(source.curve, max(readings.values()),
                                           levels[source.name], self.config.hysteresis)
            temps.update(readings)
        self.levels = levels
        return temps, max(self.config.min_pwm, *levels.values())

    def _rpm(self) -> int | None:
        try:
            return self.fan.rpm()
        except SensorError:
            return None


class StallGuard:
    """Forces full speed if a control step hangs, e.g. in the kernel on a failing disk.

    A process stuck in uninterruptible I/O cannot be killed by the systemd watchdog,
    so ExecStopPost would never run; this guard writes to the fan chip from another thread.
    """

    def __init__(self, fan: Fan, limit: float, clock: Callable[[], float] = time.monotonic):
        self.fan = fan
        self.limit = limit
        self.clock = clock
        self.last = clock()
        self.tripped = False

    def beat(self) -> None:
        self.last = self.clock()
        self.tripped = False

    def poll(self) -> bool:
        if self.tripped or self.clock() - self.last <= self.limit:
            return False
        self.fan.set_manual(FAILSAFE_PWM)
        self.tripped = True
        return True


def _watch_stalls(guard: StallGuard, stop: threading.Event) -> None:
    while not stop.wait(1):
        try:
            if guard.poll():
                log.error("Control step hung for over %gs, fan forced to %d", guard.limit, FAILSAFE_PWM)
        except SensorError:
            log.exception("Stall guard could not set failsafe PWM")


def write_state(state: State, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state)))
    os.replace(tmp, path)


def read_state(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def sd_notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(message.encode(), address)


def run(config: Config, state_path: Path, root: Path = HWMON_ROOT) -> int:
    fan = Fan(find_chip(config.chip, root), config.pwm, config.fan)
    regulator = Regulator(config, fan, root)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())

    guard = StallGuard(fan, STALL_INTERVALS * config.interval)
    threading.Thread(target=_watch_stalls, args=(guard, stop), daemon=True).start()

    log.info("Regulating %s pwm%d every %gs", config.chip, config.pwm, config.interval)
    previous_mode = None
    try:
        while not stop.is_set():
            state = regulator.step()
            guard.beat()
            if state.mode != previous_mode:
                if state.mode == "failsafe":
                    log.warning("Failsafe, fan at %d: %s", FAILSAFE_PWM, state.reason)
                else:
                    log.info("Normal mode, pwm %d, temps %s", state.pwm, state.temps)
                previous_mode = state.mode
            write_state(state, state_path)
            sd_notify("READY=1\nWATCHDOG=1")
            stop.wait(config.interval)
    finally:
        try:
            fan.set_manual(FAILSAFE_PWM)
        except SensorError:
            log.exception("Could not set failsafe PWM on exit")
    log.info("Stopped, fan left at %d", FAILSAFE_PWM)
    return 0
