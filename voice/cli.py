"""`voice` command line: starts the daemon or talks to a running one."""
from __future__ import annotations

import argparse
import logging
import sys
import time

from voice import APP_NAME, __version__
from voice.ipc import NEXT_LANGUAGE, PENDING_LANGUAGE, IPCError, is_running, send

#: How long, and how often, `language next` asks the daemon what it landed on.
#: The switch happens on the daemon's Qt thread after the reply, so a single
#: immediate read can still see the language we asked it to leave.
LANGUAGE_POLL_TIMEOUT_S = 1.5
LANGUAGE_POLL_INTERVAL_S = 0.05

log = logging.getLogger(__name__)

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


#: A shortcut the desktop registered and attached no key to. It is not "bound":
#: nothing we can do makes a press arrive, only the user in their settings.
UNASSIGNED_SHORTCUT = ("registered, no key assigned \u2014 assign it in your desktop's "
                       "keyboard settings")
DENIED_SHORTCUT = "NOT BOUND (accept the desktop's shortcut dialog)"


def _shortcut_row(reply: dict, keyboard) -> str:
    """What the portal actually did with our shortcuts, in one line.

    `shortcut_state` is what a current daemon reports; the bool fallback keeps a
    `voice status` run against an older one readable.
    """
    state = reply.get("shortcut_state")
    triggers = reply.get("shortcut_triggers") or {}
    if state == "unassigned":
        # Naming the ids matters as soon as there is more than one: "no key
        # assigned" with `language_toggle` working reads as a total failure.
        missing = ", ".join(sid for sid, trigger in triggers.items() if not trigger)
        return f"{UNASSIGNED_SHORTCUT} ({missing})" if missing else UNASSIGNED_SHORTCUT
    if state == "denied":
        return DENIED_SHORTCUT
    if state == "bound":
        keys = ", ".join(f"{sid}={trigger}" for sid, trigger in triggers.items())
        return f"bound ({keys})" if keys else "bound"
    if keyboard is None:
        return "waiting for the desktop"
    return "bound" if keyboard else DENIED_SHORTCUT


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
        rows.append(("shortcuts", _shortcut_row(reply, keyboard)))
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
    before = None
    if args.cmd == "profile":
        request["name"] = args.name
    if args.cmd == "language":
        request["code"] = args.code
        if args.code.strip().lower() == NEXT_LANGUAGE:
            # What we are cycling away from, so the read-back below can tell a
            # daemon that has not applied the switch yet from one that has.
            try:
                before = _current_language()
            except IPCError as exc:
                log.debug("could not read the language before switching: %s", exc)
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
        print(f"language: {_language_after(reply, before)}")
    return 0


def _language_after(reply: dict, before: str | None = None) -> str:
    """What the daemon actually switched to.

    `next` is resolved on the daemon's Qt thread - two toggles in a row must be
    two steps - so its reply names no language and `status` reads the result
    back. That read races the Qt thread, so it is repeated until the language
    has left `before`, briefly and with a bound: a toggle is allowed to be a
    no-op (a one-entry cycle, an entry the daemon refuses), and then the last
    language read is the right answer.
    """
    language = reply.get("language")
    if language != PENDING_LANGUAGE:
        return str(language)
    deadline = time.monotonic() + LANGUAGE_POLL_TIMEOUT_S
    while True:
        try:
            latest = _current_language()
        except IPCError as exc:
            # The switch itself already happened; only the read-back failed.
            return f"switched (could not read it back: {exc})"
        if latest is None:
            return "switched (the daemon did not say what to)"
        if latest != before or time.monotonic() >= deadline:
            return latest
        time.sleep(LANGUAGE_POLL_INTERVAL_S)


def _current_language() -> str | None:
    """The language the daemon reports now; None when its reply does not say."""
    language = send({"cmd": "status"}).get("language")
    return None if language is None else str(language)


if __name__ == "__main__":
    sys.exit(main())
