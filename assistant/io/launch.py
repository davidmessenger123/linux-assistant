"""Starting applications, without uwsm or a compositor-specific launcher.

The original launched everything through ``uwsm-app``, which is part of the
systemd user session stack. It is a reasonable choice under Omarchy and means
nothing on a system without systemd -- Gentoo, some NixOS configurations, and
anything using a session that does not go through systemd -- where the assistant
simply could not open an application.

A desktop file and the GIO helper that resolves it are on every desktop, so
that is the path taken here. The command-line launchers stay as fallbacks.
"""

from __future__ import annotations

import os
import subprocess
import sys

from .. import proc

SEARCH_HINTS = ("Terminal", "Text Editor", "Web Browser", "File Manager", "Calculator")


class LaunchError(RuntimeError):
    """The application could not be found or started."""


def _application_dirs() -> list[str]:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [os.path.join(data_home, "applications")] + [
        os.path.join(prefix, "applications") for prefix in data_dirs.split(":") if prefix
    ]


def _desktop_files() -> list[str]:
    found: list[str] = []
    for directory in _application_dirs():
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        found.extend(
            os.path.join(directory, name) for name in entries if name.endswith(".desktop")
        )
    return found


def _read_desktop(path: str) -> dict:
    """Pull the fields out of a desktop entry, skipping groups and comments."""
    fields: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith(("#", "[")):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                fields.setdefault(key.strip(), value.strip())
    except OSError:
        return {}
    return fields


def _score(fields: dict, needle: str) -> int:
    """How well a desktop entry matches a name the model asked for. Lower is better."""
    needle = needle.lower().replace(" ", "")
    candidates = [
        fields.get("Name", ""),
        fields.get("GenericName", ""),
        fields.get("Keywords", ""),
        fields.get("Exec", "").split()[0] if fields.get("Exec") else "",
    ]
    best = 99
    for candidate in candidates:
        candidate = candidate.lower()
        if not candidate:
            continue
        flat = candidate.replace(" ", "")
        if flat == needle:
            return 0
        if flat.startswith(needle):
            best = min(best, 1)
        elif needle in flat:
            best = min(best, 2)
    return best


def search(query: str, limit: int = 12) -> list[dict]:
    """Find desktop entries matching a name, best first."""
    if not str(query or "").strip():
        return []
    matches: list[tuple[int, dict]] = []
    for path in _desktop_files():
        fields = _read_desktop(path)
        if fields.get("NoDisplay", "").lower() == "true" and not fields.get("Name"):
            continue
        if fields.get("Type", "Application") not in ("Application", ""):
            continue
        rank = _score(fields, query)
        if rank < 99:
            matches.append((rank, {
                "id": os.path.splitext(os.path.basename(path))[0],
                "name": fields.get("Name", ""),
                "generic": fields.get("GenericName", ""),
                "keywords": fields.get("Keywords", ""),
                "path": path,
            }))
    matches.sort(key=lambda pair: (pair[0], pair[1]["name"].lower()))
    return [entry for _rank, entry in matches[:limit]]


def find(query: str) -> dict:
    matches = search(query, limit=1)
    if not matches:
        raise LaunchError(
            f"No application matches '{query}'. "
            "Check the name, or use a command line if the program is not installed as a desktop entry."
        )
    return matches[0]


def _is_application_entry(path: str) -> bool:
    """Whether a path looks like a real installed application entry.

    Checked again at launch time rather than trusted from the caller, because
    this is the last point before a program starts.
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    if not path.startswith("/") or ".." in path or not path.endswith(".desktop"):
        return False
    parts = path.split("/")
    if len(parts) < 3:
        return False
    return any(part == "applications" for part in parts[:-1])


def launch(query: str, prefer: str = "", source: str = "") -> dict:
    """Start an application by name, or by a desktop entry path already vetted.

    ``source`` exists for callers that validated a path themselves and must not
    have it re-derived from a name. A model-supplied name is never treated as a
    path: it is looked up in the application directories, so a name like
    ``../../etc/passwd`` finds nothing instead of launching something.
    """
    if source:
        if not _is_application_entry(source):
            raise LaunchError(f"{source} is not an application entry, so it was not started.")
        entry = {
            "id": os.path.splitext(os.path.basename(source))[0],
            "name": os.path.splitext(os.path.basename(source))[0],
            "path": source,
            "generic": "",
            "keywords": "",
        }
    else:
        entry = find(query)
    order = [prefer] if prefer else ["gio", "gtk-launch", "direct"]

    problems: list[str] = []
    for method in order:
        try:
            if method == "gio":
                if proc.have("gio"):
                    proc.run(["gio", "launch", entry["path"]], timeout=20)
                    return {"ok": True, "method": method, "name": entry["name"], "id": entry["id"]}
                raise LaunchError("gio is not installed")
            if method == "gtk-launch":
                if proc.have("gtk-launch"):
                    proc.run(["gtk-launch", entry["id"]], timeout=20)
                    return {"ok": True, "method": method, "name": entry["name"], "id": entry["id"]}
                raise LaunchError("gtk-launch is not installed")
            _launch_exec(entry["path"])
            return {"ok": True, "method": "direct", "name": entry["name"], "id": entry["id"]}
        except Exception as error:  # noqa: BLE001 - try the next launcher
            problems.append(f"{method}: {error}")
    raise LaunchError(f"'{entry['name']}' could not be started. " + " | ".join(problems))


def _launch_exec(path: str) -> None:
    """Read Exec out of the desktop file and run it detached.

    Only reached when neither GIO nor gtk-launch is present, which is rare but
    does happen on a minimal install.
    """
    fields = _read_desktop(path)
    command = fields.get("Exec", "")
    if not command:
        raise LaunchError("the desktop entry has no Exec line")
    import shlex

    parts = shlex.split(command.replace("%u", "").replace("%U", "").replace("%f", "").replace("%F", ""))
    if not parts:
        raise LaunchError("the desktop entry's Exec line was empty after expansion")
    subprocess.Popen(parts, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Find and start desktop applications.")
    sub = parser.add_subparsers(dest="command", required=True)
    search_parser = sub.add_parser("search", help="list matching desktop entries")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=12)
    launch_parser = sub.add_parser("launch", help="start an application")
    launch_parser.add_argument("query")
    launch_parser.add_argument("--prefer", default="")
    args = parser.parse_args(argv)

    try:
        if args.command == "search":
            print(json.dumps({"ok": True, "applications": search(args.query, args.limit)}))
        else:
            print(json.dumps(launch(args.query, args.prefer)))
        return 0
    except (LaunchError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)[:400]}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
