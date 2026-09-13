"""The guide, which is four pages of prose and nothing else.

It replaced an interactive first-run wizard that wrote config. Eleven defects
over three review rounds lived in the asking, not in the settings being asked
about - so it asks nothing, and the tests below mostly exist to keep it that
way.
"""
import pytest

from voice.ui.guide import PAGES, Guide


def test_it_walks_forward_and_back_through_every_page(qapp):
    guide = Guide()
    assert guide.page_index == 0
    assert not guide.back_button.isEnabled(), "nowhere to go back to"
    for expected in range(1, len(PAGES)):
        guide.next_button.click()
        assert guide.page_index == expected
    assert guide.next_button.text() == "Done"
    guide.back_button.click()
    assert guide.page_index == len(PAGES) - 2
    assert guide.next_button.text() == "Next"


def test_done_closes_it(qapp):
    guide = Guide()
    for _ in range(len(PAGES)):
        guide.next_button.click()
    assert not guide.isVisible()


def test_every_page_says_something(qapp):
    for page in PAGES:
        assert page.title and len(page.body) > 60, page.title


def test_it_never_reads_or_writes_the_config(qapp):
    """The whole reason this is not a wizard any more.

    A dialog that only describes the program cannot be wrong about its state,
    cannot trap the owner behind a failed validation, and cannot race anything.
    A control here that changes something is how the last version started.
    """
    import inspect

    from voice.ui import guide

    source = inspect.getsource(guide)
    for forbidden in ("Config", "cfg", ".set(", ".save(", "errors()"):
        assert forbidden not in source, (
            f"the guide touches {forbidden!r}; every setting it might want to "
            f"change has a tested row in the settings window instead")


def test_it_opens_without_a_config_at_all(qapp, tmp_path, monkeypatch):
    """Not even indirectly: it must not care whether voice is set up."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nothing"))
    guide = Guide()
    assert guide.title_label.text()


def test_the_guide_command_opens_it(qapp, isolated_xdg, monkeypatch):
    from voice.cli import main

    shown = []
    monkeypatch.setattr("voice.ui.guide.Guide",
                        lambda parent=None: type("G", (), {"exec": lambda self: shown.append(1) or 1})())
    assert main(["guide"]) == 0
    assert shown == [1]


def test_the_guide_command_is_in_the_help(qapp, isolated_xdg, capsys):
    from voice.cli import main

    with pytest.raises(SystemExit):
        main(["--help"])
    assert "guide" in capsys.readouterr().out
