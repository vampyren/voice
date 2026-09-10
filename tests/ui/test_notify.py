from voice.ui.notify import Notifier


def test_notify_spawns_notify_send():
    calls = []
    n = Notifier(run=lambda argv, **kw: calls.append(argv))
    n.notify("Title", "Body", "critical")
    argv = calls[0]
    assert argv[0] == "notify-send" and "--urgency" in argv and "critical" in argv
    assert argv[-2:] == ["Title", "Body"]


def test_notify_disabled_and_missing_binary_are_silent():
    calls = []
    n = Notifier(enabled=False, run=lambda argv, **kw: calls.append(argv))
    n.notify("a", "b")
    assert calls == []

    def missing(argv, **kw):
        raise FileNotFoundError("notify-send")
    Notifier(run=missing).notify("a", "b")      # must not raise
