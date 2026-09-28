"""Reminders, without the omarchy command.

The original shelled out to ``omarchy reminder``, so the reminders feature did
nothing at all anywhere else -- there was no fallback and no warning beyond the
exec error surfacing in the transcript. On a system that happens to have systemd
the portable version uses ``systemd-run --user`` to schedule a notification,
which is the same mechanism without the Omarchy dependency. Everywhere else the
reminder is recorded and reported, and the user is told plainly that nothing will
fire.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class ReminderError(RuntimeError):
    """A reminder could not be set."""


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "linux-assistant" / "reminders.json"


def _load() -> list[dict]:
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save(items: list[dict]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def _prune(items: list[dict]) -> list[dict]:
    now = time.time()
    return [item for item in items if float(item.get("due", 0)) > now]


def systemd_available() -> bool:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    try:
        done = subprocess.run(
            ["systemd-run", "--user", "--scope", "true"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return done.returncode == 0


def _notify_available() -> bool:
    from .. import proc

    return proc.have("notify-send")


def set_reminder(minutes: float, message: str) -> dict:
    """Record a reminder and, where possible, schedule it to actually fire."""
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        raise ReminderError("A reminder needs a number of minutes.") from None
    if minutes <= 0:
        raise ReminderError("A reminder needs a positive number of minutes.")
    message = str(message or "").strip()
    if not message:
        raise ReminderError("A reminder needs something to say.")

    due = time.time() + minutes * 60
    entry = {
        "id": uuid.uuid4().hex[:12],
        "message": message,
        "minutes": minutes,
        "due": due,
        "due_text": datetime.fromtimestamp(due, timezone.utc).astimezone().strftime("%H:%M"),
        "scheduled": False,
    }

    entry["scheduled"] = _schedule(entry)
    items = _prune(_load()) + [entry]
    _save(items)

    if entry["scheduled"]:
        note = "A notification will appear when it is due."
    else:
        reason = "systemd is not available" if not systemd_available() else "notify-send is not installed"
        note = f"Recorded only: {reason}, so nothing will fire. It is still listed until it passes."
    return {"ok": True, "reminder": entry, "note": note}


def _schedule(entry: dict) -> bool:
    if not systemd_available() or not _notify_available():
        return False
    command = [
        "systemd-run", "--user", "--on-active", f"{max(entry['minutes'] / 60, 0.01):.4f}",
        "--timer-property=AccuracySec=1s",
        "--", "notify-send", "linux-assistant", entry["message"],
    ]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return done.returncode == 0


def show() -> dict:
    items = _prune(_load())
    if not _save(items) and not items:
        pass
    return {
        "ok": True,
        "reminders": items,
        "can_fire": systemd_available() and _notify_available(),
    }


def clear(reminder_id: str = "") -> dict:
    items = _load()
    if reminder_id:
        kept = [item for item in items if item.get("id") != reminder_id]
        removed = len(items) - len(kept)
        items = kept
    else:
        removed = len(items)
        items = []
    _save(items)
    return {"ok": True, "cleared": removed, "reminders": _prune(items)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Set and list reminders.")
    sub = parser.add_subparsers(dest="command", required=True)
    set_parser = sub.add_parser("set")
    set_parser.add_argument("minutes", type=float)
    set_parser.add_argument("message", nargs="+")
    sub.add_parser("show")
    clear_parser = sub.add_parser("clear")
    clear_parser.add_argument("id", nargs="?", default="")
    args = parser.parse_args(argv)

    try:
        if args.command == "set":
            result = set_reminder(args.minutes, " ".join(args.message))
        elif args.command == "show":
            result = show()
        else:
            result = clear(args.id)
        print(json.dumps(result))
        return 0
    except ReminderError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
