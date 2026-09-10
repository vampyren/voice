"""`voice` command line: starts the daemon or talks to a running one."""
from __future__ import annotations

import argparse
import logging
import sys

from voice import APP_NAME, __version__
from voice.ipc import PENDING_LANGUAGE, IPCError, is_running, send

SIMPLE = ["start", "stop", "toggle", "cancel", "recall", "retry", "status", "settings", "quit", "reload"]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=APP_NAME, description="Wayland-native voice typing")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("daemon", help="run the background daemon (default when none is running)")
    for name in SIMPLE:
        sub.add_parser(name)
    prof = sub.add_parser("profile", help="switch transcription profile")
    prof.add_argument("name")
    lang = sub.add_parser("language", help="switch dictation language")
    lang.add_argument("code", help="a two-letter code, 'auto', or 'next' to cycle "
                                   "through general.languages")
    sub.add_parser("doctor", help="check this machine for everything voice needs")
    return p


def _print_status(reply: dict) -> None:
    for_language = reply.get("profile_language")
    # Only when a language actually chose it: see daemon.profile_for_status.
    chosen_by = f" (for {for_language})" if for_language else ""
    rows = [("state", reply.get("state")),
            ("profile", f"{reply.get('profile')}{chosen_by}"),
            ("backend", reply.get("backend")),
            ("language", reply.get("language")),
            ("hotkeys", reply.get("hotkey_backend", "unknown")),
            ("overlay", reply.get("overlay", "unknown"))]
    keyboard = reply.get("keyboard")
    if reply.get("hotkey_backend") == "portal":
        # There is no keyboard to have access to on this backend: the desktop
        # either bound our shortcuts, refused them, or has not answered yet.
        if keyboard is None:
            bound = "waiting for the desktop"
        else:
            bound = "bound" if keyboard else "NOT BOUND (accept the desktop's shortcut dialog)"
        rows.append(("shortcuts", bound))
    else:
        rows.append(("keyboard", "ok" if keyboard else ("unknown" if keyboard is None else "NO ACCESS")))
    if reply.get("last_error"):
        rows.append(("error", reply["last_error"]))
    # Measured, not a constant: "shortcuts" is as wide as the old padding, so a
    # fixed width put that one value a column right of every other row.
    width = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        print(f"{label + ':':<{width}} {value}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "doctor":
        from voice.doctor import run_doctor
        return run_doctor()
    if args.cmd in (None, "daemon"):
        if is_running():
            try:
                send({"cmd": "settings"})
            except IPCError:
                pass
            return 0
        from voice.daemon import main as daemon_main
        return daemon_main()
    request = {"cmd": args.cmd}
    if args.cmd == "profile":
        request["name"] = args.name
    if args.cmd == "language":
        request["code"] = args.code
    try:
        reply = send(request)
    except IPCError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 1
    if not reply.get("ok"):
        print(f"{APP_NAME}: {reply.get('error', 'failed')}", file=sys.stderr)
        return 1
    if args.cmd == "status":
        _print_status(reply)
    elif args.cmd == "language":
        print(f"language: {_language_after(reply)}")
    return 0


def _language_after(reply: dict) -> str:
    """What the daemon actually switched to.

    `next` is resolved on the daemon's Qt thread - two toggles in a row must be
    two steps - so its reply names no language and one `status` reads back the
    result instead.
    """
    language = reply.get("language")
    if language != PENDING_LANGUAGE:
        return str(language)
    try:
        return str(send({"cmd": "status"}).get("language"))
    except IPCError as exc:
        # The switch itself already happened; only the read-back failed.
        return f"switched (could not read it back: {exc})"


if __name__ == "__main__":
    sys.exit(main())
