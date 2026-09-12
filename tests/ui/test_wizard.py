"""The first-run wizard: three questions, asked once.

It exists because the two settings that matter most on a new machine - where
several gigabytes of model are about to land, and which model each language
uses - were buried in a tab nobody opens until something is already wrong.
"""
import pytest
from PySide6.QtCore import Qt

from voice import paths
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
    wiz.model_combos["local"].setCurrentText("medium")
    wiz.model_combos["local-swedish"].setCurrentText("KBLab/kb-whisper-medium")
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


def test_every_profile_a_language_uses_gets_a_row(qapp, isolated_xdg):
    """One row per profile, labelled with the languages that pick it: two rows
    writing the same profile meant one answer silently beat the other."""
    cfg, wiz = make(qapp)
    assert sorted(wiz.model_combos) == ["local", "local-swedish"]
    assert wiz.model_row_labels == {"local": "English", "local-swedish": "Swedish"}


def test_the_model_rows_recommend_the_shipped_pair(qapp, isolated_xdg):
    from voice.ui.settings import RECOMMENDED_MODELS

    cfg, wiz = make(qapp)
    assert wiz.model_combos["local"].currentText() == "large-v3"
    assert wiz.model_combos["local-swedish"].currentText() == "KBLab/kb-whisper-large"
    chooser = wiz.model_combos["local"]
    for model in RECOMMENDED_MODELS:
        assert model in [chooser.itemText(i) for i in range(chooser.count())]


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
    assert sorted(wiz.model_combos) == ["local"]
    assert wiz.model_row_labels["local"] == "English"


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


# -- what the review found ----------------------------------------------------

def test_finishing_keeps_what_changed_while_it_was_open(qapp, isolated_xdg):
    """Finding 2. The wizard held a whole document and wrote it back, so a
    language switched from the tray while it sat open was silently undone."""
    cfg, wiz = make(qapp)
    outside = Config.load()
    outside.set("general.language", "sv")
    outside.set("stt.active", "openai")
    outside.save()

    wiz.model_combos["local"].setCurrentText("medium")
    assert wiz.finish() is True
    again = Config.load()
    assert again.get("general.language") == "sv", "the tray's switch was reverted"
    assert again.get("stt.active") == "openai"
    assert again.get("stt.profiles.local.model") == "medium"


def test_an_unrelated_broken_setting_does_not_trap_the_wizard(qapp, isolated_xdg):
    """Finding 3, and the one that could make voice unusable.

    finish() validated the whole file. A bad key the wizard has no row for
    blocked Finish, left setup_complete false, and the daemon reopened the modal
    wizard on every start - with no way to finish it and no way to reach the key.
    """
    broken = Config.load()
    broken.set("audio.max_seconds", 0)
    broken.save()

    wiz = SetupWizard(Config.load())
    assert wiz.finish() is True, wiz.error_label.text()
    again = Config.load()
    assert again.needs_setup() is False, "it would ask again at every start"
    assert again.get("audio.max_seconds") == 0, "and it must not silently fix it"


def test_its_own_answers_are_still_checked(qapp, isolated_xdg):
    """The escape hatch above must not let the wizard write its own nonsense."""
    cfg, wiz = make(qapp)
    wiz.model_dir_edit.setText("models")            # relative: refused
    assert wiz.finish() is False
    assert "model_dir" in wiz.error_label.text()
    assert Config.load().needs_setup() is True


def test_an_upgraded_install_is_still_asked_which_model(qapp, isolated_xdg):
    """Finding 4. An upgraded config has no pairing, so every row vanished -
    on exactly the installs docs/usage.md sends to `voice setup`."""
    path = paths.config_file()
    path.write_text(BEFORE_PAIRING)
    wiz = SetupWizard(Config.load(path))
    assert wiz.model_combos, "the page was blank"
    assert "local" in wiz.model_combos
    wiz.model_combos["local"].setCurrentText("small")
    assert wiz.finish() is True
    assert Config.load(path).get("stt.profiles.local.model") == "small"


def test_the_model_page_does_not_promise_a_pair_it_is_not_offering(qapp, isolated_xdg):
    """One profile and no pairing still read "Which model for each language"
    over "the recommended pair is already selected" - above a single row."""
    paired = SetupWizard(Config.load())          # the shipped config, both paired
    paired.next_button.click()
    paired.next_button.click()
    assert "each language" in paired.title_label.text()
    assert "recommended pair" in paired.body_label.text().lower()

    # The same page on an upgraded config, which pairs nothing.
    path = paths.config_file()
    path.write_text(BEFORE_PAIRING)
    wiz = SetupWizard(Config.load(path))
    wiz.next_button.click()
    wiz.next_button.click()
    assert "each language" not in wiz.title_label.text()
    # It may still say how to pair them later - what it must not do is claim a
    # pair is selected when one row is on screen.
    assert "recommended pair" not in wiz.body_label.text().lower()


def test_two_languages_sharing_a_profile_get_one_row(qapp, isolated_xdg):
    """Finding 5. Two rows wrote the same key, so the second silently won."""
    cfg = Config.load()
    cfg.set("general.language_profiles", {"en": "local", "sv": "local"})
    cfg.save()
    wiz = SetupWizard(Config.load())
    assert list(wiz.model_combos) == ["local"]
    assert wiz.model_row_labels["local"] == "English, Swedish"
    wiz.model_combos["local"].setCurrentText("medium")
    assert wiz.finish() is True
    assert Config.load().get("stt.profiles.local.model") == "medium"


def test_the_setup_command_says_whether_it_was_answered(qapp, isolated_xdg, monkeypatch):
    """Finding 9. Finish, "Not now" and a wizard given up on all exited 0."""
    from voice.cli import main

    monkeypatch.setattr("voice.cli.is_running", lambda: False)
    monkeypatch.setattr("voice.ui.wizard.SetupWizard",
                        lambda cfg, parent=None: type("W", (), {"exec": lambda self: 1})())
    assert main(["setup"]) == 0
    monkeypatch.setattr("voice.ui.wizard.SetupWizard",
                        lambda cfg, parent=None: type("W", (), {"exec": lambda self: 0})())
    assert main(["setup"]) == 1


#: A config.toml from before the language pairing existed: one local profile,
#: no general.language_profiles at all. What every upgraded install looks like.
BEFORE_PAIRING = """\
[general]
language = "en"
languages = ["en", "sv"]

[hotkeys]
backend = "auto"
dictate = "KEY_F13"
dictate_mode = "hold"
portal_dictate = "CTRL+space"

[audio]
max_seconds = 120

[stt]
active = "local"

[stt.profiles.local]
backend = "local"
model = "large-v3-turbo"
device = "cuda"
compute_type = "float16"
"""
