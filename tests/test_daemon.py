import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from voice.config import Config
from voice.daemon import Daemon, hotkey_specs
from voice.hotkey.keyspec import parse_keyspec
from voice.pipeline import State


class FakeListener:
    def __init__(self):
        self.started = False
        self.specs = None

    def start(self): self.started = True
    def stop(self): self.started = False
    def capture_next(self, cb): self.cb = cb
    def modifiers_held(self): return False
    def devices_ok(self): return True


class FakeSender:
    name = "fake"
    def send_chord(self, codes): pass
    def available(self): return True


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_hotkey_specs_reads_all_bindings(isolated_xdg):
    cfg = Config.load()
    cfg.set("hotkeys.recall", "KEY_F14")
    specs = hotkey_specs(cfg)
    assert specs["dictate"] == parse_keyspec("KEY_F13")
    assert specs["recall"] == parse_keyspec("KEY_F14")
    assert specs["cancel"] == parse_keyspec("KEY_ESC")


def test_handle_commands_and_profile_switch(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda profile, secret: type("T", (), {
        "name": profile["backend"], "describe": lambda self: f"fake {profile['model']}",
        "warmup": lambda self: None, "transcribe": lambda self, *a: None})())
    cfg = Config.load()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender())
    d.build()
    assert d.handle({"cmd": "ping"}) == {"ok": True}
    st = d.handle({"cmd": "status"})
    assert st["ok"] and st["state"] == "idle" and st["profile"] == "local" and "large-v3-turbo" in st["backend"]
    assert d.handle({"cmd": "profile", "name": "openai"})["ok"]
    assert Config.load().get("stt.active") == "openai"
    assert "gpt-transcribe" in d.handle({"cmd": "status"})["backend"]
    bad = d.handle({"cmd": "profile", "name": "ghost"})
    assert bad["ok"] is False and "ghost" in bad["error"]
    assert d.handle({"cmd": "nope"})["ok"] is False
    d.shutdown()


def test_apply_config_rebinds_hotkeys(isolated_xdg, qapp, monkeypatch):
    monkeypatch.setattr("voice.daemon.make_transcriber", lambda p, s: type("T", (), {
        "name": "x", "describe": lambda self: "x", "warmup": lambda self: None})())
    cfg = Config.load()
    d = Daemon(cfg, listener=FakeListener(), sender=FakeSender())
    d.build()
    cfg.set("hotkeys.dictate", "KEY_F20")
    cfg.save()
    d.apply_config()
    assert d.tracker.feed(parse_keyspec("KEY_F20").codes.__iter__().__next__(), 1) == [("dictate", "press")]
    d.shutdown()
