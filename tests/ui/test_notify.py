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


def test_a_notifier_with_no_injected_runner_is_caught_by_the_suite_guard():
    """The default runner is looked up when it is used, so the suite-wide guard
    in conftest can intercept it: a test that reaches the owner's real desktop
    fails instead of popping a notification on it."""
    import pytest

    with pytest.raises(AssertionError, match="real desktop"):
        Notifier().notify("Profile is gone", "'openai' is no longer defined")
