"""The first-run wizard: three questions, asked once.

It exists because the two settings that matter most on a new machine - where
several gigabytes of model are about to land, and which model each language
uses - were buried in a tab nobody opens until something is already wrong.
"""
import pytest
from PySide6.QtCore import Qt

from voice.config import Config
from voice.ui.wizard import PAGES, SetupWizard


def make(qapp):
    cfg = Config.load()
    return cfg, SetupWizard(cfg)


def test_a_new_install_is_asked_and_a_settled_one_is_not(isolated_xdg):
    cfg = Config.load()
    assert cfg.needs_setup() is True
    cfg.set("general.setup_complete", True)
    assert cfg.needs_setup() is False


def test_it_walks_forward_and_back_through_every_page(qapp, isolated_xdg):
    cfg, wiz = make(qapp)
    assert wiz.page_index == 0
    assert not wiz.back_button.isEnabled(), "nowhere to go back to from the first page"
    for expected in range(1, len(PAGES)):
        wiz.next_button.click()
        assert wiz.page_index == expected
    assert wiz.next_button.text() == "Finish", "the last page ends it"
    wiz.back_button.click()
    assert wiz.page_index == len(PAGES) - 2
    assert wiz.next_button.text() == "Next"


def test_finishing_writes_the_answers_and_marks_it_done(qapp, isolated_xdg, tmp_path):
    cfg, wiz = make(qapp)
    wiz.model_dir_edit.setText(str(tmp_path / "models"))
    wiz.model_combos["en"].setCurrentText("medium")
    wiz.model_combos["sv"].setCurrentText("KBLab/kb-whisper-medium")
    wiz.finish()

    again = Config.load()
    assert again.get("stt.model_dir") == str(tmp_path / "models")
    assert again.get("stt.profiles.local.model") == "medium"
    assert again.get("stt.profiles.local-swedish.model") == "KBLab/kb-whisper-medium"
    assert again.needs_setup() is False
    assert again.errors() == []


def test_leaving_everything_alone_keeps_the_shipped_answers(qapp, isolated_xdg):
    """Next, Next, Finish must not be a way to break a working default."""
    cfg, wiz = make(qapp)
    wiz.finish()
    again = Config.load()
    assert again.get("stt.model_dir") == ""
    assert again.get("stt.profiles.local.model") == "large-v3"
    assert again.get("stt.profiles.local-swedish.model") == "KBLab/kb-whisper-large"
    assert again.needs_setup() is False


def test_closing_without_finishing_changes_nothing_and_asks_again(qapp, isolated_xdg):
    """A wizard escaped from is not a wizard answered."""
    cfg, wiz = make(qapp)
    wiz.model_dir_edit.setText("/srv/models")
    wiz.reject()
    again = Config.load()
    assert again.get("stt.model_dir") == ""
    assert again.needs_setup() is True, "it has to come back next time"


def test_a_folder_it_could_not_use_is_refused_before_it_is_saved(qapp, isolated_xdg):
    """The wizard is the last place to catch this: afterwards the failure shows
    up as a transcription error blaming the GPU."""
    cfg, wiz = make(qapp)
    wiz.model_dir_edit.setText("models")            # relative: config refuses it
    assert wiz.finish() is False
    assert wiz.error_label.text()
    assert Config.load().needs_setup() is True


def test_every_language_in_the_cycle_gets_a_row(qapp, isolated_xdg):
    cfg, wiz = make(qapp)
    assert sorted(wiz.model_combos) == ["en", "sv"]


def test_the_model_rows_recommend_the_shipped_pair(qapp, isolated_xdg):
    from voice.ui.settings import RECOMMENDED_MODELS

    cfg, wiz = make(qapp)
    assert wiz.model_combos["en"].currentText() == "large-v3"
    assert wiz.model_combos["sv"].currentText() == "KBLab/kb-whisper-large"
    for model in RECOMMENDED_MODELS:
        assert model in [wiz.model_combos["en"].itemText(i)
                         for i in range(wiz.model_combos["en"].count())]


def test_the_pages_say_what_is_about_to_happen(qapp, isolated_xdg):
    """Each page has a heading and prose; an unexplained form is not a wizard."""
    cfg, wiz = make(qapp)
    for index, page in enumerate(PAGES):
        assert page.title and page.body, f"page {index} says nothing"
        assert len(page.body) > 40


def test_a_language_with_no_profile_is_not_offered_a_model(qapp, isolated_xdg):
    """Someone who dictates German has no German profile to point at; the row
    would write a model into a table that does not exist."""
    cfg = Config.load()
    cfg.set("general.languages", ["en", "de"])
    cfg.save()
    wiz = SetupWizard(Config.load())
    assert sorted(wiz.model_combos) == ["en"]


def test_the_setup_command_runs_it_against_the_real_config(qapp, isolated_xdg, monkeypatch):
    """`voice setup` is the only way back to these questions once answered."""
    from voice import paths
    from voice.cli import main

    seen = {}

    class FakeWizard:
        def __init__(self, cfg, parent=None):
            seen["config"] = cfg

        def exec(self):
            seen["ran"] = True
            return 1

    monkeypatch.setattr("voice.ui.wizard.SetupWizard", FakeWizard)
    monkeypatch.setattr("voice.cli.is_running", lambda: False)
    assert main(["setup"]) == 0
    assert seen["ran"] is True
    assert seen["config"].path == paths.config_file()


def test_answering_it_tells_a_running_daemon_to_reload(qapp, isolated_xdg, monkeypatch):
    """Otherwise the daemon keeps the model the wizard was opened to change."""
    from voice.cli import main

    sent = []
    monkeypatch.setattr("voice.ui.wizard.SetupWizard",
                        lambda cfg, parent=None: type("W", (), {"exec": lambda self: 1})())
    monkeypatch.setattr("voice.cli.is_running", lambda: True)
    monkeypatch.setattr("voice.cli.send", lambda req: sent.append(req) or {"ok": True})
    assert main(["setup"]) == 0
    assert sent == [{"cmd": "reload"}]
