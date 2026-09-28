"""Running external commands without assuming where they are installed.

The original code hardcoded ``/usr/bin/hyprctl`` and friends, which is an FHS
assumption that quietly breaks on NixOS, on distros that split packages, and
anywhere a tool lands in ``/usr/local/bin``. Everything here resolves through
``PATH`` and fails with the tool's own message when it is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess

DEFAULT_TIMEOUT = 12


class ToolMissing(OSError):
    """A required command is not installed."""


def which(name: str) -> str:
    """Resolve a command through PATH, or explain that it is not there."""
    found = shutil.which(name)
    if not found:
        raise ToolMissing(f"{name} is not installed.")
    return found


def have(name: str) -> bool:
    return shutil.which(name) is not None


def run(args: list[str], timeout: int = DEFAULT_TIMEOUT, check: bool = True) -> str:
    """Run a command and return stdout, or raise with something readable."""
    try:
        done = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise ToolMissing(f"{args[0]} is not installed.") from error
    except subprocess.TimeoutExpired as error:
        raise OSError(f"{args[0]} did not answer within {timeout}s.") from error
    if check and done.returncode != 0:
        message = (done.stderr or done.stdout or "").strip().splitlines()
        raise OSError(message[0] if message else f"{args[0]} exited {done.returncode}")
    return done.stdout


def run_json(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> object:
    """Run a command and parse its stdout as JSON."""
    raw = run(args, timeout=timeout)
    try:
        return json.loads(raw or "null")
    except json.JSONDecodeError as error:
        raise OSError(f"{args[0]} returned output that is not JSON: {error}") from error
