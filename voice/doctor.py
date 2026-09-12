"""`voice doctor`: tells the owner what works on this machine and what to fix."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from voice import APP_ID, __version__
from voice.hotkey.portal_listener import NO_TRIGGER, STATE_BOUND, STATE_UNASSIGNED
from voice.inject.injector import insertion_status, pill_policy
from voice.inject.window import (effective_window_command, is_plasma,
                                 terminal_chord_is_unreachable)
from voice.ui.overlay_client import pill_takes_focus, probe_helper
from voice.ui.placement import NO_LAYER_SHELL_NOTE

REQUIRED = {"portal", "wl-clipboard", "pw-record", "keyboard access", "config"}

#: Where GNOME keeps the trigger the user confirmed in its shortcut dialog. It
#: is the desktop's copy, not ours: once it exists, GNOME keeps binding it and
#: ignores a later hotkeys.portal_* change (verified on GNOME 49).
GNOME_SHORTCUTS_KEY = f"/org/gnome/settings-daemon/global-shortcuts/{APP_ID}/shortcuts"
DCONF_TIMEOUT_S = 2

#: Where the user assigns the key. The app appears there under its own name.
ASSIGN_WHERE = "Settings \u2192 Keyboard \u2192 Keyboard Shortcuts"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _which(binary: str) -> tuple[bool, str]:
    path = shutil.which(binary)
    return (True, path) if path else (False, f"{binary} not found in PATH")


def _config() -> tuple[bool, str]:
    from voice.config import Config
    cfg = Config.load()
    errs = cfg.errors()
    return (not errs, "; ".join(errs) or str(cfg.path))


def _keyboard() -> tuple[bool, str]:
    from voice.hotkey.evdev_listener import list_keyboards
    devs = list_keyboards()
    if not devs:
        return False, "no readable keyboards: run install.sh (udev rule) or add yourself to the 'input' group"
    return True, ", ".join(d.name for d in devs)


def _readable_keyboards() -> bool:
    from voice.daemon import keyboards_are_readable
    return keyboards_are_readable()


def _hotkey_backend() -> tuple[bool, str]:
    """Informational: which hotkey listener the daemon would build here, and why."""
    from voice.config import Config
    from voice.daemon import choose_hotkey_backend, has_local_seat
    cfg = Config.load()
    keyboards, seat = _readable_keyboards(), has_local_seat()
    backend = choose_hotkey_backend(cfg, keyboards, seat)
    from voice.daemon import HOTKEY_BACKENDS
    if str(cfg.get("hotkeys.backend", "auto") or "auto").strip().lower() in HOTKEY_BACKENDS:
        return True, f"{backend} (set in config)"
    reasons = [why for why, ok in (("no readable keyboards", keyboards), ("no local seat", seat)) if not ok]
    if reasons:
        return True, f"{backend} ({', '.join(reasons)})"
    return True, backend


def _dconf_shortcuts() -> str:
    """What GNOME stored for our app id, or "" wherever that cannot be read.

    `dconf read` and nothing else: no new dependency, and a machine without
    dconf (KDE, a plain wlroots session) simply has nothing to report.
    """
    if not shutil.which("dconf"):
        return ""
    try:
        done = subprocess.run(["dconf", "read", GNOME_SHORTCUTS_KEY],
                              capture_output=True, text=True, timeout=DCONF_TIMEOUT_S)
    except Exception:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _daemon_shortcuts() -> dict | None:
    """What the running daemon's portal listener holds, or None if it cannot say.

    dconf shows GNOME's copy; this shows what the portal answered BindShortcuts
    with, which is the only thing that decides whether a press ever arrives. A
    daemon older than the field, one on evdev, or none at all: fall back.
    """
    try:
        from voice.ipc import is_running, send
        if not is_running():
            return None
        reply = send({"cmd": "status"})
    except Exception:
        return None
    if not reply.get("ok") or reply.get("hotkey_backend") != "portal":
        return None
    return reply if reply.get("shortcut_state") else None


def _render_shortcuts(state: str, triggers: dict) -> tuple[bool, str]:
    keys = ", ".join(f"{sid}={trigger or NO_TRIGGER}" for sid, trigger in triggers.items())
    if state == STATE_BOUND:
        return True, f"bound by the desktop: {keys}"
    if state == STATE_UNASSIGNED:
        return False, (f"{keys} - the desktop registered the shortcut and attached no key. "
                       f"Assign one in {ASSIGN_WHERE}; hotkeys.portal_* is only a first-run "
                       f"preference and cannot move it.")
    return False, (f"the desktop refused to bind our shortcuts. Accept its permission dialog, "
                   f"or run install.sh so the portal can resolve '{APP_ID}'.")


def _portal_shortcuts() -> tuple[bool, str]:
    """Who owns the trigger, and what it actually is right now.

    `hotkeys.portal_*` is only what we ask for on a first run. The desktop owns
    the trigger from then on, so this reports the effective one - from the
    running daemon where there is one, and from GNOME's own copy otherwise.
    """
    from voice.config import Config
    from voice.daemon import choose_hotkey_backend, has_local_seat
    cfg = Config.load()
    if choose_hotkey_backend(cfg, _readable_keyboards(), has_local_seat()) != "portal":
        return True, "not in use: the evdev backend reads hotkeys.* directly"
    live = _daemon_shortcuts()
    if live is not None:
        return _render_shortcuts(live["shortcut_state"], live.get("shortcut_triggers") or {})
    stored = _dconf_shortcuts()
    if stored:
        return True, (f"GNOME holds the trigger and will keep it whatever hotkeys.portal_* says "
                      f"(start voice for the effective one): {GNOME_SHORTCUTS_KEY} = {stored}")
    return True, (f"the desktop owns the trigger - assign it in {ASSIGN_WHERE} (GNOME stores it "
                  f"under {GNOME_SHORTCUTS_KEY}; KDE: the settings window's \"Change in the "
                  "desktop\" dialog), not in hotkeys.portal_*")


def _overlay() -> tuple[bool, str]:
    """Informational: whether the recording pill can run here, and through what."""
    from voice.config import Config
    cfg = Config.load()
    if not cfg.get("ui.overlay", True):
        return True, "disabled (ui.overlay = false)"
    probe = probe_helper()
    if probe.command is None:
        return True, f"unavailable: {probe.reason}"
    fallback = ("fallback window allowed, it will take focus"
                if cfg.get("ui.overlay_allow_fallback", False) else "the pill stays off")
    if probe.layer_shell:
        shell = "layer-shell ok"
    elif probe.layer_shell_unsupported:
        # Installed but inert (GNOME): "layer-shell absent" would send the owner
        # off to install a package that is already there.
        shell = ("layer-shell: installed but unsupported by this compositor "
                 f"- {fallback}")
    else:
        shell = f"layer-shell absent - {fallback}"
    line = f"enabled, helper via {probe.command[0]} (gtk4 ok, {shell})"
    # Only where it applies: on a compositor that gives the pill a real layer
    # surface none of this happens, and a sentence about it is noise.
    if pill_takes_focus(cfg, lambda: probe):
        line += f"; text insertion: {insertion_status(cfg, pill_policy(cfg, True))}"
    return True, line


def _pill_placement() -> tuple[bool, str]:
    """Informational: whether `ui.overlay_position` can be honoured here at all.

    The owner dragged the pill into a corner in the settings window and the pill
    kept appearing in the middle. That is what a plain GTK window on a
    compositor with no layer shell does - it cannot position itself, and GNOME
    has no zwlr_layer_shell_v1 - and the only record of it was a line in the
    daemon's log. It is not a failure: the placement is still recorded and still
    applies on a machine that has one.
    """
    from voice.config import Config
    cfg = Config.load()
    position, margin_x, margin_y = cfg.overlay_placement()
    where = f"{position} (margins {margin_x}/{margin_y})"
    if not cfg.get("ui.overlay", True):
        return True, f"{where}, but the pill is off (ui.overlay = false)"
    probe = probe_helper()
    if probe.command is None:
        return True, f"{where}, but no pill helper can run here"
    if probe.layer_shell:
        return True, f"{where}, honoured through gtk4-layer-shell"
    return True, f"{where} is saved, but not applied here: {NO_LAYER_SHELL_NOTE}"


def _sources() -> tuple[bool, str]:
    from voice.audio.capture import list_sources
    srcs = list_sources()
    return (bool(srcs), ", ".join(f"{s.description}{' *' if s.is_default else ''}" for s in srcs) or "no microphones")


def _portal() -> tuple[bool, str]:
    from voice.inject.portal import portal_available
    return (True, "RemoteDesktop portal v2+") if portal_available() else (False, "RemoteDesktop portal missing (xdg-desktop-portal-kde/gnome)")


def _cuda() -> tuple[bool, str]:
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        return False, f"ctranslate2 cuda probe failed: {exc}"
    if n == 0:
        return False, "no CUDA device; local transcription will run on CPU (install with --extra gpu on the NVIDIA PC)"
    return True, f"{n} CUDA device(s)"


def _hub_repository(model: str) -> str:
    """The repository faster-whisper will really fetch `model` from.

    The short names we ship are aliases: faster-whisper maps `large-v3-turbo`
    to `mobiuslabsgmbh/faster-whisper-large-v3-turbo` and `medium` to
    `Systran/faster-whisper-medium`, and the cache directory is named after the
    repository rather than the alias - so looking up the raw config value here
    reported "not downloaded yet" over a model that had been on disk all along,
    for the shipped default profile.

    faster-whisper's own table is asked, never copied: the copy `stt/local.py`
    used to keep was wrong, and removing it was right. A build without
    faster-whisper installed gets the name as it stands, which is the answer
    for anything already spelled as a repository.
    """
    try:
        from faster_whisper.utils import _MODELS
    except Exception:                       # not installed, or it moved
        return model
    return _MODELS.get(model, model)


def _model_cache() -> tuple[bool, str]:
    from voice.config import Config
    cfg = Config.load()
    _, profile = cfg.stt_profile()
    if profile.get("backend") != "local":
        return True, "active profile is cloud; nothing to cache"
    name = _hub_repository(str(profile["model"])).replace("/", "--")
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub" / f"models--{name}"
    return (True, str(hub)) if hub.exists() else (False, f"{profile['model']} not downloaded yet (first dictation downloads it)")


def _language_profiles() -> tuple[bool, str]:
    """Informational: which profile each language selects, if any.

    `config` already rejects a map naming a profile that is gone, so this line
    reports rather than judges - it is the one place the pairing can be read at a
    glance. (An unreadable config.toml still fails it, like every other probe.)
    """
    from voice.config import Config
    cfg = Config.load()
    mapping = cfg.language_profiles()
    if not mapping:
        return True, "none: general.language_profiles is empty"
    profiles = cfg.get("stt.profiles", {}) or {}
    return True, ", ".join(f"{code} \u2192 {name}" + ("" if name in profiles else " (not defined)")
                           for code, name in mapping.items())


def _clipboard() -> tuple[bool, str]:
    copy_ok, _ = _which("wl-copy")
    paste_ok, _ = _which("wl-paste")
    if copy_ok and paste_ok:
        return True, "wl-copy/wl-paste"
    missing = [b for b, ok in (("wl-copy", copy_ok), ("wl-paste", paste_ok)) if not ok]
    return False, f"install wl-clipboard (missing: {', '.join(missing)})"


def _inject_settings() -> dict:
    """The `[inject]` table, or an empty one if the config cannot be read."""
    from voice.config import Config
    try:
        return Config.load().get("inject", {}) or {}
    except Exception:
        return {}


def _daemon_window_command() -> tuple[str, bool] | None:
    """What the running daemon resolved and whether it still works, or None.

    Two separate facts. The daemon lives in the graphical session and doctor
    may not, so only the daemon can say what `effective_window_command` came
    out as where it matters - and only the daemon knows whether that command
    has since stopped answering, which leaves every paste blind while the
    configured string still looks perfectly healthy.
    """
    try:
        from voice.ipc import is_running, send
        if not is_running():
            return None
        reply = send({"cmd": "status"})
    except Exception:
        return None
    if not reply.get("ok") or "window_command" not in reply:
        return None                       # a daemon older than this field
    return str(reply["window_command"] or ""), bool(reply.get("window_command_usable", True))


def _pill_policy_now() -> str:
    """The pill policy the daemon would use here, or "none" if unknowable.

    Its own function so `_paste_target` has one seam per fact it depends on,
    and so a config that cannot be read degrades to "say nothing special"
    rather than to a wrong diagnosis.
    """
    from voice.config import Config
    try:
        cfg = Config.load()
        return pill_policy(cfg, pill_takes_focus(cfg))
    except Exception:
        return "none"


def _paste_target() -> tuple[bool, str]:
    """Can the text actually reach the window the owner is looking at?

    The one failure the injector can never detect for itself: a terminal
    pastes with Ctrl+Shift+V and does nothing at all with Ctrl+V, the
    compositor accepts either chord whatever has focus, and so a dictation
    into a terminal that received nothing looks exactly like one that worked.
    """
    settings = _inject_settings()
    mode = str(settings.get("mode", "paste")).strip().lower()
    if mode != "paste":
        return True, f"inject.mode = {mode}; the text is left on the clipboard on purpose"
    if _pill_policy_now() == "clipboard":
        # Pasting is configured, but the pill takes focus here and
        # inject.pill_focus = clipboard has already ruled the chord out. No
        # chord is sent, so no chord can be the wrong one.
        return True, ("inject.pill_focus = clipboard; no chord is sent here and "
                      "the text is left on the clipboard on purpose")
    if is_plasma(os.environ) and not settings.get("active_window_command", ""):
        # KWin is asked directly - a script it runs for us, answering over
        # D-Bus. There is no command to name and nothing to install.
        return True, "focused window read from KWin directly (no command needed)"
    # The running daemon's answer first. Doctor may well be running somewhere
    # the daemon is not - over SSH, from a TTY, from a unit with no graphical
    # environment - and diagnosing from *this* shell's XDG_CURRENT_DESKTOP
    # reports a Plasma box as unable to answer. Falling back to our own
    # environment is still worth doing; saying which one we read is what makes
    # a wrong answer recognisable as one.
    live = _daemon_window_command()
    if live is not None:
        command, usable = live
        if command and not usable:
            return False, (f"{command} stopped answering, so it is no longer asked and "
                           f"every dictation is now pasted without knowing the focused "
                           f"window. If this is KWin's queryWindowInfo it is an "
                           f"interactive window picker, not a query - set "
                           f"inject.active_window_command to something that answers on "
                           f"its own.")
        whose = "the running daemon"
    else:
        command, whose = None, ""
    if command is None:
        # `effective_window_command` reads a whole-config key; here we already
        # have just the `[inject]` table, so hand it the one value it asks for.
        configured = settings.get("active_window_command", "")
        command = effective_window_command(lambda key, default=None: configured, os.environ)
        whose = f"XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP') or '(unset)'}"
    unreachable, why = terminal_chord_is_unreachable(
        command, str(settings.get("paste_chord", "ctrl+v")),
        str(settings.get("terminal_chord", "ctrl+shift+v")),
        settings.get("terminal_classes", []))
    if unreachable:
        return False, f"{why} [read from {whose}]"
    return True, (f"focused window read with: {command} [via {whose}]" if command
                  else f"one chord for every window [read from {whose}]")


def _senders() -> tuple[bool, str]:
    found = [b for b in ("wtype", "ydotool") if shutil.which(b)]
    return True, ", ".join(found) or "none (portal is the primary path)"


def default_probes() -> dict[str, Callable[[], tuple[bool, str]]]:
    return {
        "python": lambda: (True, f"{sys.version.split()[0]} · voice {__version__}"),
        "config": _config,
        "keyboard access": _keyboard,
        "hotkey backend": _hotkey_backend,
        "portal shortcuts": _portal_shortcuts,
        "pw-record": lambda: _which("pw-record"),
        "microphones": _sources,
        "language profiles": _language_profiles,
        "wl-clipboard": _clipboard,
        "portal": _portal,
        "cuda": _cuda,
        "model cache": _model_cache,
        "overlay": _overlay,
        "pill placement": _pill_placement,
        "notify-send": lambda: _which("notify-send"),
        "paste target": _paste_target,
        "fallback senders": _senders,
    }


def run_checks(probes: dict[str, Callable[[], tuple[bool, str]]] | None = None) -> list[Check]:
    out = []
    for name, probe in (probes or default_probes()).items():
        try:
            ok, detail = probe()
        except Exception as exc:
            ok, detail = False, str(exc)
        out.append(Check(name, ok, detail))
    return out


def run_doctor() -> int:
    checks = run_checks()
    failed_required = False
    for c in checks:
        mark = "✔" if c.ok else "✘"
        tag = "" if c.ok or c.name in REQUIRED else " (optional)"
        print(f"{mark} {c.name}{tag}: {c.detail}")
        if not c.ok and c.name in REQUIRED:
            failed_required = True
    print("\nall required checks passed" if not failed_required else "\nfix the ✘ required items above")
    return 1 if failed_required else 0
