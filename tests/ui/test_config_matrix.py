"""The same windows, opened against the configurations people actually have.

Four of the five defects in the last review round were "open this window on a
machine set up like *that* and look at what state it is in" - a dead button
over an empty list, a tab opening on the wrong profile, a page with no rows
under text describing rows. Every one was found by a reviewer writing a
throwaway script against a config we already ship. That is a test, so here it
is as one: the configurations are the table, and every window has to be sane
in all of them.

Add a row here whenever a new shape of config becomes possible. It is much
cheaper than another round of single-site patches.
"""
import pytest
from PySide6.QtCore import Qt

from voice import paths
from voice.audio.capture import Source
from voice.config import DEFAULT_CONFIG, Config
from voice.ui.settings import PROFILE_TEMPLATES, SettingsDialog
from voice.ui.wizard import SetupWizard

#: A config.toml from before the language pairing existed: one local profile,
#: no `general.language_profiles`. What every upgraded install looks like.
UPGRADED = """\
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


def _shipped() -> str:
    return DEFAULT_CONFIG


def _cloud_only() -> str:
    """Everything transcribed by an online service; nothing runs locally."""
    return DEFAULT_CONFIG.replace('active = "local"', 'active = "openai"').replace(
        'en = "local"\nsv = "local-swedish"', "")


def _every_template_defined() -> str:
    """A config that already has every ready-made profile in it."""
    lines = [DEFAULT_CONFIG]
    defined = ("openai", "groq", "openrouter", "local-swedish")
    for name, fields in PROFILE_TEMPLATES.items():
        if name in defined:
            continue
        lines.append(f"\n[stt.profiles.{name}]\n")
        for key, value in fields.items():
            lines.append(f'{key} = {value!r}\n'.replace("'", '"'))
    return "".join(lines)


CONFIGS = {
    "shipped": _shipped,
    "upgraded": lambda: UPGRADED,
    "cloud-only": _cloud_only,
    "every-template-defined": _every_template_defined,
}


@pytest.fixture
def config_of(isolated_xdg):
    def build(name: str) -> Config:
        paths.config_file().write_text(CONFIGS[name]())
        cfg = Config.load()
        assert cfg.errors() == [], f"the {name} config is not even valid: {cfg.errors()}"
        return cfg
    return build


def _dialog(cfg):
    return SettingsDialog(cfg, capture_key=lambda cb: None,
                          sources=lambda: [Source("a", "Mic", True)])


@pytest.mark.parametrize("shape", list(CONFIGS))
def test_the_settings_window_opens_on_the_profile_that_is_in_use(qapp, config_of, shape):
    """Sorting the list alphabetically silently changed which profile a fresh
    window shows: on the shipped config it opened on `groq`, an empty API-key
    form, while `local` was the one transcribing."""
    cfg = config_of(shape)
    dlg = _dialog(cfg)
    selected = dlg.profile_list.currentItem().data(Qt.ItemDataRole.UserRole)
    assert selected == cfg.get("stt.active"), (
        f"{shape}: opened on {selected!r}, but {cfg.get('stt.active')!r} is in use")


@pytest.mark.parametrize("shape", list(CONFIGS))
def test_the_add_template_button_is_pressable_exactly_when_it_would_do_something(
        qapp, config_of, shape):
    """A dead button was replaced by a button that raised KeyError over an
    empty dropdown. Neither is allowed in any shape of config."""
    cfg = config_of(shape)
    dlg = _dialog(cfg)
    offered = dlg.add_profile_combo.count()
    assert dlg.add_profile_button.isEnabled() == bool(offered), (
        f"{shape}: {offered} templates on offer, button "
        f"enabled={dlg.add_profile_button.isEnabled()}")
    if offered:
        dlg.add_profile_button.click()        # must not raise
    assert dlg.error_label.text() == ""


@pytest.mark.parametrize("shape", list(CONFIGS))
def test_the_wizard_never_shows_a_page_that_describes_rows_it_has_not_got(
        qapp, config_of, shape):
    """The model page claimed "the recommended pair is already selected" over a
    blank area, and later "one model for every language" on an install with no
    local model at all."""
    cfg = config_of(shape)
    wiz = SetupWizard(cfg)
    quality = next(i for i, page in enumerate(wiz.pages_shown()) if page.key == "quality")
    page = wiz.page_at(quality)
    if wiz.model_combos:
        assert "nothing on this computer" not in page.body.lower(), shape
    else:
        assert "model" in page.body.lower()
        assert "pair is already selected" not in page.body.lower(), shape
        assert "one model for every language" not in page.body.lower(), shape


@pytest.mark.parametrize("shape", list(CONFIGS))
def test_the_wizard_can_always_be_finished(qapp, config_of, shape):
    """Finish refusing over something it cannot edit traps the owner in a modal
    dialog. Whatever the config looks like, answering it has to be possible."""
    cfg = config_of(shape)
    wiz = SetupWizard(cfg)
    assert wiz.finish() is True, f"{shape}: {wiz.error_label.text()}"
    assert Config.load().needs_setup() is False


def test_a_per_profile_model_folder_does_not_trap_the_wizard(qapp, isolated_xdg):
    """The exact misfire: OWN_KEYS matched `.model` inside
    `stt.profiles.local.model_dir`, a key the wizard never writes."""
    paths.config_file().write_text(DEFAULT_CONFIG)
    cfg = Config.load()
    cfg.set("stt.profiles.local.model_dir", "models")      # relative: a real error
    cfg.save()

    wiz = SetupWizard(Config.load())
    assert wiz.finish() is True, wiz.error_label.text()
    assert Config.load().needs_setup() is False
