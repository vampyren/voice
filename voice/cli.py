"""`voice` command line: starts the daemon or talks to a running one."""
from __future__ import annotations

import argparse
import logging
import sys
import time

from voice import APP_NAME, __version__
from voice.ipc import NEXT_LANGUAGE, PENDING_LANGUAGE, IPCError, is_running, send

#: How long, and how often, `language` asks the daemon what it landed on.
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
        code = args.code.strip().lower()
        if code == NEXT_LANGUAGE:
            print(f"language: {_language_after(reply, before)}")
        else:
            return _confirm_language(code, reply)
    return 0


def _confirm_language(code: str, reply: dict) -> int:
    """0 once the daemon really is on the language it accepted, 1 if it is not.

    `handle()` validates the code and replies ok from the IPC thread; the
    switch itself happens afterwards on the Qt thread, and `_set_language`
    gives up silently when the config cannot be reloaded or saved. Reporting
    the reply on its own therefore printed `language: sv` and exited 0 for a
    daemon that stayed on `en`, so the reply is confirmed by a bounded read-back
    - the same poll `next` uses, and for the same race.
    """
    wanted = str(reply.get("language") or code)
    deadline = time.monotonic() + LANGUAGE_POLL_TIMEOUT_S
    while True:
        try:
            latest = _current_language()
        except IPCError as exc:
            # The switch may or may not have happened; either way we cannot say
            # that it did, and claiming it is what this is here to stop.
            print(f"{APP_NAME}: could not confirm the switch to {wanted}: {exc}",
                  file=sys.stderr)
            return 1
        if latest == wanted:
            print(f"language: {latest}")
            return 0
        if time.monotonic() >= deadline:
            print(f"{APP_NAME}: the daemon is still on "
                  f"{latest if latest else 'no language it will name'}, not {wanted}; "
                  "the switch did not take (see the daemon's log)", file=sys.stderr)
            return 1
        time.sleep(LANGUAGE_POLL_INTERVAL_S)


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
    if before is None:
        # The pre-switch read failed or named nothing, so a read-back has
        # nothing to be different from: the first one would return whatever the
        # daemon happens to say, which is the language being cycled away from
        # until the Qt thread gets to the switch. Better to name none at all.
        return "switched (could not read the previous language)"
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
