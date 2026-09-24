"""Command-line entry point."""

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

from .config import Config, ConfigError, load_config
from .daemon import FAILSAFE_PWM, read_state, run
from .health import diagnose
from .hwmon import Fan, SensorError, find_chip
from .module import STATE_FILE, SetupError, is_loaded, load, restore, service_active
from .truenas import TrueNASError, clear_alert, raise_alert, register, unregister

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config.toml"
EXAMPLE = REPO / "config.example.toml"

log = logging.getLogger("ugreen_fan")


def cmd_load(args: argparse.Namespace) -> int:
    load(load_config(CONFIG), REPO, args.force)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(CONFIG)
    if (config.chip, config.pwm) != (args.chip, args.pwm):
        raise ConfigError(f"config.toml now drives {config.chip} pwm{config.pwm}, but the service "
                          f"was set up for {args.chip} pwm{args.pwm}; run 'ugreen-fan load'")
    return run(config, STATE_FILE)


def cmd_failsafe(args: argparse.Namespace) -> int:
    Fan(find_chip(args.chip), args.pwm, 0).set_manual(FAILSAFE_PWM)
    return 0


def _pwm_enable(chip: str, pwm: int) -> int | None:
    try:
        return Fan(find_chip(chip), pwm, 0).enable()
    except SensorError:
        return None


def _probe_problem(config: Config) -> str | None:
    return diagnose(module_loaded=is_loaded(), service_active=service_active(),
                    state=read_state(STATE_FILE), pwm_enable=_pwm_enable(config.chip, config.pwm),
                    now=time.time(), interval=config.interval)


def cmd_check(args: argparse.Namespace) -> int:
    alert = True
    try:
        config = load_config(CONFIG)
        alert = config.truenas_alert
        problem = _probe_problem(config)
    except ConfigError as e:
        problem = f"config error: {e}"
    try:
        if problem is None:
            if alert:
                clear_alert()
            return 0
        print(problem)
        if alert:
            raise_alert(problem)
    except TrueNASError as e:
        print(f"could not update TrueNAS alert: {e}")
    return 1


def cmd_restore(args: argparse.Namespace) -> int:
    restore(load_config(CONFIG))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(read_state(STATE_FILE), indent=2))
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    if not CONFIG.exists():
        shutil.copyfile(EXAMPLE, CONFIG)
        log.info("Created %s from the example", CONFIG)
    config = load_config(CONFIG)
    register(REPO)
    log.info("Registered POSTINIT script and hourly Cron Job in TrueNAS")
    load(config, REPO, args.force)
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    unregister()
    log.info("Removed POSTINIT script and Cron Job from TrueNAS")
    restore(load_config(CONFIG))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ugreen-fan", description="UGREEN NAS fan regulator")
    commands = parser.add_subparsers(required=True, metavar="command")

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        return sub

    add("load", cmd_load, "load the module and start the regulator (POSTINIT)") \
        .add_argument("--force", action="store_true", help="skip the model check")
    for name, handler, help_text in (
            ("run", cmd_run, "run the regulator in the foreground (systemd)"),
            ("failsafe", cmd_failsafe, "set the fan to full speed")):
        sub = add(name, handler, help_text)
        sub.add_argument("--chip", required=True)
        sub.add_argument("--pwm", type=int, required=True)
    add("check", cmd_check, "health check for the TrueNAS Cron Job")
    add("restore", cmd_restore, "stop regulating and hand the fan back to BIOS")
    add("status", cmd_status, "print the regulator state")
    add("install", cmd_install, "register in TrueNAS and start") \
        .add_argument("--force", action="store_true", help="skip the model check")
    add("uninstall", cmd_uninstall, "restore BIOS control and unregister from TrueNAS")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    try:
        return args.handler(args)
    except (ConfigError, SetupError, SensorError, TrueNASError) as e:
        log.error("%s", e)
        return 1
