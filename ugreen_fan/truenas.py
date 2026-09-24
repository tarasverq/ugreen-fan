"""TrueNAS middleware integration through midclt: alerts and boot/cron registration."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

# TrueNAS SCALE has no custom-text alert class; this built-in one-shot class takes a
# free-form "error" argument. Its args are the alert key, so ours never collide with
# Docker's own alert — but oneshot_delete always removes the whole class.
ALERT_CLASS = "ApplicationsStartFailed"
ALERT_PREFIX = "[ugreen-fan]"
TAG = "ugreen-fan"
CRON_MINUTE = "7"

Call = Callable[..., Any]


class TrueNASError(Exception):
    pass


def midclt(method: str, *params: Any, job: bool = False) -> Any:
    args = ["midclt", "call", *(["-j"] if job else []), method, *(json.dumps(p) for p in params)]
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TrueNASError(f"midclt {method}: {e}") from e
    if result.returncode:
        raise TrueNASError(f"midclt {method}: {result.stderr.strip() or result.stdout.strip()}")
    output = result.stdout.strip()
    return None if job or not output else json.loads(output)


def _is_ours(alert: dict[str, Any]) -> bool:
    args = alert.get("args")
    return isinstance(args, dict) and str(args.get("error", "")).startswith(ALERT_PREFIX)


def _class_alerts(call: Call) -> list[dict[str, Any]]:
    return [a for a in call("alert.list") if a.get("klass") == ALERT_CLASS]


def raise_alert(reason: str, call: Call = midclt) -> None:
    error = f"{ALERT_PREFIX} {reason}"
    alerts = _class_alerts(call)
    if any(isinstance(a.get("args"), dict) and a["args"].get("error") == error for a in alerts):
        return
    if alerts and all(_is_ours(a) for a in alerts):
        call("alert.oneshot_delete", ALERT_CLASS, None, job=True)
    call("alert.oneshot_create", ALERT_CLASS, {"error": error}, job=True)


def clear_alert(call: Call = midclt) -> None:
    alerts = _class_alerts(call)
    if alerts and all(_is_ours(a) for a in alerts):
        call("alert.oneshot_delete", ALERT_CLASS, None, job=True)


def register(repo: Path, call: Call = midclt) -> None:
    command = repo / "bin" / "ugreen-fan"
    if not call("initshutdownscript.query", [["comment", "=", TAG]]):
        call("initshutdownscript.create", {
            "type": "COMMAND", "command": f"{command} load", "when": "POSTINIT",
            "timeout": 60, "comment": TAG, "enabled": True,
        })
    if not call("cronjob.query", [["description", "=", TAG]]):
        call("cronjob.create", {
            "user": "root", "command": f"{command} check", "description": TAG,
            "schedule": {"minute": CRON_MINUTE, "hour": "*", "dom": "*", "month": "*", "dow": "*"},
            "stdout": False, "stderr": False, "enabled": True,
        })


def unregister(call: Call = midclt) -> None:
    for entry in call("initshutdownscript.query", [["comment", "=", TAG]]):
        call("initshutdownscript.delete", entry["id"])
    for entry in call("cronjob.query", [["description", "=", TAG]]):
        call("cronjob.delete", entry["id"])
