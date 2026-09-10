"""`voice` command line: starts the daemon or talks to a running one."""
from __future__ import annotations

import argparse
import logging
import sys

from voice import APP_NAME, __version__
from voice.ipc import IPCError, is_running, send

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
    sub.add_parser("doctor", help="check this machine for everything voice needs")
    return p


def _print_status(reply: dict) -> None:
    print(f"state:    {reply.get('state')}")
    print(f"profile:  {reply.get('profile')}")
    print(f"backend:  {reply.get('backend')}")
    print(f"hotkeys:  {reply.get('hotkey_backend', 'unknown')}")
    keyboard = reply.get("keyboard")
    kb_text = "ok" if keyboard else ("unknown" if keyboard is None else "NO ACCESS")
    print(f"keyboard: {kb_text}")
    if reply.get("last_error"):
        print(f"error:    {reply['last_error']}")


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
