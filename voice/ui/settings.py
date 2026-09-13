"""Settings dialog: edits config.toml through Config so comments survive."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from html import escape
from typing import Callable, Iterable

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QGuiApplication, QImage, QKeySequence, QPalette,
                           QRegion)
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
                               QListWidgetItem, QSizePolicy, QSpinBox, QTableWidget,
                               QTableWidgetItem, QTabWidget,
                               QFileDialog, QToolButton, QToolTip, QVBoxLayout, QWidget)

from voice.audio.capture import Source
from voice.config import INJECT_MODES, Config, is_language_code
from voice.hotkey.desktop_shortcuts import (PUNCTUATION_KEYSYMS, PUNCTUATION_NAMES,
                                            ShortcutStoreError, desktop_shortcut_store,
                                            to_accelerator)
from voice.hotkey.keyspec import parse_keyspec
from voice.hotkey.portal_listener import DIALOG_MESSAGE
from voice.ui.pill_placer import PillPlacer
from voice.ui.placement import NO_LAYER_SHELL_NOTE, placement_summary

log = logging.getLogger(__name__)

PROFILE_TEMPLATES: dict[str, dict] = {
    "openai": {"backend": "openai_compatible", "base_url": "https://api.openai.com/v1", "model": "gpt-transcribe", "api_key": "", "prompt": ""},
    "groq": {"backend": "openai_compatible", "base_url": "https://api.groq.com/openai/v1", "model": "whisper-large-v3-turbo", "api_key": "", "prompt": ""},
    "mistral": {"backend": "openai_compatible", "base_url": "https://api.mistral.ai/v1", "model": "voxtral-mini-latest", "api_key": "", "prompt": ""},
    "openrouter": {"backend": "openai_compatible", "base_url": "https://openrouter.ai/api/v1", "model": "openai/whisper-large-v3-turbo", "api_key": "", "prompt": ""},
    "together": {"backend": "openai_compatible", "base_url": "https://api.together.xyz/v1", "model": "openai/whisper-large-v3", "api_key": "", "prompt": ""},
    "local-swedish": {"backend": "local", "model": "KBLab/kb-whisper-large", "device": "cuda", "compute_type": "float16", "beam_size": 5, "prompt": ""},
}
_LOCAL_FIELDS = ["model", "device", "compute_type", "beam_size", "prompt"]

#: Smaller than this is a stored size nothing could be read in - a window
#: dragged shut, or a compositor reporting nonsense while it maps. Ignored
#: rather than obeyed, so a bad number cannot make settings unopenable.
MIN_SETTINGS_SIZE = 320

#: Spare pixels under the pairing table's rows. Styles disagree about how tall
#: a combo in a table cell is, and being a few short clips the last row.
TABLE_SLACK = 6

#: The two the shipped config uses, and what we suggest to anyone who has not
#: measured their own machine: the best of each family. Marked in the list so
#: "which one do I want?" has an answer without reading nine tooltips.
RECOMMENDED_MODELS = ("large-v3", "KBLab/kb-whisper-large")

#: What the Model row offers, largest first within each family. Not a limit -
#: the row is editable and any Hugging Face repository id works - but "medium
#: or large?" should be a choice from a list, not a name to remember. The
#: KB-Whisper entries are the Swedish ones; everything above them is
#: faster-whisper's own short name, which it resolves itself.
MODEL_NOTES = {
    "large-v3": "OpenAI Whisper, the full size. The most accurate of these for "
                "English and the best all-rounder. About 3 GB, and the slowest.",
    "large-v3-turbo": "The same size model with a cut-down decoder: roughly 3x "
                      "faster than large-v3 and a little less accurate, mostly on "
                      "languages other than English. About 1.6 GB.",
    "medium": "Half the size of large. Noticeably less accurate on names, accents "
              "and noisy rooms, but quick on a CPU. About 1.5 GB.",
    "small": "Faster again, and starts to misspell anything unusual. About 500 MB.",
    "base": "For a slow machine. Expect to correct it often. About 150 MB.",
    "tiny": "The smallest there is. Useful for testing that dictation works at "
            "all, not for writing. About 75 MB.",
    "KBLab/kb-whisper-large": "Swedish. Trained by the National Library of Sweden "
                              "on Swedish speech, and clearly better at it than "
                              "large-v3 - which knows Swedish, but as one language "
                              "among a hundred. Useless for English. About 3 GB.",
    "KBLab/kb-whisper-medium": "The same Swedish training at half the size: quicker "
                               "on a CPU, less sure of unusual words. About 1.5 GB.",
    "KBLab/kb-whisper-small": "The lightest Swedish one. About 500 MB.",
}

#: What the Model row offers, largest first within each family. Not a limit -
#: the row is editable and any Hugging Face repository id works - but "medium
#: or large?" should be a choice from a list, not a name to remember.
MODEL_CHOICES = tuple(MODEL_NOTES)


class ModelChooser(QComboBox):
    """The Model row: pick a size, or type any repository id.

    It answers to `text()`/`setText()` as well, so the profile form can hold it
    beside the plain line edits and read them all the same way.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for index, name in enumerate(MODEL_CHOICES):
            self.addItem(name)
            note = MODEL_NOTES[name]
            if name in RECOMMENDED_MODELS:
                # Bold in the list and said in words on hover: the row's text
                # has to stay the model name, because that is what is saved.
                note = f"Recommended. {note}"
                font = self.font()
                font.setBold(True)
                self.setItemData(index, font, Qt.ItemDataRole.FontRole)
            # Hovering a row is the only place there is room to say what the
            # difference between two model names actually is.
            self.setItemData(index, note, Qt.ItemDataRole.ToolTipRole)

    def text(self) -> str:
        return self.currentText().strip()

    def setText(self, value: str) -> None:
        self.setCurrentText(str(value))
#: The profile form reads as a form, not as a config file: the keys stay the
#: keys (they are what is written), only what the user reads changes.
_FIELD_LABELS = {"backend": "Where it runs", "base_url": "Service address",
                 "model": "Model", "api_key": "API key",
                 "api_key_env": "API key variable", "prompt": "Vocabulary hint",
                 "device": "Processor", "compute_type": "Number format",
                 "beam_size": "Search width"}
#: How a profile's kind reads on the tab. The value written to the file is
#: unchanged; what a person is shown is where the work happens.
_BACKEND_LABELS = {"local": "On this computer", "openai_compatible": "On an online service"}
_CLOUD_FIELDS = ["base_url", "model", "api_key", "api_key_env", "prompt"]
#: The Paste with row, in the order it is offered. Keyed by what goes in the
#: file; the labels name the keys, because "terminal_chord" is a setting name
#: and Ctrl+Shift+V is the thing the owner recognises.
PASTE_WITH_LABELS = {
    "auto": "Work it out from the window",
    "normal": "Always Ctrl+V",
    "terminal": "Always Ctrl+Shift+V",
}

_LANGUAGES = [("English", "en"), ("Swedish", "sv"), ("Auto-detect", "auto")]


def language_name(code: str) -> str:
    """"en" is a thing a config file says; "English" is the thing it means."""
    for label, known in _LANGUAGES:
        if known == str(code).strip().lower():
            return label
    return str(code)
#: The same names, for the per-language table: "en" is a code, not a language.
_LANGUAGE_NAMES = {code: label for label, code in _LANGUAGES}
#: inject.mode, in the order the combo shows it; the data is the config value.
_INJECT_MODE_LABELS = {"paste": "Paste automatically",
                       "clipboard": "Copy only (press Ctrl+V yourself)"}
#: What the dictation key does, in the order the combo shows it; the data is
#: what is stored. "hold" and "toggle" say nothing to anyone who has not read
#: the config file, and this row is the one that decides how the key feels.
_DICTATE_MODES = (("Hold it down while you talk", "hold"),
                  ("Press once to start, again to stop", "toggle"))
#: Notifications, same idea: a value the file keeps as true or false.
_NOTIFICATION_LABELS = (("On", True), ("Off", False))
#: Under the preview: what dragging it there can and cannot do.
PILL_PLACEMENT_NOTE = (
    "Drag the pill to where it should appear; arrow keys nudge it a pixel at a time, "
    "Shift+arrow ten. Dropping it near one of the nine anchor points takes that point "
    "exactly. What is saved is the corner or edge it sits against and the gap from it, "
    "not a free position - that is all a desktop can be asked for.\n\n"
    "Some desktops place small windows themselves and ignore what they are asked for. "
    "Where that happens the line under the preview says so, and the pill still appears - "
    "just wherever your desktop decides.")
#: Above the preview: what this screen in miniature is for. Without it the
#: tab shows a black rectangle and leaves the reader to work out that it is a
#: screen, that the dot in it is the pill, and that both can be dragged.
PILL_PLACER_NOTE = "Drag the pill to where it should appear on your screen."
#: The "leave stt.active alone for this language" row of the profile table.
KEEP_CURRENT = "(keep current)"
#: The four things a key can do, in the order the Hotkeys tab lists them, named
#: for what they do rather than for the setting they are kept in. The owner: "I
#: need to know how to use it without deep tech understanding".
ACTIONS = (("dictate", "Start and stop dictation"),
           ("cancel", "Cancel the current recording"),
           ("language_toggle", "Switch language"),
           ("recall", "Insert the last dictation again"))
ACTION_LABELS = dict(ACTIONS)
#: What "Change…" has to do on this machine. Only one of the three is ever in
#: force, and the window shows only that one - which of them it is is decided
#: once, from the running backend and whether this desktop's shortcut store is
#: ours to write.
CAPTURE = "capture"                  # voice reads the keyboard: take the next key
DESKTOP_STORE = "desktop-store"      # the desktop's store is ours to write (GNOME)
DESKTOP_DIALOG = "desktop-dialog"    # the desktop insists on its own window (KDE)
#: The one sentence at the top of the tab: who owns these keys here. No
#: mechanism, no setting name - just who the user has to argue with.
WHO_MANAGES = {
    CAPTURE: "voice reads your keyboard directly, so you can set any key here.",
    DESKTOP_STORE: "Your desktop manages these shortcuts, and voice can change them for you.",
    DESKTOP_DIALOG: "Your desktop manages these shortcuts, and only its own window can "
                    "change them.",
}
#: One button per key, the same on every route. What it says while it waits.
CHANGE = "Change…"
CHANGE_BUSY = {CAPTURE: "Press a key…", DESKTOP_STORE: "Press the keys…",
               DESKTOP_DIALOG: "Opening…"}
#: And the line under the list, which says the same thing in full.
CHANGE_PROMPT = {
    CAPTURE: "Press the key you want to use.",
    DESKTOP_STORE: "Press the keys you want to use, or Escape to leave it as it is.",
    DESKTOP_DIALOG: "Opening your desktop's shortcut settings…",
}
#: How a change that came to nothing reads, and a key nothing can be made of.
CHANGE_STOPPED = "Left as it was."
#: How long capture waits before giving up. Long enough to find the key, short
#: enough that a window left open is not still listening an hour later.
CAPTURE_TIMEOUT_MS = 15000
CAPTURE_GAVE_UP = "No key was pressed, so nothing changed."
CLEAR_KEY = "Clear"
#: How a change that reached the desktop reads, and a save of the whole set.
#: The store answers with a sentence of its own - the key it wrote, in the
#: desktop's own spelling, and the ids it skipped - which is a sentence for
#: whoever wrote the store. It goes to the log; this goes on screen.
CHANGED_ON_DESKTOP = "Your desktop now uses {key} for: {what}."
#: And the one way that can half-succeed: the desktop took the key, and this
#: window could not write it down. Both halves have to agree, so say so.
TRIGGER_NOT_KEPT = ("Your desktop now uses {key}, but it could not be saved here "
                    "({error}) - press Save.")
SAVED_TO_DESKTOP = "Your shortcuts have been handed to your desktop."
#: And the one case where nothing can be handed over: a shortcut this desktop
#: has never been told about, because voice has never asked it for one. Saving
#: asks, which is why that is what this says to do.
NOT_OFFERED = "Your desktop has not been offered this shortcut yet - press Save, then Change… again."
#: Both refusals a key press can meet, and both leave the window still
#: listening: the user has just pressed something and has to be told what to
#: press instead, not handed back a window that says nothing is happening.
CHANGE_UNUSABLE = ("That key cannot be used as a shortcut - try another one, "
                   "or press Escape to leave it as it is.")
NEEDS_A_COMBINATION = ("A key on its own would stop working everywhere else - hold Ctrl, "
                       "Alt or Super as well, or press Escape to leave it as it is.")
#: And when the store refuses without a word of its own to show.
CHANGE_REFUSED = "Your desktop did not take that key."
#: What a listener that cannot hand back a key answers with instead: a sentence
#: about the mechanism it is. This is that sentence, for the person reading it.
CAPTURE_NOT_POSSIBLE = "This desktop handles the shortcuts itself - set the key in its own keyboard settings."
#: What the desktop's own window being open reads as, in place of the portal's
#: own sentence, which is written for whoever wrote the portal.
DESKTOP_DIALOG_OPEN = "Your desktop has opened its own window - set the key there."
#: The desktop's own shortcut editor, tried in PATH order: GNOME has no portal
#: reconfigure dialog, so its Keyboard panel is the next best thing.
SHORTCUT_SETTINGS_COMMANDS = (("gnome-control-center", "keyboard"),
                              ("systemsettings", "kcm_keys"))
#: Where to click when neither is installed, and the button's own subject.
SHORTCUT_SETTINGS_PATH = "Settings → Keyboard → Keyboard Shortcuts"
#: What the desktop's settings app being open - or refusing to open - reads as.
SETTINGS_APP_OPENED = f"Your desktop's settings are open: look under {SHORTCUT_SETTINGS_PATH}."
SETTINGS_APP_MISSING = ("This desktop has no settings app we can start - open "
                        f"{SHORTCUT_SETTINGS_PATH} yourself.")
SETTINGS_APP_FAILED = ("Your desktop's settings would not start ({error}) - open "
                       f"{SHORTCUT_SETTINGS_PATH} yourself.")
#: How a key that is not set reads, and how one nobody has answered for yet
#: does. Both are what the row shows instead of a key, so both are sentences a
#: person can act on rather than a state name.
NOT_SET = "Not set"
UNKNOWN_TRIGGER = "Checking with your desktop…"
#: The button inside "Advanced" that goes to the desktop's own keyboard panel.
SHORTCUT_SETTINGS_BUTTON = "Open your desktop's keyboard settings"
#: The collapsed section at the bottom, and the one line above each half of it.
ADVANCED = "Advanced"
ADVANCED_DESKTOP_NOTE = "What voice asks your desktop for, the way your desktop writes it."
ADVANCED_DEVICE_NOTE = {
    CAPTURE: "The exact key names voice reads. Join two with a plus for a combination.",
    DESKTOP_STORE: "Keys for the case where voice reads the keyboard itself instead.",
    DESKTOP_DIALOG: "Keys for the case where voice reads the keyboard itself instead.",
}
#: Behind the "?" on the Hotkeys tab: the detail a first-time reader needs once,
#: per route, in words that name no mechanism and no file.
HOTKEY_HELP = {
    CAPTURE: (
        "voice watches the keyboard itself, so any key you press can be a shortcut: press "
        "\"Change…\", then press the key.\n\n"
        "For two keys at once - say Super and Space - open \"Advanced\" and type both names "
        "with a plus between them.\n\n"
        "\"Dictation key\" decides whether you hold that key down while you talk or press it "
        "once to start and once to stop."),
    DESKTOP_STORE: (
        "Your desktop keeps the list of shortcuts, not voice. \"Change…\" asks you for the "
        "new keys and hands them straight to your desktop, so the new key works at once.\n\n"
        "Each row shows what your desktop holds right now, which is why a key you set in your "
        "desktop's own keyboard settings shows up here too.\n\n"
        "If a new shortcut does nothing, something else on this computer is probably already "
        "using it - try another combination."),
    DESKTOP_DIALOG: (
        "Your desktop keeps the list of shortcuts and only lets you change them in its own "
        "window, so \"Change…\" opens that window: find voice in the list there and set the "
        "key.\n\n"
        "Each row shows what your desktop holds right now, so a key set over there shows up "
        "here as soon as your desktop says so.\n\n"
        "If a new shortcut does nothing, something else on this computer is probably already "
        "using it - try another combination."),
}
#: How long after the last drag or nudge the pill is shown on the desktop. Long
#: enough that a run of arrow keys is one preview, short enough to feel immediate.
PREVIEW_DELAY_MS = 600
#: What the window says while the real pill is on screen at the new placement.
PREVIEW_SHOWING = "Showing the pill there on your desktop…"
#: The same, where the compositor ignores placement: the pill does appear, just
#: not where it was dropped. Saying "there" beside the note explaining that the
#: desktop chooses is a contradiction the owner has to resolve on their own.
PREVIEW_SHOWING_ANYWHERE = "Showing the pill now - your desktop chooses where."
#: The daemon's refusals, in words that say what to do about it. The owner drags,
#: drops, sees nothing and concludes the feature is broken; the daemon knew why
#: all along and told the window, which threw it away.
PREVIEW_REFUSALS = {
    "not while a dictation is running": "Not while you are dictating - try again in a moment.",
}
#: Anything else it refuses with is shown as it comes rather than swallowed: the
#: other refusal names a setting ("ui.overlay = false") and is worth reading.
PREVIEW_REFUSED = "Not showing it: {error}"
#: And a refusal with nothing to say for itself.
PREVIEW_CANNOT = "Cannot show it here."
#: How long a refusal stays before it clears itself. A note left on screen is
#: read as the state of things now, so none of them may outlive what it describes:
#: the "showing" line goes when the pill does, the reason after long enough to
#: read it twice.
REFUSAL_NOTE_MS = 6000
#: How long to assume a preview lasts when the daemon's reply does not say, or
#: says something that is not a number. The daemon has its own PREVIEW_SECONDS
#: and normally tells us; this is only what to do when it has not.
ASSUMED_PREVIEW_SECONDS = 5.0
#: Behind the other "?" buttons, one paragraph each.
def _model_help() -> str:
    """The whole list, in the "?" - not one tooltip at a time.

    Written from MODEL_NOTES rather than beside it: two lists of nine models
    would disagree within a release.
    """
    lines = ["Which speech model does the listening. Bigger is more accurate and "
             "slower; the KBLab ones are trained on Swedish speech rather than on a "
             "hundred languages at once.\n"]
    for name in MODEL_CHOICES:
        mark = " (Recommended)" if name in RECOMMENDED_MODELS else ""
        lines.append(f"{name}{mark}\n    {MODEL_NOTES[name]}\n")
    lines.append("You can also type the name of any speech model on Hugging Face.")
    return "\n".join(lines)


_MODEL_HELP = _model_help()

HELP = {
    "pill_position": PILL_PLACEMENT_NOTE,
    "profile_per_language": (
        "Two ways to decide which model transcribes. This table picks one of them.\n\n"
        "PAIRED — give a language a profile, and switching to that language switches "
        "to that profile. Swedish speech gets a Swedish model without a second "
        "change. This is how a new install ships.\n\n"
        "\"(keep current)\" — that language does not touch the profile. Set every "
        "language to \"(keep current)\" and nothing is paired at all: the language "
        "switch changes only the language, and you choose the model yourself with "
        "\"Use this profile\" on the Transcription tab. That is the other way, and it "
        "is the one to use if you keep a cloud profile for everything.\n\n"
        "With a pairing in force, \"Use this profile\" still works - but only until "
        "the next language switch puts the paired profile back.\n\n"
        "A profile has to exist before a language can point at it; add it on the "
        "Transcription tab first."),
    "paste_with": (
        "How voice presses paste for you.\n\n"
        "\"Work it out\" looks at the window you were dictating into: terminals "
        "ignore Ctrl+V and want Ctrl+Shift+V, so it sends whichever fits.\n\n"
        "That needs the desktop to say which window has the keyboard, and over a "
        "remote desktop - or in a virtual machine window - it cannot. A terminal "
        "then gets Ctrl+V, which it ignores, and the text silently stays on the "
        "clipboard.\n\n"
        "So if you mostly dictate into a terminal, choose \"Always Ctrl+Shift+V\" "
        "and nothing is guessed. Ordinary programs accept it too, in most cases - "
        "but not all, which is why it is not the default."),
    "text_insertion": (
        "\"Paste automatically\" copies the text and presses Ctrl+V for you.\n\n"
        "\"Copy only\" leaves the text on the clipboard and tells you to press Ctrl+V "
        "yourself. Use that if the text never arrives on its own - some desktops refuse to "
        "let one program press keys in another."),
    "max_seconds": ("A recording stops itself after this many seconds, so a key left held "
                    "down by accident cannot record all afternoon."),
    "model": _MODEL_HELP,
    "cloud_model": (
        "Which model the service should use, spelled the way that service spells "
        "it - \"gpt-transcribe\", \"whisper-large-v3-turbo\". \"Add from template\" "
        "fills in a working one for each service voice knows.\n\n"
        "Their model lists live in their own documentation, and change without "
        "warning: a name that stops working is the first thing to check when a "
        "cloud profile starts failing."),
    "device": (
        "Where the model runs.\n\n"
        "\"cuda\" uses an NVIDIA graphics card, which is many times faster. \"cpu\" "
        "uses the processor. Leave it on \"cuda\": if there is no usable card, voice "
        "falls back to the processor on its own and tells you it did."),
    "compute_type": (
        "How precisely the numbers inside the model are stored. Fewer bits means less "
        "memory and more speed, for a little accuracy. The usual ones:\n\n"
        "float16 — half precision on a graphics card. The normal choice with "
        "Processor \"cuda\": full accuracy for practical purposes, half the memory of "
        "float32.\n\n"
        "int8 — whole numbers, a quarter of the size. The normal choice with Processor "
        "\"cpu\", and several times faster there. A little more likely to mis-hear an "
        "unusual word.\n\n"
        "int8_float16 — int8 weights with float16 arithmetic, on a graphics card. "
        "Saves memory on a small card; rarely worth it on a big one.\n\n"
        "float32 — full precision. Twice the memory and slower, for no accuracy you "
        "will hear. Only useful on hardware with no float16 support.\n\n"
        "Leave it as it is: voice picks the right one when it falls back from the "
        "graphics card to the processor."),
    "beam_size": (
        "How many possible sentences the model keeps in play before choosing one.\n\n"
        "5 is the default and a good balance. Higher (8-10) is slightly more accurate "
        "on difficult audio and slower. 1 makes it take the first thing it thinks of: "
        "fastest, and noticeably worse."),
    "prompt": (
        "A sentence describing the kind of speech, to steer wording and punctuation - "
        "\"Notes on a meeting.\", say.\n\n"
        "Leave it empty unless you have a reason. Names and jargon do NOT belong here: "
        "put those in the Dictionary tab, where they are given to the model as a word "
        "list. Prose here drags ordinary sentences towards its own wording."),
    "base_url": (
        "The address of the online service that does the transcribing, up to and "
        "including the version - https://api.openai.com/v1. \"Add from template\" "
        "fills this in for the services voice knows."),
    "api_key": (
        "Your key for that service, pasted in. It is stored in config.toml, which is "
        "readable only by you.\n\n"
        "If you would rather not keep it in a file at all, leave this empty and use "
        "the API key variable below instead."),
    "api_key_env": (
        "The name of an environment variable holding the key - OPENAI_API_KEY - so "
        "the key itself stays out of config.toml. Used only when the API key box "
        "above is empty."),
    "activate": (
        "Makes the selected profile the one that does the transcribing, from the next "
        "dictation on. The row in the list above is marked \"in use\".\n\n"
        "It does not change your language. If a language is paired with a profile "
        "(General tab), switching to that language switches the profile too, and will "
        "override this.\n\n"
        "Nothing is written until you press Save."),
    "add_template": (
        "Adds a ready-made profile so you do not have to type its settings in.\n\n"
        "The online services need an API key pasting in afterwards. \"local-swedish\" "
        "is the Swedish model, and is already there in a new install."),
    "model_dir": (
        "Leave this empty and the models go to ~/.cache/huggingface, shared with any "
        "other program on this computer that uses Hugging Face models.\n\n"
        "Give a folder and voice keeps its own there instead - useful if that is a "
        "few gigabytes you would rather have on another disk. Models already "
        "downloaded are not moved: the new folder starts empty and fills the next "
        "time a model is needed."),
    "profiles": (
        "A profile is one way of turning speech into text, with its settings: a model that "
        "runs on this computer, or an online service you have an account with.\n\n"
        "\"Use this profile\" makes the selected one the one that transcribes; \"Add from "
        "template\" fills in a known service for you, and you paste in your API key."),
    "hotwords": (
        "Names and jargon the model has never seen come out as the nearest ordinary "
        "English - \"CachyOS\" becomes \"khaki OS\". Listing them here tells it to expect "
        "those words, so it writes them correctly the first time.\n\n"
        "Whole words and short phrases, separated by commas. The replacements below are "
        "the other half: they fix a spelling after the fact, and what you put in their "
        "\"Replace with\" column is listened for here automatically."),
    "dictionary": (
        "Every replacement is applied to the text before it is inserted, in order: names and "
        "words the model hears wrong, fixed once here.\n\n"
        "The options column can be left empty. Type \"icase\" to match a word however it is "
        "capitalised, or \"regex\" if you know what a regular expression is and want one."),
}
#: The longest run of text allowed to sit on a tab. Anything above this belongs
#: behind a "?": the window is a form, not a manual.
MAX_INLINE_TEXT = 160
#: The circled "?" itself, and how wide its answer is allowed to be.
HELP_SIZE = 18
HELP_WIDTH = 320
#: The circled "?", with its one colour left to be filled in per theme: a
#: colour written in here is a colour that cannot follow the desktop.
HELP_STYLE = """
QToolButton {{
    border: 1px solid {colour}; border-radius: {radius}px;
    color: {colour}; font-weight: bold; padding: 0px;
}}
QToolButton:hover {{ border-color: palette(highlight); color: palette(highlight); }}
"""
#: Layout only. What the dim captions are coloured with is taken from the
#: palette per widget - see `secondary_text_colour`.
DIALOG_STYLE = """
QGroupBox { font-weight: bold; margin-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 2px; padding: 0 3px; }
"""
#: The least contrast a colour of ours may have against what is painted behind
#: it. These are sentences the owner has to read, not decoration, so the bar is
#: the WCAG AA floor for normal text rather than the 3:1 allowed for incidental
#: text - which left no margin at all: at 3:1 the worst caption measured 3.14:1
#: on the real pixels, and dim text that cannot be read is not dim, it is
#: missing.
MIN_CONTRAST = 4.5
#: How far the theme's own text colour is faded when the palette offers nothing
#: dim that can be read: enough to read as secondary, not enough to disappear.
DIM_ALPHA = 0.65
#: What the chooser actually aims for. No pixel of a thin glyph is ever the full
#: colour - a "?" is mostly antialiasing, and its circle is a one-pixel line -
#: so a colour computed to land exactly on the bar measures a few hundredths
#: under it once it is drawn. Aim a little over, and the rendered pixels clear
#: the bar rather than sitting just below it.
TARGET_CONTRAST = MIN_CONTRAST * 1.05
#: How far from `Window` a palette colour may be and still be believable as a
#: panel this window's text sits on. A tab pane is a shade of the window; a role
#: a hand-built palette never filled in is not.
SHADE_OF_WINDOW = 2.0
#: An error is a signal, not a theme colour, so the hue stays put on every
#: desktop - but `error_text_colour` lightens or darkens it until it can be
#: read, because a red the window swallows is not a signal at all.
ERROR_HUE = "#e5484d"
#: The one spacing the whole window uses, so no two tabs breathe differently.
ROW_SPACING = 8
COLUMN_SPACING = 12
MARGIN = 12


def shortcut_settings_command(which: Callable[[str], str | None] | None = None) -> list[str] | None:
    """The desktop's own shortcut editor, or None if neither is installed."""
    look_up = which or shutil.which          # resolved per call, not at import
    for command in SHORTCUT_SETTINGS_COMMANDS:
        if look_up(command[0]):
            return list(command)
    return None


def desktop_key_text(triggers: dict[str, str] | None, name: str) -> str:
    """The key the desktop holds for `name`, the way a person writes it.

    No answer at all is "we have not been told yet", which is not the same as
    "no key": the desktop has not replied before the first bind, and a row that
    said "Not set" then would be a row that lies for a second or two.
    """
    if not triggers:
        return UNKNOWN_TRIGGER
    return pretty_trigger(triggers.get(name, "")) or NOT_SET


#: Our trigger syntax, and the keyboard's own key names, as a person writes
#: them. Nothing here changes what is stored - this is the reading of it.
_MODIFIER_WORDS = {"CTRL": "Ctrl", "CONTROL": "Ctrl", "SHIFT": "Shift", "ALT": "Alt",
                   "ALTGR": "Alt Gr", "SUPER": "Super", "META": "Super", "LOGO": "Super"}
_DEVICE_MODIFIERS = {"LEFTCTRL": "Ctrl", "RIGHTCTRL": "Right Ctrl", "LEFTSHIFT": "Shift",
                     "RIGHTSHIFT": "Right Shift", "LEFTALT": "Alt", "RIGHTALT": "Alt Gr",
                     "LEFTMETA": "Super", "RIGHTMETA": "Right Super"}
_KEY_WORDS = {"SPACE": "Space", "ESC": "Escape", "ESCAPE": "Escape", "RETURN": "Enter",
              "ENTER": "Enter", "KPENTER": "Enter", "TAB": "Tab", "BACKSPACE": "Backspace",
              "DELETE": "Delete", "DEL": "Delete", "INSERT": "Insert", "HOME": "Home",
              "END": "End", "PAGEUP": "Page Up", "PAGE_UP": "Page Up",
              "PAGEDOWN": "Page Down", "PAGE_DOWN": "Page Down", "UP": "Up", "DOWN": "Down",
              "LEFT": "Left", "RIGHT": "Right", "CAPSLOCK": "Caps Lock", "MENU": "Menu",
              "PRINT": "Print Screen", "SYSRQ": "Print Screen", "PAUSE": "Pause",
              "INS": "Insert", "PGUP": "Page Up", "PGDOWN": "Page Down",
              "LEFTBRACE": "[", "RIGHTBRACE": "]", "MINUS": "-", "EQUAL": "=",
              "SEMICOLON": ";", "APOSTROPHE": "'", "GRAVE": "`", "COMMA": ",", "DOT": ".",
              "SLASH": "/", "BACKSLASH": "\\"}
#: And the X keysym names a portal trigger now carries for those same keys, so
#: "CTRL+comma" reads back as "Ctrl+," rather than as "Ctrl+Comma". One table,
#: read the other way round, so the two cannot drift apart.
_KEY_WORDS.update({name.upper(): char for char, name in PUNCTUATION_KEYSYMS.items()})


def _pretty_part(part: str) -> str:
    """One key or modifier of a combination, as a person writes it."""
    upper = part.upper()
    if upper in _MODIFIER_WORDS:
        return _MODIFIER_WORDS[upper]
    if upper in _KEY_WORDS:
        return _KEY_WORDS[upper]
    if len(part) == 1:
        return part.upper()
    if upper.startswith("F") and upper[1:].isdigit():
        return upper
    return part[:1].upper() + part[1:].lower()


#: What the portal hands back is a sentence in the desktop's own spelling -
#: "Press <Control>space" - not our "CTRL+space". Both have to read the same
#: on the row, so the desktop's form is translated into ours before it is
#: prettified, rather than being title-cased into "Press <control>space".
_DESKTOP_MODIFIER = re.compile(r"<([A-Za-z]+)>")


def _from_desktop_spelling(trigger: str) -> str:
    """The desktop's own way of writing a shortcut, in ours."""
    text = (trigger or "").strip()
    if text.lower().startswith("press "):
        text = text[len("press "):].strip()
    if "<" not in text:
        return text
    parts = [m.group(1) for m in _DESKTOP_MODIFIER.finditer(text)]
    key = _DESKTOP_MODIFIER.sub("", text).strip()
    if key:
        parts.append(key)
    return "+".join(parts)


def pretty_trigger(trigger: str) -> str:
    """A desktop shortcut ("CTRL+space") as a person writes it ("Ctrl+Space")."""
    parts = [part.strip() for part in _from_desktop_spelling(trigger).split("+") if part.strip()]
    return "+".join(_pretty_part(part) for part in parts)


def pretty_device_key(text: str) -> str:
    """A keyboard key name ("KEY_LEFTMETA+KEY_SPACE") the same way ("Super+Space")."""
    out = []
    for part in (text or "").split("+"):
        name = part.strip().upper()
        if name.startswith(("KEY_", "BTN_")):
            name = name[4:]
        if not name:
            continue
        out.append(_DEVICE_MODIFIERS.get(name) or _pretty_part(name))
    return "+".join(out)


#: A key press, read as a shortcut. The modifiers in the order our own trigger
#: syntax writes them, and the keys that are only ever half of a combination.
_CHORD_MODIFIERS = ((Qt.KeyboardModifier.ControlModifier, "CTRL"),
                    (Qt.KeyboardModifier.ShiftModifier, "SHIFT"),
                    (Qt.KeyboardModifier.AltModifier, "ALT"),
                    (Qt.KeyboardModifier.MetaModifier, "SUPER"))
_HALF_A_CHORD = {Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_AltGr,
                 Qt.Key.Key_Meta, Qt.Key.Key_Super_L, Qt.Key.Key_Super_R, Qt.Key.Key_CapsLock,
                 Qt.Key.Key_NumLock, Qt.Key.Key_ScrollLock, Qt.Key.Key_unknown}


def chord_trigger(event) -> str | None:
    """The shortcut a key press asks for, in the syntax the desktop is given.

    None while the press is only half of one - a modifier held down on its own,
    which is what every combination starts with - and an empty string for a key
    no shortcut can be made of, so the caller can say so rather than write
    something the desktop would refuse.
    """
    key = event.key()
    if key in _HALF_A_CHORD or not key:
        return None
    name = QKeySequence(key).toString()
    # Qt writes a punctuation key as the character itself, which GTK cannot
    # parse - and "+" cannot even survive our own separator. The keysym name
    # can do both, so it is what the trigger carries from here on.
    name = PUNCTUATION_KEYSYMS.get(name, name)
    if not name or " " in name or "+" in name or len(name) > 20:
        return ""
    modifiers = event.modifiers()
    parts = [word for flag, word in _CHORD_MODIFIERS if modifiers & flag]
    parts.append(name)
    return "+".join(parts)


#: Keys that cannot be a shortcut on their own. A global shortcut takes its key
#: away from every other window on the machine, so a bare letter, digit or
#: typing key is not a shortcut - it is that key, gone. Anything else (a
#: function key, Insert, a media key) is nobody's typing and is fine alone.
_NEVER_ALONE = {"space", "return", "enter", "tab", "backspace"} | set(PUNCTUATION_NAMES)


def chord_problem(trigger: str) -> str | None:
    """Why this combination cannot be given to the desktop, or None if it can.

    The window grabs the whole application while it waits for a combination, so
    every key press in it arrives here - including the ones the user only meant
    to type. Answering with the reason rather than writing them is what keeps
    "Change…" from quietly rebinding dictation to the letter A.
    """
    if not trigger:
        return CHANGE_UNUSABLE
    parts = trigger.split("+")
    key = parts[-1]
    if len(parts) == 1 and (key.lower() in _NEVER_ALONE
                            or (len(key) == 1 and key.isascii() and key.isalnum())):
        return NEEDS_A_COMBINATION
    try:
        to_accelerator(trigger)          # the desktop's own spelling of it, or a refusal
    except ValueError:
        return CHANGE_UNUSABLE
    return None


def _luminance(colour: QColor) -> float:
    """The WCAG relative luminance of an opaque colour, 0.0 to 1.0."""
    def channel(value: int) -> float:
        part = value / 255.0
        return part / 12.92 if part <= 0.03928 else ((part + 0.055) / 1.055) ** 2.4
    return (0.2126 * channel(colour.red()) + 0.7152 * channel(colour.green())
            + 0.0722 * channel(colour.blue()))


def contrast_ratio(one: QColor, other: QColor) -> float:
    """How far apart two opaque colours are: 1.0 identical, 21.0 black on white."""
    high, low = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _over(colour: QColor, behind: QColor) -> QColor:
    """`colour` composited onto `behind`: a palette colour may be translucent.

    `PlaceholderText` usually is - it is the theme's own text at half alpha -
    and half of an invisible colour is still invisible, so it has to be flattened
    before anything is measured.
    """
    alpha = colour.alphaF()
    return QColor.fromRgbF(*(colour.redF() * alpha + behind.redF() * (1 - alpha),
                             colour.greenF() * alpha + behind.greenF() * (1 - alpha),
                             colour.blueF() * alpha + behind.blueF() * (1 - alpha)))


def _legible(colour: QColor, behind: QColor) -> QColor:
    """`colour`, moved towards white or black until it can be read on `behind`."""
    towards = QColor("white") if _luminance(behind) < 0.5 else QColor("black")
    mixed = colour
    for step in range(21):
        mixed = _over(QColor(towards.red(), towards.green(), towards.blue(),
                             round(255 * step / 20)), colour)
        if contrast_ratio(mixed, behind) >= TARGET_CONTRAST:
            break
    return mixed


def _mix(one: QColor, other: QColor, part: float) -> QColor:
    """`part` of the way from `one` to `other`."""
    return QColor.fromRgbF(one.redF() + (other.redF() - one.redF()) * part,
                           one.greenF() + (other.greenF() - one.greenF()) * part,
                           one.blueF() + (other.blueF() - one.blueF()) * part)


def conservative_background(palette: QPalette) -> QColor:
    """The hardest surface in the palette to read dim text on.

    The style does not paint `Window` behind a caption. Fusion fills a tab pane
    with a colour it derives from `Button` and lightens - `#424242` where the
    window is `#2b2b2b` - and that shade is nowhere in the palette. Where the
    real colour cannot be sampled (`painted_background`), the palette's own
    extreme in the direction that hurts is used instead: the lightest surface on
    a dark theme, where the text will be light, and the darkest on a light one.
    That errs towards too much contrast rather than towards too little.

    Only surfaces that are a shade of the window count. A palette built by hand
    leaves roles it was never given at their light defaults, and an
    `AlternateBase` of near-white behind a `#2b2b2b` window is not a panel this
    text will ever sit on - believing it would choose dark text for a dark
    window, which is the bug this whole ladder exists to avoid.
    """
    window = palette.color(QPalette.ColorRole.Window)
    surfaces = [colour for colour in
                (palette.color(role) for role in (QPalette.ColorRole.Base,
                                                  QPalette.ColorRole.AlternateBase,
                                                  QPalette.ColorRole.Button))
                if contrast_ratio(colour, window) <= SHADE_OF_WINDOW]
    surfaces.append(window)
    dark_theme = _luminance(window) < 0.5
    return max(surfaces, key=_luminance) if dark_theme else min(surfaces, key=_luminance)


def theme_palette(widget: QWidget) -> QPalette:
    """The palette a widget should take its theme from: its window's.

    Not its parent's. A stylesheet anywhere up the tree stops Qt propagating a
    palette set on that widget down to its children - even a stylesheet with no
    colour in it, which is what this dialog has - so a parent halfway down can
    still be holding the theme before last. The window is always current: it is
    what the palette was set on. An application-wide change, which is what a
    desktop switching theme actually does, reaches everything either way.
    """
    window = widget.window()
    if window is None or window is widget:
        return QApplication.palette()
    return window.palette()


def painted_background(widget: QWidget, theme: QPalette) -> QColor | None:
    """What is really painted behind `widget`, or None while nothing can say.

    Walks out through the ancestors, rendering each one alone - no children, and
    no forced background - onto a transparent pixel at the widget's own centre.
    The first one that covers that pixel is what the text sits on: a group box
    paints only its frame, a tab page paints nothing at all, and it is the tab
    widget's pane that is actually behind every caption in this window.

    A whole column of the widget's height is sampled, not one pixel: a tab pane
    is a gradient, so the top of a caption sits on a slightly different shade
    from its bottom, and the one that is taken is the shade that makes the text
    hardest to read - the lightest on a dark theme, the darkest on a light one.

    None until the widget has been laid out, which is why everything here is
    re-taken on `showEvent` as well as on a palette change.
    """
    height = widget.height()
    if not widget.isVisible() or widget.width() <= 0 or height <= 0:
        # Nothing is painted behind a widget that is not on screen, and its
        # ancestors have neither been laid out nor, on a window that was given a
        # palette it could not pass on, repainted in the theme now running. What
        # they would draw is fiction; the palette's own worst case is not.
        return None
    top = QPoint(widget.width() // 2, 0)
    column = QImage(1, height, QImage.Format.Format_ARGB32)
    dark_theme = _luminance(theme.color(QPalette.ColorRole.Window)) < 0.5
    ancestor = widget.parentWidget()
    while ancestor is not None:
        spot = widget.mapTo(ancestor, top)
        if ancestor.rect().contains(spot):
            column.fill(Qt.GlobalColor.transparent)
            try:
                ancestor.render(column, QPoint(0, 0), QRegion(QRect(spot, QSize(1, height))),
                                QWidget.RenderFlag(0))
            except Exception as exc:        # a colour is never worth a broken window
                log.debug("could not sample what is behind %s: %s", widget.objectName(), exc)
                return None
            painted = [column.pixelColor(0, y) for y in range(height)
                       if column.pixelColor(0, y).alpha() == 255]
            if painted:
                return (max(painted, key=_luminance) if dark_theme
                        else min(painted, key=_luminance))
        ancestor = ancestor.parentWidget()
    return None


def secondary_text_colour(palette: QPalette, behind: QColor | None = None) -> QColor:
    """Dim text the running theme can actually show, taken from its own palette.

    `PlaceholderText` is the role meant for exactly this and most themes fill it
    in; a palette built by hand leaves it black, which a dark window swallows.
    The disabled `WindowText` is the usual second best, and on a light theme it
    is a grey too pale to read. The theme's own text colour, faded, is the third.
    None is trusted on its own: whichever of them can be read on `behind` wins.

    And if none of them can, the ladder does not stop at a best effort - the
    dimmest candidate is moved towards the theme's own text until it clears the
    bar, and past even that towards black or white. There is always an answer,
    because dim text that cannot be read is not dim, it is missing.
    """
    behind = behind if behind is not None and behind.isValid() else conservative_background(palette)
    foreground = palette.color(QPalette.ColorRole.WindowText)
    faded = QColor(foreground)
    faded.setAlphaF(DIM_ALPHA)
    ladder = [_over(palette.color(QPalette.ColorGroup.Active,
                                  QPalette.ColorRole.PlaceholderText), behind),
              _over(palette.color(QPalette.ColorGroup.Disabled,
                                  QPalette.ColorRole.WindowText), behind),
              _over(faded, behind)]
    for candidate in ladder:
        if contrast_ratio(candidate, behind) >= TARGET_CONTRAST:
            return candidate
    for step in range(1, 21):
        towards_text = _mix(ladder[0], foreground, step / 20)
        if contrast_ratio(towards_text, behind) >= TARGET_CONTRAST:
            return towards_text
    return _legible(foreground, behind)     # a theme whose own text cannot be read here


def error_text_colour(palette: QPalette, behind: QColor | None = None) -> QColor:
    """The red beside Save, lightened or darkened until this theme can show it."""
    behind = behind if behind is not None and behind.isValid() else conservative_background(palette)
    return _legible(QColor(ERROR_HUE), behind)


class TintedLabel(QLabel):
    """A wrapping label whose colour is taken from the palette, never written down.

    A grey written into a stylesheet suits one desktop and vanishes on the next;
    this asks the theme instead, and asks again whenever the theme changes, so a
    desktop that switches to dark while the window is open is followed.

    The colour is derived from the *parent's* palette rather than from its own,
    which this class overwrites - deriving from a colour it had already dimmed
    would fade a shade further on every palette change - and it is measured
    against the colour really painted behind the label, not against `Window`,
    which the style does not put there.
    """

    def __init__(self, text: str = "",
                 tint: Callable[[QPalette, QColor | None], QColor] = secondary_text_colour,
                 parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        self._tint = tint
        self._tinting = False
        self.retint()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ParentChange):
            self.retint()

    def showEvent(self, event) -> None:
        # Laid out at last: only now can what is behind this label be sampled.
        super().showEvent(event)
        self.retint()

    def theme(self) -> QPalette:
        """The palette this label should take its colour from."""
        return theme_palette(self)

    def retint(self) -> None:
        if self._tinting:                    # setPalette comes back through changeEvent
            return
        self._tinting = True
        try:
            theme = self.theme()
            colour = self._tint(theme, painted_background(self, theme))
            # Two roles on an otherwise untouched palette, never a copy of the
            # resolved one: a palette with every role spoken for stops
            # inheriting, so Qt would never tell this label the theme changed
            # again - and the first thing it would miss is the switch to dark.
            palette = QPalette()
            palette.setColor(QPalette.ColorRole.WindowText, colour)
            palette.setColor(QPalette.ColorRole.Text, colour)
            self.setPalette(palette)
        finally:
            self._tinting = False


def _wrapped(text: str) -> QLabel:
    """A label that wraps: these hold sentences, not words."""
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def _caption(text: str = "") -> TintedLabel:
    """A dim one-line note under a control. Never a paragraph - see HELP."""
    label = TintedLabel(text)
    label.setProperty("caption", True)
    return label


class HelpButton(QToolButton):
    """The circled "?" beside a control: one click, one paragraph of detail.

    The tabs used to carry that paragraph inline, which is what made the window
    read as a wall of text. The words are unchanged; they are simply not on
    screen until they are asked for - and they are the tooltip as well, so
    hovering answers the question without a click.

    The circle and the glyph take the same theme-derived colour as the captions:
    a "?" nobody can see is a paragraph nobody can reach.
    """

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.help_text = text
        #: The colour the circle and the "?" are drawn in, per theme.
        self.glyph_colour = QColor()
        self._tinting = False
        self.setText("?")
        self.setAccessibleName("More information")
        self.setToolTip(_as_rich(text))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(HELP_SIZE, HELP_SIZE)
        self.retint()
        self.clicked.connect(self._show)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ParentChange):
            self.retint()

    def showEvent(self, event) -> None:
        # Laid out at last: only now can what is behind this glyph be sampled.
        super().showEvent(event)
        self.retint()

    def retint(self) -> None:
        """Take the glyph's colour from whatever theme is running now."""
        if self._tinting:                    # applying a stylesheet re-enters here
            return
        self._tinting = True
        try:
            theme = theme_palette(self)
            self.glyph_colour = secondary_text_colour(theme, painted_background(self, theme))
            self.setStyleSheet(HELP_STYLE.format(colour=self.glyph_colour.name(),
                                                 radius=HELP_SIZE // 2))
        finally:
            self._tinting = False

    def _show(self) -> None:
        QToolTip.showText(self.mapToGlobal(QPoint(0, self.height())), _as_rich(self.help_text),
                          self)


def _as_rich(text: str) -> str:
    """Help text as the small rich-text block a tooltip will wrap for us."""
    body = "<br><br>".join(escape(part.strip()) for part in text.split("\n\n") if part.strip())
    return f"<div style='max-width:{HELP_WIDTH}px'>{body}</div>"


def _preview_seconds(reply: dict) -> float:
    """How long the note about `reply` should stay up.

    A line about a pill on screen goes when the pill does, so it takes the
    daemon's own number; a reason stays long enough to be read twice. Whatever
    the reply holds, this returns a number: the note is set from a Qt slot, and
    a daemon a version ahead must not be able to raise out of one.
    """
    if not reply.get("ok"):
        return REFUSAL_NOTE_MS / 1000
    try:
        seconds = float(reply.get("seconds"))
    except (TypeError, ValueError):
        return ASSUMED_PREVIEW_SECONDS
    return seconds if seconds > 0 else ASSUMED_PREVIEW_SECONDS


def _spawn(command: list[str]) -> None:
    """Start the desktop's settings app detached: it outlives this dialog."""
    subprocess.Popen(command, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class SettingsDialog(QDialog):
    saved = Signal()
    #: The desktop's own shortcut store took a new trigger, so whatever is
    #: listening has to bind again - otherwise the old key stays live until the
    #: daemon is restarted, which is exactly the surprise this feature removes.
    shortcuts_rebound = Signal()
    _captured = Signal(str)
    #: The portal answers capture_next from the listener thread; both of these
    #: hop back onto the Qt thread before a widget is touched.
    _desktop_answered = Signal(str)

    def __init__(self, config: Config, capture_key: Callable[[Callable[[str], None]], None],
                 sources: Callable[[], list[Source]], parent=None, backend: str = "evdev",
                 triggers: Callable[[], dict[str, str]] | None = None,
                 shortcut_store: Callable[[], object | None] = desktop_shortcut_store,
                 preview_pill: Callable[[str, int, int], dict] | None = None, cancel_capture: Callable[[], None] | None = None):
        super().__init__(parent)
        self._backend = backend
        #: Asks the daemon to show the real pill at a placement for a few
        #: seconds. None when nothing can show one (no daemon behind us), and
        #: then the placer simply records the placement as it always did.
        self._preview_pill = preview_pill
        #: Where this desktop keeps its global shortcuts, or None where it keeps
        #: them somewhere we must not touch (KDE, which has its own dialog).
        self._shortcut_store = shortcut_store
        #: Reads back what the desktop actually holds per shortcut id. The
        #: portal_* fields can only ever ask for a trigger - on GNOME not even
        #: that - so this is the only truthful thing the tab can show.
        self._triggers = triggers
        self.setWindowTitle("voice settings")
        self.setMinimumWidth(560)
        # The dialog edits a private Config loaded from the same file: Close simply
        # discards it, and the daemon's live Config is never mutated from here.
        # Save writes to disk and emits `saved`; the daemon reloads from disk.
        self._cfg = Config.load(config.path)
        self._capture_key, self._sources = capture_key, sources
        #: Told when a capture is given up on. Without it the listener stays
        #: armed and takes the next key pressed anywhere, silently.
        self._cancel_capture = cancel_capture or (lambda: None)
        #: Capture waits for a key, and every way of leaving this window presses
        #: something - so it has to end on its own as well.
        self._capture_timeout = QTimer(self)
        self._capture_timeout.setSingleShot(True)
        self._capture_timeout.setInterval(CAPTURE_TIMEOUT_MS)
        self._capture_timeout.timeout.connect(
            lambda: self._stop_change(CAPTURE_GAVE_UP))
        #: The microphone the *user* picked in this window, or None while the
        #: combo is only showing what the config holds. Listing the sources is
        #: slow enough that the daemon does it in the background and calls
        #: `set_sources` later, and that must not overwrite a live choice.
        self._device_choice: str | None = None
        #: Whether this desktop can place the pill at all; None until probed.
        self._layer_shell: bool | None = None
        self._current_profile: str | None = None
        #: Rebuilding the pairing table fires every combo's signal; that is not
        #: somebody changing a pairing.
        self._loading_language_profiles = False
        self._active_changed = False       # True once "Use this profile" was pressed
        self._language_changed = False     # True once the user picked a language here
        self._inject_mode_changed = False  # True once the user picked a text insertion mode here
        self._pill_placement_changed = False  # True once the pill was moved here
        #: Which action's key is being changed right now, and whether this
        #: window is holding the keyboard to read the next combination.
        self._changing: str | None = None
        self._grabbing = False
        #: What "Change…" does here, decided once: see CAPTURE and friends.
        self._route = self._change_route()
        self._captured.connect(self._on_captured)
        self._desktop_answered.connect(self._on_desktop_answered)
        #: Every circled "?" in the window, by the control it explains.
        self.help_buttons: dict[str, HelpButton] = {}
        self.setStyleSheet(DIALOG_STYLE)
        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "General")
        tabs.addTab(self._hotkeys_tab(), "Hotkeys")
        tabs.addTab(self._audio_tab(), "Audio")
        tabs.addTab(self._transcription_tab(), "Transcription")
        tabs.addTab(self._dictionary_tab(), "Dictionary")
        self.error_label = TintedLabel("", tint=error_text_colour)
        self.save_button = QPushButton("Save")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(tabs)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        self._restore_size()
        self._load()

    # -- the pieces every tab is built from -----------------------------------
    def _restore_size(self) -> None:
        """The size this window was last closed at, if it is a usable one.

        Clamped to the screen it is opening on, not only to a floor: a size
        stored while maximised on a 4K monitor reopened at 3840x2160 on a
        laptop, with the Save and Close row below the bottom of the display and
        nothing on X11 to pull it back.
        """
        width = self._cfg.get("ui.settings_width")
        height = self._cfg.get("ui.settings_height")
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= MIN_SETTINGS_SIZE
                   for v in (width, height)):
            return
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            room = screen.availableGeometry()
            width, height = min(width, room.width()), min(height, room.height())
        self.resize(width, height)

    def _form(self, parent: QWidget | None = None) -> QFormLayout:
        """One form layout, spaced and aligned like every other one here."""
        form = QFormLayout(parent) if parent is not None else QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(COLUMN_SPACING)
        form.setVerticalSpacing(ROW_SPACING)
        form.setContentsMargins(0, 0, 0, 0)
        return form

    def _group(self, title: str, layout) -> QGroupBox:
        """A titled box around one group of rows, so a tab reads as sections."""
        box = QGroupBox(title)
        box.setLayout(layout)
        layout.setContentsMargins(MARGIN, ROW_SPACING, MARGIN, MARGIN)
        return box

    def _row(self, form: QFormLayout, label: str, field: QWidget,
             help_key: str | None = None) -> None:
        """One labelled row, with the circled "?" at the end where there is one."""
        form.addRow(label, field if help_key is None
                    else self._with_help(field, help_key, HELP[help_key]))

    def _with_help(self, field: QWidget, key: str, text: str,
                   register: bool = True) -> QWidget:
        """`field` with a circled "?" after it, as one widget the form can take.

        The "?" ends up in the same place on every row, so the buttons read as
        a column rather than as decoration stuck to each field.

        `register` is False for a row that gets rebuilt - the profile form,
        whose widgets are destroyed when another profile is selected. Kept in
        the dialog-wide dict, those keys went on pointing at deleted C++
        objects, and anything iterating it afterwards raised.
        """
        button = HelpButton(text)
        if register:
            self.help_buttons[key] = button
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(COLUMN_SPACING // 2)
        if (field.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Fixed
                or field.minimumWidth() == field.maximumWidth()):
            # A narrow field stays narrow and on the left; the "?" still lines
            # up with every other one down the right of the column.
            row.addWidget(field, 0)
            row.addStretch(1)
        else:
            row.addWidget(field, 1)
        row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        return holder

    def showEvent(self, event) -> None:
        """Re-take every dim colour once the window has been laid out.

        A child's own `showEvent` arrives before this one and, on the tab that
        opens first, before the tab widget has a pane to sample - so that tab's
        captions would keep the colour they were given against a guess while
        every other tab got the real one. Qt sends this last; by here the
        geometry is final and there is something real behind them.
        """
        super().showEvent(event)
        self.retint_all()

    def changeEvent(self, event) -> None:
        """A palette set on this window has to be passed on by hand.

        This dialog has a stylesheet, and a stylesheet stops Qt propagating a
        palette change from here down to the children - so the captions would
        never hear about it. An application-wide change reaches them on its own;
        this covers the case where the window is given a palette directly.
        """
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self.retint_all()

    def retint_all(self) -> None:
        """Every dim colour in the window, re-taken from the theme as it is now."""
        for widget in self.findChildren(TintedLabel) + self.findChildren(HelpButton):
            widget.retint()

    def reload_from_disk(self) -> None:
        """Re-read the file and repopulate every widget, discarding unsaved edits."""
        self._cfg = Config.load(self._cfg.path)
        self._current_profile = None       # so repopulating cannot commit stale form values
        # Rebuilding the pairing table fires every combo's signal, which is not
        # somebody changing a pairing. (The comment that used to sit here
        # described clearing a remembered active profile; there is no such flag
        # any more - `_active_profile_name()` reads the config.)
        self._loading_language_profiles = False
        self._active_changed = False
        self._language_changed = False
        self._inject_mode_changed = False
        self._pill_placement_changed = False
        self.preview_timer.stop()          # an abandoned gesture shows nothing
        self._say_about_preview("")
        self._stop_change("")              # nor does an abandoned key change
        self.profile_form = {}
        self._device_choice = None
        self.error_label.setText("")
        self._load()

    # -- tabs -----------------------------------------------------------------
    def _general_tab(self) -> QWidget:
        w = QWidget()
        self.language_combo = QComboBox()
        for label, code in _LANGUAGES:
            self.language_combo.addItem(label, code)
        # Only a change made *here* may overwrite the language on disk: the
        # toggle hotkey and the tray change it while this dialog sits open.
        self.language_combo.currentIndexChanged.connect(self._on_language_picked)
        self.notifications_combo = QComboBox()
        for label, on in _NOTIFICATION_LABELS:
            self.notifications_combo.addItem(label, on)
        self.inject_mode_combo = QComboBox()
        for code in INJECT_MODES:
            self.inject_mode_combo.addItem(_INJECT_MODE_LABELS[code], code)
        # Connected after the items exist, and for the same reason as the language
        # combo: only a change made *here* may overwrite what the file says.
        self.inject_mode_combo.currentIndexChanged.connect(self._on_inject_mode_picked)
        self.paste_with_combo = QComboBox()
        for value, label in PASTE_WITH_LABELS.items():
            self.paste_with_combo.addItem(label, value)
        # This screen in miniature, with the pill in it. Only a drag or a nudge
        # *here* may overwrite what the file says - it can be hand-edited while
        # this window sits open - so the widget stays quiet when it is merely
        # shown a placement.
        self.pill_placer = PillPlacer()
        self.pill_placer.placement_changed.connect(self._on_pill_placement_picked)
        # Three captions under the placement, each at most a line: what it is,
        # whether this desktop will honour it, and what the preview is doing.
        # None of them is an error, and none of them is a paragraph.
        #: One preview per gesture: a drag emits once, but arrow keys emit per
        #: keystroke, and a helper restarted per keystroke flickers across the
        #: screen. Every move restarts this; the pause after the last one shows.
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self._request_pill_preview)
        #: And what takes the note off again. A note describes what is happening
        #: now - a pill on screen, or a refusal a moment old - so none of them
        #: may outlive that and be read as still true.
        self.preview_note_timer = QTimer(self)
        self.preview_note_timer.setSingleShot(True)
        self.preview_note_timer.timeout.connect(lambda: self.preview_note.setText(""))
        self.pill_placer_note = _caption(PILL_PLACER_NOTE)
        self.pill_placement_label = _caption()
        self.placement_warning = _caption(NO_LAYER_SHELL_NOTE)
        self.placement_warning.setVisible(False)
        self.preview_note = _caption()
        beside = QVBoxLayout()
        beside.setSpacing(ROW_SPACING // 2)
        beside.addWidget(self.pill_placement_label)
        beside.addWidget(self.placement_warning)
        beside.addWidget(self.preview_note)
        # Full width rather than a form row: the placer is as wide as the screen
        # it models, and a label column beside it leaves the captions in a
        # gutter too narrow to read.
        pill_help = HelpButton(HELP["pill_position"])
        self.help_buttons["pill_position"] = pill_help
        preview = QHBoxLayout()
        preview.setSpacing(COLUMN_SPACING)
        preview.addWidget(self.pill_placer, 0, Qt.AlignmentFlag.AlignTop)
        preview.addStretch(1)
        preview.addWidget(pill_help, 0, Qt.AlignmentFlag.AlignTop)
        # The captions go under the placer, not beside it: a column beside a
        # 320 px preview is too narrow to read a sentence in.
        placement = QVBoxLayout()
        placement.setSpacing(ROW_SPACING)
        placement.addWidget(self.pill_placer_note)
        placement.addLayout(preview)
        placement.addLayout(beside)
        self.language_profile_table = QTableWidget(0, 2)
        self.language_profile_table.setHorizontalHeaderLabels(["Language", "Profile"])
        self.language_profile_table.horizontalHeader().setStretchLastSection(True)
        self.language_profile_table.verticalHeader().setVisible(False)
        # The last column stretches, so there is nothing to scroll to sideways -
        # and a scrollbar that appears anyway steals the height the rows need.
        self.language_profile_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.language_profile_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.language_profile_combos: dict[str, QComboBox] = {}

        dictation = self._form()
        self._row(dictation, "Language", self.language_combo)
        self._row(dictation, "Notifications", self.notifications_combo)
        self._row(dictation, "Inserting text", self.inject_mode_combo, "text_insertion")
        self._row(dictation, "Paste with", self.paste_with_combo, "paste_with")
        self._row(dictation, "Profile per language", self.language_profile_table,
                  "profile_per_language")
        self.profile_mode_label = _caption("")
        dictation.addRow("", self.profile_mode_label)
        layout = QVBoxLayout(w)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(self._group("Dictation", dictation))
        layout.addWidget(self._group("Recording pill", placement))
        layout.addStretch()
        return w

    def _hotkeys_tab(self) -> QWidget:
        """One list of the four actions, each showing the key in force and a
        button that changes it - whatever "change it" means on this machine.

        What it used to be: two sets of key fields, only one of them in force
        and neither saying which; a dim line under every field repeating the
        field; and a paragraph naming the mechanism, the store and the setting
        that switches between them. The owner could not find the record button
        because there was none.
        """
        w = QWidget()
        #: The key each action is bound to, as the row shows it, and the one
        #: button that changes it. On a desktop that owns the keys the labels
        #: are `portal_effective` as well: what the row shows is then exactly
        #: what the desktop holds, which is the only truthful thing to show.
        self.key_labels: dict[str, QLabel] = {}
        self.change_buttons: dict[str, QPushButton] = {}
        #: "Clear" beside each: there is no key that means "none", so unbinding
        #: needed a control of its own.
        self.clear_buttons: dict[str, QPushButton] = {}
        self.portal_edits: dict[str, QLineEdit] = {}
        self.portal_effective: dict[str, QLabel] = {}
        self.shortcuts_button: QPushButton | None = None
        #: The keyboard's own key names. These exist whatever is in force: they
        #: are what applies when voice reads the keyboard itself, and on any
        #: other route they sit in "Advanced", unread and unedited.
        self.hotkey_edit = QLineEdit()
        self.recall_edit = QLineEdit()
        self.cancel_edit = QLineEdit()
        self.language_toggle_edit = QLineEdit()
        self.key_edits = {"dictate": self.hotkey_edit, "cancel": self.cancel_edit,
                          "language_toggle": self.language_toggle_edit,
                          "recall": self.recall_edit}
        self.mode_combo = QComboBox()
        for label, code in _DICTATE_MODES:
            self.mode_combo.addItem(label, code)

        header = QHBoxLayout()
        header.setSpacing(COLUMN_SPACING)
        self.hotkey_owner_label = _wrapped(WHO_MANAGES[self._route])
        help_button = HelpButton(HOTKEY_HELP[self._route])
        self.help_buttons["hotkeys"] = help_button
        header.addWidget(self.hotkey_owner_label, 1)
        header.addWidget(help_button, 0, Qt.AlignmentFlag.AlignTop)

        keys = self._form()
        for name, label in ACTIONS:
            shown = QLabel(NOT_SET)
            shown.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            button = QPushButton(CHANGE)
            button.setAccessibleName(f"Change the key for {label.lower()}")
            button.clicked.connect(lambda _checked=False, n=name: self._start_change(n))
            clear = QPushButton(CLEAR_KEY)
            clear.setAccessibleName(f"Clear the key for {label.lower()}")
            clear.setToolTip(f"Leave {label.lower()} with no key at all")
            clear.clicked.connect(lambda _checked=False, n=name: self._clear_key(n))
            self.key_labels[name] = shown
            self.change_buttons[name] = button
            self.clear_buttons[name] = clear
            holder = QWidget()
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(COLUMN_SPACING)
            row.addWidget(shown, 1)
            row.addWidget(button, 0)
            row.addWidget(clear, 0)
            keys.addRow(label, holder)
        keys.addRow("Dictation key", self.mode_combo)
        if self._route == CAPTURE:
            # There is nothing else to capture with: the row's own button is the
            # capture button, and the evdev fields in Advanced follow it.
            self.capture_button = self.change_buttons["dictate"]
        else:
            # What the row shows *is* what the desktop holds here, so these are
            # the same labels: one key per action, on screen exactly once.
            self.portal_effective = self.key_labels
            self.capture_button = QPushButton(CHANGE)
            self.capture_button.setVisible(False)   # no keystroke ever reaches us here
            self.capture_button.clicked.connect(lambda: self._start_capture("dictate"))
        #: One line under the list: what is happening now, and how it went.
        self.hotkey_status = _caption()

        layout = QVBoxLayout(w)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(ROW_SPACING)
        layout.addLayout(header)
        layout.addWidget(self._group("Shortcuts", keys))
        layout.addWidget(self.hotkey_status)
        # Directly under the shortcuts it expands on, not pinned to the far
        # bottom of the tab where it reads as belonging to nothing.
        layout.addWidget(self._advanced_keys(), 0)
        layout.addStretch()
        return w

    def _advanced_keys(self) -> QWidget:
        """The raw fields, behind one collapsed "Advanced" line.

        Nobody needs them to work the window - the rows above set every key -
        but the values are still there for whoever wants to type one, and the
        desktop's own keyboard panel is one click away for whoever wants that.
        """
        holder = QWidget()
        self.advanced_box = QWidget()
        self.advanced_box.setVisible(False)              # collapsed until asked for
        self.advanced_button = QToolButton()
        self.advanced_button.setText(ADVANCED)
        self.advanced_button.setCheckable(True)
        self.advanced_button.setAutoRaise(True)
        self.advanced_button.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.advanced_button.toggled.connect(self._show_advanced)

        inside = QVBoxLayout(self.advanced_box)
        inside.setContentsMargins(0, ROW_SPACING, 0, 0)
        inside.setSpacing(ROW_SPACING)
        if self._route != CAPTURE:
            desktop = self._form()
            for name, label in ACTIONS:
                edit = QLineEdit()
                edit.setPlaceholderText(NOT_SET.lower())
                self.portal_edits[name] = edit
                desktop.addRow(label, edit)
            self.shortcuts_button = QPushButton(SHORTCUT_SETTINGS_BUTTON)
            self.shortcuts_button.clicked.connect(self._open_shortcut_settings)
            inside.addWidget(_caption(ADVANCED_DESKTOP_NOTE))
            inside.addLayout(desktop)
            inside.addWidget(self.shortcuts_button)
        device = self._form()
        for name, label in ACTIONS:
            edit = self.key_edits[name]
            edit.textChanged.connect(lambda _text, n=name: self._device_key_changed(n))
            if name != "dictate" or self._route == CAPTURE:
                # Where these keys are the ones in force, their own row upstairs
                # has the button - and a widget has one parent, so putting it
                # here as well would take it off that row.
                device.addRow(label, edit)
                continue
            pair = QWidget()          # the capture button nothing can capture with
            row = QHBoxLayout(pair)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(COLUMN_SPACING // 2)
            row.addWidget(edit, 1)
            row.addWidget(self.capture_button, 0)
            device.addRow(label, pair)
        inside.addWidget(_caption(ADVANCED_DEVICE_NOTE[self._route]))
        inside.addLayout(device)

        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.advanced_box)
        return holder

    def _show_advanced(self, open_it: bool) -> None:
        self.advanced_box.setVisible(open_it)
        self.advanced_button.setArrowType(Qt.ArrowType.DownArrow if open_it
                                          else Qt.ArrowType.RightArrow)

    def _audio_tab(self) -> QWidget:
        w = QWidget()
        self.device_combo = QComboBox()
        self.device_combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Fixed)
        self.device_combo.currentIndexChanged.connect(self._on_device_picked)
        self.max_seconds = QSpinBox()
        self.max_seconds.setRange(5, 600)
        self.max_seconds.setSuffix(" s")
        self.max_seconds.setFixedWidth(90)
        form = self._form()
        self._row(form, "Microphone", self.device_combo)
        self._row(form, "Stop recording after", self.max_seconds, "max_seconds")
        layout = QVBoxLayout(w)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(self._group("Recording", form))
        layout.addStretch()
        return w

    def _transcription_tab(self) -> QWidget:
        w = QWidget()
        self.profile_list = QListWidget()
        self.profile_list.currentRowChanged.connect(self._show_profile)
        self.add_profile_combo = QComboBox()
        self.add_profile_button = QPushButton("Add from template")
        self.add_profile_button.clicked.connect(self._add_profile)
        self.activate_button = QPushButton("Use this profile")
        self.activate_button.clicked.connect(self._activate_profile)
        self.remove_profile_button = QPushButton("Remove")
        self.remove_profile_button.clicked.connect(self._remove_profile)
        left = QVBoxLayout()
        left.setSpacing(ROW_SPACING // 2)
        left.addWidget(self.profile_list, 1)
        self.add_profile_label = _caption("Add another profile:")
        left.addWidget(self._with_help(self.add_profile_label, "add_template",
                                       HELP["add_template"]))
        add = QHBoxLayout()                       # the two halves of one action
        add.setSpacing(ROW_SPACING // 2)
        add.addWidget(self.add_profile_combo, 1)
        add.addWidget(self.add_profile_button)
        left.addLayout(add)
        actions = QHBoxLayout()
        actions.setSpacing(ROW_SPACING // 2)
        actions.addWidget(self._with_help(self.activate_button, "activate",
                                          HELP["activate"]), 1)
        actions.addWidget(self.remove_profile_button)
        left.addLayout(actions)
        self.profile_form: dict[str, QLineEdit] = {}
        self._form_widget = QWidget()
        self._form_layout = self._form(self._form_widget)
        self.active_label = QLabel()
        self.active_label.setStyleSheet("font-weight: bold")
        right = QVBoxLayout()
        right.setSpacing(ROW_SPACING)
        right.addWidget(self.active_label)
        right.addWidget(self._form_widget)
        right.addStretch()
        columns = QHBoxLayout()
        columns.setSpacing(COLUMN_SPACING)
        columns.addWidget(self._group("Profiles", left), 1)
        columns.addWidget(self._group("Selected profile", right), 2)
        layout = QVBoxLayout(w)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(ROW_SPACING)
        layout.addLayout(columns)
        layout.addWidget(self._with_help(
            _caption("The active profile is the one that turns your speech into text."),
            "profiles", HELP["profiles"]))
        layout.addWidget(self._group("Downloaded models", self._model_dir_row()))
        return w

    def _model_dir_row(self) -> QVBoxLayout:
        """Where the speech models are kept on this machine."""
        self.model_dir_edit = QLineEdit()
        self.model_dir_edit.setPlaceholderText(
            "~/.cache/huggingface — the shared cache, where they are now")
        self.model_dir_button = QPushButton("Browse…")
        self.model_dir_button.clicked.connect(self._pick_model_dir)
        row = QHBoxLayout()
        row.setSpacing(ROW_SPACING // 2)
        row.addWidget(self.model_dir_edit, 1)
        row.addWidget(self.model_dir_button)
        box = QVBoxLayout()
        box.setSpacing(ROW_SPACING)
        box.addWidget(self._with_help(
            _caption("Each model is a few gigabytes, downloaded the first time you "
                     "dictate in the language that uses it."),
            "model_dir", HELP["model_dir"]))
        box.addLayout(row)
        return box

    def _pick_model_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Where to keep the downloaded models", self.model_dir_edit.text())
        if chosen:
            self.model_dir_edit.setText(chosen)

    def _dictionary_tab(self) -> QWidget:
        w = QWidget()
        self.hotwords_edit = QLineEdit()
        self.hotwords_edit.setPlaceholderText("CachyOS, Hollyland Lark, Keychron")
        heard = QVBoxLayout()
        heard.setSpacing(ROW_SPACING)
        heard.addWidget(self._with_help(
            _caption("Names and jargon to expect, separated by commas."),
            "hotwords", HELP["hotwords"]))
        heard.addWidget(self.hotwords_edit)
        self.replacements_table = QTableWidget(0, 3)
        self.replacements_table.setHorizontalHeaderLabels(["Heard", "Replace with", "Options"])
        self.replacements_table.horizontalHeader().setStretchLastSection(True)
        self.replacements_table.verticalHeader().setVisible(False)
        add = QPushButton("Add row")
        add.clicked.connect(lambda: self.replacements_table.insertRow(self.replacements_table.rowCount()))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(lambda: self.replacements_table.removeRow(self.replacements_table.currentRow()))
        row = QHBoxLayout()
        row.setSpacing(ROW_SPACING // 2)
        row.addStretch()
        row.addWidget(add)
        row.addWidget(remove)
        inner = QVBoxLayout()
        inner.setSpacing(ROW_SPACING)
        inner.addWidget(self._with_help(
            _caption("What voice heard on the left, what it should write on the right."),
            "dictionary", HELP["dictionary"]))
        inner.addWidget(self.replacements_table, 1)
        inner.addLayout(row)
        layout = QVBoxLayout(w)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(self._group("Words to listen for", heard))
        layout.addWidget(self._group("Word replacements", inner), 1)
        return w

    # -- load/save --------------------------------------------------------------
    def set_sources(self, sources: Iterable[Source]) -> None:
        """Repopulate the microphone list, keeping the chosen device chosen.

        Listing them means running `pw-dump`, a subprocess with a five second
        timeout, so the daemon hands the window whatever it listed last and
        calls this again when a fresh listing arrives. Two rules make that
        safe: a device the listing does not mention still gets a row of its
        own, because a window that quietly reselected "System default" would
        write that back the next time the user pressed Save; and a choice the
        user has already made here wins over the file.
        """
        wanted = self._device_choice
        if wanted is None:
            wanted = self._cfg.get("audio.device", "") or ""
        blocked = self.device_combo.blockSignals(True)   # repopulating is not a pick
        try:
            self.device_combo.clear()
            self.device_combo.addItem("System default", "")
            for src in sources:
                self.device_combo.addItem(
                    src.description + (" (default)" if src.is_default else ""), src.name)
            if wanted and self.device_combo.findData(wanted) < 0:
                self.device_combo.addItem(wanted, wanted)
            self.device_combo.setCurrentIndex(max(0, self.device_combo.findData(wanted)))
        finally:
            self.device_combo.blockSignals(blocked)

    def _on_device_picked(self) -> None:
        self._device_choice = self.device_combo.currentData()

    def _load(self) -> None:
        c = self._cfg
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(c.get("general.language", "en"))))
        self.notifications_combo.setCurrentIndex(
            max(0, self.notifications_combo.findData(bool(c.get("general.notifications", True)))))
        self.inject_mode_combo.setCurrentIndex(max(0, self.inject_mode_combo.findData(c.get("inject.mode", "paste"))))
        self._show_pill_placement(*c.overlay_placement())
        for name, edit in self.key_edits.items():
            edit.setText(c.get(f"hotkeys.{name}", "") or "")
            self._device_key_changed(name)     # a text that did not change says nothing
        self.mode_combo.setCurrentIndex(
            max(0, self.mode_combo.findData(c.get("hotkeys.dictate_mode", "hold"))))
        for name, edit in self.portal_edits.items():
            edit.setText(c.portal_trigger(name))
        self.refresh_effective_triggers()
        self._device_choice = None         # populating the combo is not a user edit
        self.set_sources(self._sources())
        self.max_seconds.setValue(int(c.get("audio.max_seconds", 120)))
        self.model_dir_edit.setText(str(c.get("stt.model_dir", "") or ""))
        self.paste_with_combo.setCurrentIndex(
            max(0, self.paste_with_combo.findData(c.get("inject.paste_with", "auto"))))
        # The pairing table first: the profile list is annotated from those
        # combos, and building it first read the ones this reload is about to
        # discard - so a dropped edit lived on in the other tab's labels.
        self._load_language_profiles()
        self._update_profile_mode()
        # The profile in use, not whatever sorts first: alphabetical order put
        # `groq` - an empty API-key form - in front of the `local` that is
        # actually transcribing.
        self._load_profile_list(select=str(c.get("stt.active") or ""))
        self.active_label.setText(f"Active profile: {c.get('stt.active')}")
        words = c.get("dictionary.hotwords", []) or []
        self.hotwords_edit.setText(", ".join(str(word) for word in words)
                                   if isinstance(words, list) else str(words))
        rules = c.get("dictionary.replacements", []) or []
        self.replacements_table.setRowCount(len(rules))
        for i, rule in enumerate(rules):
            self.set_replacement_row(i, *(list(rule) + ["", "", ""])[:3])
        self._language_changed = False     # populating the combos is not a user edit
        self._inject_mode_changed = False
        self._pill_placement_changed = False

    def _show_pill_placement(self, position: str, margin_x: int, margin_y: int) -> None:
        """Put a placement into the preview and say it in words beside it.

        Fed from `Config.overlay_placement()`, so a file holding one of the two
        older values - or something that is not a placement at all - still shows
        the pill somewhere real.
        """
        self.pill_placer.set_placement(position, margin_x, margin_y)
        self.pill_placement_label.setText(placement_summary(position, margin_x, margin_y))

    def set_layer_shell(self, available: bool | None) -> None:
        """Say whether this desktop can put the pill where the placer says.

        `False` is the GNOME case the owner hit: a plain GTK window on Wayland
        cannot position itself and GNOME has no layer shell, so the compositor
        decides and the pill appears in the middle whatever is dragged here.
        The control stays live - the setting is recorded, and it applies on a
        machine that does have one. `None` is "nobody has probed yet", which is
        not a claim either way and shows nothing.
        """
        self._layer_shell = available
        self.placement_warning.setVisible(available is False)

    def _chosen_pill_placement(self) -> tuple[str, int, int]:
        """The placement as this dialog would save it."""
        return self.pill_placer.placement()

    def _load_language_profiles(self, mapping: dict[str, str] | None = None) -> None:
        """One row per general.languages entry, each with the profiles that exist.

        `mapping` overrides what the file says, so the table can be rebuilt after a
        profile is added without losing choices made here but not yet saved.
        """
        mapping = self._cfg.language_profiles() if mapping is None else mapping
        profiles = list(self._cfg.get("stt.profiles", {}) or {})
        codes = self._cfg.languages()
        self._loading_language_profiles = True
        table = self.language_profile_table
        # Take the old combos out by hand: replacing a cell widget only schedules
        # the previous one for deletion, and until that runs it stays parented to
        # the viewport and paints over the first cell.
        for row in range(table.rowCount()):
            for col in range(table.columnCount()):
                widget = table.cellWidget(row, col)
                if widget is not None:
                    table.removeCellWidget(row, col)
                    widget.setParent(None)
                    widget.deleteLater()
        self.language_profile_combos = {}
        table.setRowCount(len(codes))
        for row, code in enumerate(codes):
            item = QTableWidgetItem(_LANGUAGE_NAMES.get(code.strip().lower(), code))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, item)
            combo = QComboBox()
            combo.addItem(KEEP_CURRENT, "")
            for name in profiles:
                combo.addItem(name, name)
            # A map naming a profile that no longer exists falls back to
            # "(keep current)" rather than offering something unbuildable.
            combo.setCurrentIndex(max(0, combo.findData(mapping.get(code.lower(), ""))))
            # Changing a pairing changes what the Transcription tab's list says
            # each profile is for, and what the line under this table says about
            # which way round the machine is set up. Both follow at once, or
            # they disagree until the next save.
            combo.currentIndexChanged.connect(self._on_pairing_picked)
            table.setCellWidget(row, 1, combo)
            self.language_profile_combos[code] = combo
        # Exactly as tall as its rows: a fixed height leaves either dead space
        # under two languages or a scrollbar under four. The rows have to be
        # measured after the combos are in them - a row still holding only its
        # default height reports about half what the combo will need, which is
        # what squashed this table to a row and a half.
        table.resizeRowsToContents()
        # Each row set to what it actually needs, and only then summed. Summing
        # what the rows *would* be while leaving them to lay themselves out let
        # the total come up short by a few pixels a row, and the last row was
        # then clipped by the group's frame with the caption on top of it.
        rows = 0
        for r in range(table.rowCount()):
            widget = table.cellWidget(r, 1)
            height = max(table.rowHeight(r),
                         widget.sizeHint().height() if widget else 0,
                         widget.minimumSizeHint().height() if widget else 0)
            table.setRowHeight(r, height)
            rows += height
        # A little slack, and a floor rather than only a ceiling. The exact sum
        # is right on the style this was measured on and a few pixels short on
        # Breeze, where the last row was clipped by the group's frame with the
        # caption sitting on top of it. Slack costs nothing; clipping hides a
        # language.
        needed = (table.horizontalHeader().sizeHint().height() + rows
                  + 2 * table.frameWidth() + TABLE_SLACK)
        table.setMinimumHeight(needed)
        table.setFixedHeight(needed)
        self._loading_language_profiles = False

    def _on_language_picked(self, index: int) -> None:
        self._language_changed = True

    def _on_inject_mode_picked(self, index: int) -> None:
        self._inject_mode_changed = True

    def _on_pill_placement_picked(self) -> None:
        """The owner dragged or nudged the pill in the preview."""
        self._pill_placement_changed = True
        self.pill_placement_label.setText(placement_summary(*self.pill_placer.placement()))
        # Whatever the note said was about the placement that has just been left
        # behind, so it is not true of this one even where it will be again.
        self._say_about_preview("")
        if self._preview_pill is not None:
            self.preview_timer.start(PREVIEW_DELAY_MS)

    def _say_about_preview(self, note: str, seconds: float = 0.0) -> None:
        """Put one line about the preview beside the placer, for `seconds`.

        Its own line, under the note about what this desktop does with a
        placement: the two answer different questions - "will this setting be
        honoured here" and "did anything happen just now" - and an owner on
        GNOME needs both at once.
        """
        self.preview_note_timer.stop()
        self.preview_note.setText(note)
        if note:
            self.preview_note_timer.start(int(seconds * 1000))

    def preview_message(self, reply: dict) -> str:
        """What a reply from the daemon says, in words for this window."""
        if reply.get("ok"):
            return PREVIEW_SHOWING if self._layer_shell is not False else PREVIEW_SHOWING_ANYWHERE
        error = str(reply.get("error") or "").strip()
        if not error:
            return PREVIEW_CANNOT
        return PREVIEW_REFUSALS.get(error.lower(), PREVIEW_REFUSED.format(error=error))

    def _request_pill_preview(self) -> None:
        """Ask the daemon to put the real pill where the placer says, briefly.

        Nothing here is a settings error: a daemon that refuses (no pill, or a
        dictation in flight) has a reason worth reading, and a daemon that has
        gone away is worth saying once - but neither belongs in the red label
        beside Save, and neither may stop the placement being saved.

        What it may not do is say nothing at all, which is what it used to do:
        the owner dragged the pill while dictating, saw nothing happen, and
        reported the feature as broken. The daemon had said why the whole time.
        """
        if self._preview_pill is None:
            return
        try:
            reply = self._preview_pill(*self.pill_placer.placement()) or {}
        except Exception as exc:
            log.debug("the pill preview could not be started: %s", exc)
            self._say_about_preview(PREVIEW_REFUSED.format(error=exc),
                                    REFUSAL_NOTE_MS / 1000)
            return
        self._say_about_preview(self.preview_message(reply), _preview_seconds(reply))

    def set_replacement_row(self, row: int, src: str, dst: str, flags: str = "") -> None:
        for col, val in enumerate((src, dst, flags)):
            self.replacements_table.setItem(row, col, QTableWidgetItem(str(val)))

    def _load_profile_list(self, select: str | None = None) -> None:
        """The profiles, in name order, each saying which language picks it.

        Alphabetical because config order is the order they were added, which
        is nothing to a reader. Annotated because five bare names gave no clue
        which two the owner's own languages actually use.
        """
        # Which row to end up on, and - separately - what the form is currently
        # showing. They are not the same question: a reload asks for a row while
        # the form holds nothing, and conflating them left the form unbuilt.
        chosen = select if select is not None else self._current_profile
        # Signals blocked across the rebuild: clear() selects row -1, which
        # tears the profile form down and commits it - and a commit that fails
        # validation rebuilds every field from disk, losing edits on rows that
        # were perfectly good. Nothing about relabelling this list should touch
        # what is being typed on the other side of the tab.
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        used_by: dict[str, list[str]] = {}
        # What the table says, not what the file says: the caption under it
        # reads the same source, and the two disagreed over an unsaved change.
        for code, name in self._pairings_now().items():
            used_by.setdefault(name, []).append(language_name(code))
        active = self._active_profile_name()
        for name in sorted(self._cfg.get("stt.profiles", {}) or {}):
            self._add_profile_row(name, used_by.get(name, []), name == active)
        row = self._profile_row(chosen) if chosen else -1
        row = row if row >= 0 else 0
        self.profile_list.setCurrentRow(row)
        self.profile_list.blockSignals(False)
        self._refresh_templates_on_offer()
        if self._profile_at(row) != self._current_profile:
            self._show_profile(row)           # a different profile: rebuild the form
        else:
            self._refresh_profile_buttons()   # the same one: only the labels moved

    def _on_pairing_picked(self) -> None:
        if self._loading_language_profiles:
            return              # populating the combos is not a user edit
        self._load_profile_list()
        self._update_profile_mode()

    def _pairings_now(self) -> dict[str, str]:
        """The language pairing as this window currently shows it.

        The table during a session, the file before it is built - the profile
        list is loaded once from `reload_from_disk` before the General tab's
        combos exist.
        """
        if not self.language_profile_combos:
            return self._cfg.language_profiles()
        return {code: name for code, name in self._chosen_language_profiles().items()
                if name}

    def _active_profile_name(self) -> str:
        """Which profile actually transcribes.

        Read from the config this window edits, never from a flag beside it.
        A remembered copy has to be cleared at every path that changes
        `stt.active` - "Use this profile", a language save, an external edit
        carried over - and missing one of them let the window offer to delete
        the profile that was really in use, which blocks every later save. It
        was missed twice. There is nothing to miss now.
        """
        return str(self._cfg.get("stt.active") or "")

    def _add_profile_row(self, name: str, languages: Iterable[str] = (),
                         active: bool = False) -> None:
        """One row: the name is the data, the label is what it is for."""
        parts = list(languages)
        if active:
            # "I press Use this profile but don't see what it does." Now the
            # list says so, on the row, in the same words as the button.
            parts.append("in use")
        item = QListWidgetItem(f"{name} — {', '.join(parts)}" if parts else name)
        # The real name lives in the data, so the label can say anything: every
        # reader of this list asks the item for its data, never its text.
        item.setData(Qt.ItemDataRole.UserRole, name)
        if active:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        self.profile_list.addItem(item)

    def _update_profile_mode(self) -> None:
        """One line saying which way round this machine is set up.

        The two controls are alternatives and nothing said so: with a pairing,
        the language chooses the model; without one, you choose it yourself and
        the language never touches it.
        """
        paired = [code for code, name in self._pairings_now().items() if name]
        if paired:
            named = [language_name(code) for code in sorted(paired)]
            names = " and ".join([", ".join(named[:-1]), named[-1]] if len(named) > 1
                                 else named)
            text = (f"Switching to {names} also switches the model. "
                    f'"Use this profile" on the Transcription tab overrides that '
                    f"until the next language switch.")
        else:
            text = ('Nothing is paired, so switching language never changes the '
                    'model - you choose it yourself with "Use this profile" on the '
                    'Transcription tab.')
        self.profile_mode_label.setText(text)

    def _refresh_templates_on_offer(self) -> None:
        """Only templates this config does not already define.

        The shipped config now defines every one of them, so on a new install
        the button did nothing whatever was picked - under a caption and a "?"
        this branch added to draw attention to it.
        """
        defined = set(self._cfg.get("stt.profiles", {}) or {})
        wanted = [name for name in PROFILE_TEMPLATES if name not in defined]
        shown = [self.add_profile_combo.itemText(i)
                 for i in range(self.add_profile_combo.count())]
        if wanted != shown:
            # Only the list is skipped when nothing moved - rebuilding it would
            # throw away the owner's selection on every relabel of the profile
            # list. What the button *does* is decided every time, unconditionally:
            # guarding that too left it pressable over an empty dropdown, which
            # turned a button that did nothing into one that raised KeyError.
            self.add_profile_combo.clear()
            self.add_profile_combo.addItems(wanted)
        self.add_profile_combo.setEnabled(bool(wanted))
        self.add_profile_button.setEnabled(bool(wanted))
        self.add_profile_label.setText(
            "Add another profile:" if wanted
            else "Every ready-made profile is already here.")

    def _refresh_profile_buttons(self) -> None:
        """What can be done to the selected profile, said by the buttons.

        A button that is pressable and does nothing is worse than one that is
        greyed out: the first time, it reads as the program ignoring you.
        """
        name = self._current_profile
        active = self._active_profile_name()
        self.activate_button.setEnabled(bool(name) and name != active)
        self.activate_button.setText("Already in use" if name and name == active
                                     else "Use this profile")
        # The last profile is not removable either: stt.active has to name one.
        removable = bool(name) and name != active and self.profile_list.count() > 1
        self.remove_profile_button.setEnabled(removable)

    def _profile_at(self, row: int) -> str | None:
        item = self.profile_list.item(row)
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _profile_row(self, name: str) -> int:
        for row in range(self.profile_list.count()):
            if self._profile_at(row) == name:
                return row
        return -1

    def _show_profile(self, row: int) -> None:
        self._commit_profile_form()
        name = self._profile_at(row)
        if name is None:
            return
        self._current_profile = name
        profile = self._cfg.get(f"stt.profiles.{name}", {}) or {}
        while self._form_layout.rowCount():
            self._form_layout.removeRow(0)
        self.profile_form = {}
        self.profile_help_buttons = {}
        fields = _LOCAL_FIELDS if profile.get("backend") == "local" else _CLOUD_FIELDS
        kind = str(profile.get("backend", ""))
        self._form_layout.addRow(_FIELD_LABELS["backend"],
                                 QLabel(_BACKEND_LABELS.get(kind, kind)))
        for field in fields:
            if field == "model" and profile.get("backend") == "local":
                edit = ModelChooser()
                edit.setText(str(profile.get(field, "")))
            else:
                edit = QLineEdit(str(profile.get(field, "")))
            if field == "api_key":
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.profile_form[field] = edit
            # Every row, not the ones that happened to seem obvious: "what the
            # hell is search width, and 5 is what?" was asked about a row that
            # had no "?" precisely because it looked self-explanatory.
            # A cloud profile's Model row wants the name that service uses, so
            # the nine local Whisper models behind the shared "?" are wrong there.
            key = ("cloud_model" if field == "model" and kind != "local" else field)
            labelled = self._with_help(edit, key, HELP[key], register=False)
            self.profile_help_buttons[field] = labelled.findChild(QToolButton)
            self._form_layout.addRow(_FIELD_LABELS.get(field, field), labelled)
        self._refresh_profile_buttons()

    def _commit_profile_form(self) -> bool:
        if not self._current_profile or not self.profile_form:
            return True
        values: dict[str, object] = {}
        for field, edit in self.profile_form.items():
            value: object = edit.text()
            if field == "beam_size":
                try:
                    value = int(value or 5)
                except ValueError:
                    value = None
                if value is None or value < 1:
                    self.error_label.setText(
                        f"{self._current_profile}: {_FIELD_LABELS['beam_size']} must be a "
                        "whole number, 1 or more")
                    return False
            values[field] = value
        for field, value in values.items():
            self._cfg.set(f"stt.profiles.{self._current_profile}.{field}", value)
        return True

    def _add_profile(self) -> None:
        name = self.add_profile_combo.currentText()
        # Belt as well as braces: the button is disabled when there is nothing
        # to add, and this is what makes an empty selection harmless anyway.
        if not name or name not in PROFILE_TEMPLATES or self._profile_row(name) >= 0:
            return
        for field, value in PROFILE_TEMPLATES[name].items():
            self._cfg.set(f"stt.profiles.{name}.{field}", value)
        # Rebuilt rather than appended: the list is in name order, and a new
        # profile belongs where its name puts it.
        self._load_profile_list()
        self.profile_list.setCurrentRow(max(0, self._profile_row(name)))
        # The new profile has to be selectable per language straight away: adding
        # local-swedish and mapping sv to it is one visit to this window.
        self._load_language_profiles(self._chosen_language_profiles())
        self._update_profile_mode()

    def _activate_profile(self) -> None:
        """Make the selected profile the one that transcribes.

        And, when exactly one language is paired with it, switch to that
        language too. Otherwise choosing the row that reads "local — English"
        left the dictation in Swedish with the English model loaded - the pill
        said SE, the model said English, and the button looked like it had done
        nothing at all. Two languages sharing a profile have no single answer,
        so that case changes only the model.
        """
        name = self._current_profile
        if not name:
            return
        self._cfg.set("stt.active", name)
        self._active_changed = True
        self.active_label.setText(f"Active profile: {name}")
        claimed = [code for code, mapped in self._pairings_now().items()
                   if mapped == name]
        if len(claimed) == 1:
            index = self.language_combo.findData(claimed[0])
            if index >= 0:
                self.language_combo.setCurrentIndex(index)
        self._load_profile_list()
        self._update_profile_mode()

    def _remove_profile(self) -> None:
        """Delete the selected profile, and anything that pointed at it.

        A language left mapped to a profile that is gone is a config error, and
        one this window would have no row to fix - it would block every later
        save. Unpairing here is the only way the removal can be legal.
        """
        name = self._current_profile
        if not name or name == self._active_profile_name():
            return
        self._current_profile = None          # so nothing commits into a dead table
        self.profile_form = {}
        self._cfg.unset(f"stt.profiles.{name}")
        for code, mapped in list(self._cfg.language_profiles().items()):
            if mapped == name:
                self._cfg.unset(f"general.language_profiles.{code}")
        self._load_profile_list()
        self._load_language_profiles(self._chosen_language_profiles())
        self._update_profile_mode()

    def _change_route(self) -> str:
        """What "Change…" has to do on this machine.

        Three mechanisms, one button. Where voice reads the keyboard itself it
        can simply take the next key. Where the desktop owns the keys but keeps
        them somewhere we may write, it takes the next combination and hands it
        over. Where the desktop insists on its own window, that window is what
        the button opens. The user never has to know which - but the window does,
        because every sentence on the tab turns on it.
        """
        if self._backend != "portal":
            return CAPTURE
        try:
            store = self._shortcut_store()
        except Exception as exc:                 # a probe is never worth a broken window
            log.debug("cannot tell how this desktop keeps its shortcuts: %s", exc)
            return DESKTOP_DIALOG
        return DESKTOP_STORE if store is not None else DESKTOP_DIALOG

    def refresh_effective_triggers(self) -> None:
        """Show what the desktop holds right now, on the row each key belongs to.

        Called on every load, so reopening the window (which re-reads the file,
        and makes the daemon ask the portal again) also re-reads the keys - a
        row showing what was bound when the daemon started is the same lie as a
        field that cannot move one.
        """
        if not self.portal_effective:
            return
        triggers = self._desktop_triggers()
        for name, label in self.portal_effective.items():
            label.setText(desktop_key_text(triggers, name))

    def _desktop_triggers(self) -> dict[str, str]:
        """What the desktop says it holds, or nothing where nobody can say."""
        if self._triggers is None:
            return {}
        try:
            return dict(self._triggers() or {})
        except Exception:
            return {}                  # never let a dead accessor block the window

    def _device_key_changed(self, name: str) -> None:
        """An evdev field was typed in or filled from a capture.

        The row above it shows the key in force, so where these fields *are*
        what is in force it has to follow them; where the desktop owns the keys
        they are a spare set and the row keeps showing the desktop's answer.
        """
        if self._route != CAPTURE:
            self._show_key_row(name)
            return
        self.key_labels[name].setText(pretty_device_key(self.key_edits[name].text()) or NOT_SET)
        self._show_key_row(name)

    def _show_key_row(self, name: str) -> None:
        """Whether this row has anything to clear."""
        if name not in self.clear_buttons:
            return
        edit = self.key_edits.get(name) or self.portal_edits.get(name)
        self.clear_buttons[name].setEnabled(bool(edit and edit.text().strip()))

    def hotkey_help_text(self) -> str:
        """The detail behind the Hotkeys tab's "?", for what is in force here."""
        return HOTKEY_HELP[self._route]

    # -- one button per key ----------------------------------------------------
    def _clear_key(self, name: str) -> None:
        """Unbind one action. Written on Save like any other edit."""
        if self._changing is not None:
            self._stop_change(CHANGE_STOPPED)
        if name in self.key_edits:
            self.key_edits[name].setText("")
        if name in self.portal_edits:
            self.portal_edits[name].setText("")
        self._show_key_row(name)
        self._say_about_hotkeys("")

    def _start_change(self, name: str) -> None:
        """"Change…" on one row: set that key, however this machine does it."""
        if self._changing is not None:
            self._stop_change(CHANGE_STOPPED)    # a second press gives up on the first
            return
        if self._route == CAPTURE:
            self._start_capture(name)
        elif self._route == DESKTOP_STORE:
            self._start_chord(name)
        else:
            self._begin_change(name)
            self._open_shortcut_settings()

    def _begin_change(self, name: str) -> None:
        """Say, on the row and under the list, that this key is being changed."""
        self._changing = name
        self._capture_timeout.start()
        self.change_buttons[name].setText(CHANGE_BUSY[self._route])
        self._say_about_hotkeys(CHANGE_PROMPT[self._route])

    def _stop_change(self, note: str) -> None:
        """Put the button back and say how it went, in one line or none."""
        name, self._changing = self._changing, None
        self._capture_timeout.stop()
        if name is not None:
            # Whatever ended it, the listener must stop waiting: left armed, it
            # takes the next key pressed anywhere and saves it.
            self._cancel_capture()
        self._release_keyboard()
        if name is not None and name in self.change_buttons:
            self.change_buttons[name].setText(CHANGE)
        self._say_about_hotkeys(note)

    def _say_about_hotkeys(self, note: str) -> None:
        self.hotkey_status.setText(note)

    def _start_capture(self, name: str = "dictate") -> None:
        """Take the next key straight from the keyboard, for `name`."""
        self._begin_change(name)
        self._capture_key(self._captured.emit)

    def _on_captured(self, key: str) -> None:
        """A key name from the listener - or a sentence, where it cannot say one.

        An answer to a change nobody is waiting for any more is dropped: the
        listener is asked once and answers whenever the user presses something,
        which can be after the button was pressed again to give up. Writing that
        key into a row the user has stopped editing is how a hotkey changes
        itself.
        """
        name = self._changing
        if name is None or name not in self.key_edits:
            log.debug("a captured key arrived after the change was given up: %s", key)
            return
        if key.startswith(("KEY_", "BTN_")):
            self.key_edits[name].setText(key)    # the row follows the field
            self._stop_change("")
            return
        # A listener that cannot hand back a key name - the desktop keeps the
        # keystroke - answers with a sentence about itself instead. What it
        # means for the user is the same every time, so that is what is shown.
        log.info("the listener could not capture a key: %s",
                 key or "(it returned nothing)")
        self._stop_change(CAPTURE_NOT_POSSIBLE)

    # -- the next combination, read here and handed to the desktop -------------
    def _start_chord(self, name: str) -> None:
        """Hold the keyboard until the user presses the combination they want.

        The desktop owns the key, so there is nothing to capture from the
        keyboard device - but this window is an ordinary window, and an ordinary
        window is told about a combination the desktop has not already taken.
        That is enough to ask for a new one.
        """
        self._begin_change(name)
        app = QApplication.instance()
        if app is None:                          # nothing to read keys from
            self._stop_change(CHANGE_STOPPED)
            return
        self._grabbing = True
        app.installEventFilter(self)

    def _release_keyboard(self) -> None:
        if not self._grabbing:
            return
        self._grabbing = False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        """Every key press in the application, while a combination is wanted.

        Swallowed rather than passed on: the window is full of fields, and a
        chord typed at one of them must not also land in it.
        """
        if not self._grabbing:
            return super().eventFilter(watched, event)
        kind = event.type()
        if kind in (QEvent.Type.KeyRelease, QEvent.Type.ShortcutOverride):
            return True
        if kind != QEvent.Type.KeyPress:
            return super().eventFilter(watched, event)
        if event.key() == Qt.Key.Key_Escape:
            self._stop_change(CHANGE_STOPPED)
            return True
        trigger = chord_trigger(event)
        if trigger is None:                      # a modifier on its own: keep waiting
            return True
        name = self._changing
        if name is None:                         # nothing is being changed any more
            self._stop_change(CHANGE_STOPPED)
            return True
        problem = chord_problem(trigger)
        if problem is not None:
            # Say why and keep listening: the user has just pressed a key, and a
            # window that let go of the keyboard here would leave them with
            # nothing waiting for the key they press instead. Escape is read
            # above, before this, so it can always end the wait.
            self._say_about_hotkeys(problem)
            return True
        self._stop_change("")
        self._give_the_desktop(name, trigger)
        return True

    def _give_the_desktop(self, name: str, trigger: str) -> None:
        """Hand one new combination to the desktop, and say how that went.

        The row shows what the desktop holds, so it may only move once the
        desktop has taken the key: a refusal leaves the row on the truth and
        puts the reason where the user is already looking.
        """
        self.portal_edits[name].setText(trigger)
        known = self._desktop_triggers()
        if known and name not in known:
            # The desktop has never been told this shortcut exists, so its store
            # has nothing to change and would skip it in silence. The field now
            # holds the key, and saving is what makes the desktop ask for it.
            self._say_about_hotkeys(NOT_OFFERED)
            return
        if not self._apply_desktop_shortcuts(CHANGED_ON_DESKTOP.format(
                key=pretty_trigger(trigger), what=ACTION_LABELS[name].lower())):
            self.refresh_effective_triggers()
            if not self.error_label.text():
                # A store that went away between the probe and the write says
                # nothing at all, and a button that does nothing and says
                # nothing is the whole complaint this tab was rebuilt over.
                self._say_about_hotkeys(CHANGE_REFUSED)
            return
        self.key_labels[name].setText(pretty_trigger(trigger))
        self._keep_the_trigger(name, trigger)
        self.shortcuts_rebound.emit()

    def _keep_the_trigger(self, name: str, trigger: str) -> None:
        """Write a trigger the desktop has taken into the config, at once.

        The desktop holds the new key from this moment on, so the file has to
        as well: without this, closing the window without saving left the field
        on the old trigger and the row on the new one - the tab contradicting
        itself - and the next Save for any reason wrote every field back,
        pushing the old trigger over the key the user had just set.

        Through a fresh read of the file, not this window's snapshot, for the
        same reason the daemon does it that way: only the one setting that has
        actually changed may be written from here. The snapshot is updated too,
        so a later Save from this window agrees with the file.
        """
        self._cfg.set(f"hotkeys.portal_{name}", trigger)
        try:
            on_disk = Config.load(self._cfg.path)
            on_disk.set(f"hotkeys.portal_{name}", trigger)
            on_disk.save()
        except Exception as exc:
            log.exception("could not save the trigger the desktop took")
            self.error_label.setText(TRIGGER_NOT_KEPT.format(
                key=pretty_trigger(trigger), error=exc))

    def _apply_desktop_shortcuts(self, note: str = SAVED_TO_DESKTOP) -> bool:
        """Put the triggers into the desktop's own store, and say so.

        Only a desktop that owns the keys has any: where voice reads the
        keyboard the key is ours and no desktop is involved. A desktop that
        keeps its shortcuts somewhere we must not touch answers None and is left
        alone - the trigger is still saved, and its own dialog applies it.

        Returns whether the listener now needs to bind again.
        """
        if self._backend != "portal" or not self.portal_edits:
            return False
        try:
            store = self._shortcut_store()
        except Exception as exc:
            log.debug("cannot reach this desktop's shortcut store: %s", exc)
            return False
        if store is None:
            return False
        triggers = {name: edit.text().strip() for name, edit in self.portal_edits.items()}
        try:
            message = store.write(triggers)
        except ShortcutStoreError as exc:
            # A refusal is the store protecting the user's other shortcuts, and
            # the one thing they have to see: it is why the key did not move.
            self.error_label.setText(str(exc))
            return False
        except Exception as exc:
            log.exception("writing the desktop's shortcut store failed")
            self.error_label.setText(f"Could not change this desktop's shortcuts: {exc}")
            return False
        log.info("the desktop's shortcut store: %s", message)
        refused = dict(getattr(store, "refused", None) or {})
        if refused:
            # The rest of the set did move, so this is not a failed save - it is
            # the one field that stayed behind, named, because a trigger nothing
            # can be made of is otherwise invisible until the key never fires.
            self.error_label.setText("; ".join(refused.values()))
        self._say_about_hotkeys(note)
        return True

    def _open_shortcut_settings(self) -> None:
        """Take the user to wherever this desktop really keeps the key.

        Some desktops have a window of their own for exactly this and the
        listener opens it; that is the same call as a capture, and it answers
        with a sentence either way. Anywhere else - a desktop with no such
        window, or a shortcut that was never registered - falls through to the
        desktop's settings app.
        """
        try:
            self._capture_key(self._desktop_answered.emit)
        except Exception as exc:
            log.debug("the desktop would not open its shortcut window: %s", exc)
            self._launch_shortcut_settings()

    def _on_desktop_answered(self, message: str) -> None:
        if message == DIALOG_MESSAGE:               # the desktop's own window is up
            self._stop_change(DESKTOP_DIALOG_OPEN)
            return
        self._launch_shortcut_settings()

    def _launch_shortcut_settings(self) -> None:
        command = shortcut_settings_command()
        if command is None:
            self._stop_change(SETTINGS_APP_MISSING)
            return
        try:
            _spawn(command)
        except Exception as exc:
            self._stop_change(SETTINGS_APP_FAILED.format(error=exc))
            return
        self._stop_change(SETTINGS_APP_OPENED)

    def closeEvent(self, event) -> None:
        """Never leave this window reading the keyboard after it is gone."""
        self._leaving()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        """Escape and reject() never reach closeEvent, and this is the way out
        most people take: the size was forgotten, and the keyboard capture -
        which closeEvent's own docstring says must always stop - was left
        running."""
        self._leaving()
        super().done(result)

    def _leaving(self) -> None:
        self._stop_change("")
        self._remember_size()

    def _remember_size(self) -> None:
        """Reopen at the size it was left at.

        Written straight to the file rather than waiting for Save: the size is
        not a setting anyone edits, and closing without saving is exactly when
        the window was resized to something worth keeping. A failure here is
        never allowed to stop the window closing.
        """
        try:
            width, height = int(self.width()), int(self.height())
            if (self._cfg.get("ui.settings_width"), self._cfg.get("ui.settings_height")) \
                    == (width, height):
                # Already recorded. Closing runs this twice - closeEvent, then
                # reject() -> done() underneath it - and each write is a whole
                # file rewritten, fsynced and replaced.
                return
            fresh = Config.load(self._cfg.path)
            fresh.set("ui.settings_width", width)
            fresh.set("ui.settings_height", height)
            fresh.save()
            self._cfg.set("ui.settings_width", width)
            self._cfg.set("ui.settings_height", height)
        except Exception:
            log.debug("could not remember the settings window size", exc_info=True)

    def _carry_over_external_edits(self) -> bool:
        """Keep settings switched from the tray, a hotkey or the CLI since this
        dialog opened.

        The dialog edits a whole-document snapshot, so writing it back would
        otherwise revert them. Each field is carried only when the user did not
        change it here. Returns False if the file cannot be read.
        """
        try:
            on_disk = Config.load(self._cfg.path)
        except ValueError as exc:
            self.error_label.setText(f"{self._cfg.path.name}: {exc}")
            return False
        if not self._active_changed:
            self._carry_over_active_profile(on_disk)
        if not self._language_changed:
            self._carry_over_language(on_disk)
        if not self._inject_mode_changed:
            self._carry_over_inject_mode(on_disk)
        if not self._pill_placement_changed:
            self._carry_over_pill_placement(on_disk)
        return True

    def _carry_over_active_profile(self, on_disk: Config) -> None:
        active = on_disk.get("stt.active")
        if not active or active == self._cfg.get("stt.active"):
            return
        if self._language_changed and self._chosen_for_another_language(on_disk, active):
            return
        # Only if this document actually defines it; otherwise the write would
        # produce a config errors() rejects and the save would be blocked.
        if active in (self._cfg.get("stt.profiles", {}) or {}):
            self._cfg.set("stt.active", active)
            self.active_label.setText(f"Active profile: {active}")

    def _chosen_for_another_language(self, on_disk: Config, active: str) -> bool:
        """Whether `active` is the profile the *file's* language selected, while
        this dialog is about to write a different language.

        Carrying it over then pairs the Swedish model with English: the switch
        that wrote the two wrote them as a pair, and only half of it would
        survive here. Asked only when the user picked a language in this dialog -
        otherwise the pair is carried over whole, language included.
        """
        on_disk_language = str(on_disk.get("general.language", "") or "").strip().lower()
        if on_disk.profile_for_language(on_disk_language) != active:
            return False
        return str(self._cfg.get("general.language", "") or "").strip().lower() != on_disk_language

    def _carry_over_language(self, on_disk: Config) -> None:
        language = on_disk.get("general.language")
        if not language or language == self._cfg.get("general.language"):
            return
        if not is_language_code(language):     # same guard: never write a bad value
            return
        self._cfg.set("general.language", language)
        index = self.language_combo.findData(language)
        if index >= 0:
            self.language_combo.setCurrentIndex(index)   # show what was really saved
        self._language_changed = False         # that was us, not the user

    def _carry_over_inject_mode(self, on_disk: Config) -> None:
        """Same as the language above: inject.mode can be changed from the CLI
        while this dialog holds its snapshot."""
        mode = on_disk.get("inject.mode")
        if not mode or mode == self._cfg.get("inject.mode"):
            return
        if mode not in INJECT_MODES:           # never write a value errors() rejects
            return
        self._cfg.set("inject.mode", mode)
        index = self.inject_mode_combo.findData(mode)
        if index >= 0:
            self.inject_mode_combo.setCurrentIndex(index)   # show what was really saved
        self._inject_mode_changed = False      # that was us, not the user

    def _carry_over_pill_placement(self, on_disk: Config) -> None:
        """The same as the language and the mode above: the pill can be moved by
        a hand edit and a `voice reload` while this window holds its snapshot.

        Read through overlay_placement(), so what is carried over is a placement
        the config accepts rather than whatever spelling the file happened to use.
        """
        placement = on_disk.overlay_placement()
        if placement == self._cfg.overlay_placement():
            return
        position, margin_x, margin_y = placement
        self._cfg.set("ui.overlay_position", position)
        self._cfg.set("ui.overlay_margin_x", margin_x)
        self._cfg.set("ui.overlay_margin_y", margin_y)
        self._show_pill_placement(*placement)        # show what was really saved
        self._pill_placement_changed = False         # that was us, not the user

    def _chosen_language_profiles(self) -> dict[str, str]:
        """The map as this dialog would save it: what the file says, with the rows
        the table actually shows overlaid.

        The table has a row per general.languages entry only, so a mapping for a
        language it cannot show - `de`, or `auto`, which the Language combo does
        offer - is carried through untouched instead of being dropped by a save
        that had nothing to do with it.
        """
        # Only entries that still name a real profile: a dangling one for a
        # language with no row is a config error errors() rejects, and no row
        # means no way to clear it - it would block every save from here.
        profiles = self._cfg.get("stt.profiles", {}) or {}
        mapping = {code: name for code, name in self._cfg.language_profiles().items()
                   if name in profiles}
        for code, combo in self.language_profile_combos.items():
            chosen = combo.currentData()
            if chosen:
                mapping[code.strip().lower()] = chosen
            else:
                mapping.pop(code.strip().lower(), None)     # "(keep current)"
        return mapping

    def _save_language_profiles(self) -> None:
        """Write the map, then apply it when the language was picked here.

        Key by key, and only the keys that actually moved: replacing the whole
        table costs every comment inside it, including the line that explains
        what the table is for. An owner who changes nothing here keeps the file
        they had, byte for byte.
        """
        mapping = self._chosen_language_profiles()
        current = self._cfg.get("general.language_profiles") or {}
        for code in current:
            if code not in mapping:
                self._cfg.unset(f"general.language_profiles.{code}")
        for code, name in mapping.items():
            if current.get(code) != name:
                self._cfg.set(f"general.language_profiles.{code}", name)
        if not self._language_changed or self._active_changed:
            # Nothing to follow (the language came from the file or elsewhere),
            # or "Use this profile" was pressed and that choice wins.
            return
        name = mapping.get(str(self.language_combo.currentData()).strip().lower())
        if not name or name not in (self._cfg.get("stt.profiles", {}) or {}):
            return
        self._cfg.set("stt.active", name)
        self.active_label.setText(f"Active profile: {name}")
        # This dialog decided the active profile, so the on-disk value must not
        # be carried back over it in _carry_over_external_edits.
        self._active_changed = True

    def _save(self) -> None:
        c = self._cfg
        for name, edit in self.key_edits.items():
            try:
                parse_keyspec(edit.text())
            except ValueError as exc:
                # Named for what the key does: "hotkeys.recall" is a line in a
                # file the user is not reading, and this is the one message
                # that has to tell them which row to go and fix.
                self.error_label.setText(f"{ACTION_LABELS[name]}: {exc}")
                return
            c.set(f"hotkeys.{name}", edit.text().strip())
        if self.portal_edits and not self.portal_edits["dictate"].text().strip():
            # Config.errors() refuses this too, in words about a setting name.
            self.error_label.setText(f"{ACTION_LABELS['dictate']} needs a shortcut.")
            return
        for name, edit in self.portal_edits.items():
            c.set(f"hotkeys.portal_{name}", edit.text().strip())
        c.set("general.language", self.language_combo.currentData())
        c.set("general.notifications", bool(self.notifications_combo.currentData()))
        c.set("inject.mode", self.inject_mode_combo.currentData())
        c.set("inject.paste_with", self.paste_with_combo.currentData())
        c.set("hotkeys.dictate_mode", self.mode_combo.currentData())
        c.set("audio.device", self.device_combo.currentData() or "")
        c.set("audio.max_seconds", self.max_seconds.value())
        c.set("stt.model_dir", self.model_dir_edit.text().strip())
        position, margin_x, margin_y = self._chosen_pill_placement()
        c.set("ui.overlay_position", position)
        c.set("ui.overlay_margin_x", margin_x)
        c.set("ui.overlay_margin_y", margin_y)
        if not self._commit_profile_form():
            return
        rules = []
        for r in range(self.replacements_table.rowCount()):
            cells = [self.replacements_table.item(r, col) for col in range(3)]
            src, dst, flags = [(x.text() if x else "").strip() for x in cells]
            if src:
                rules.append([src, dst, flags] if flags else [src, dst])
        c.set("dictionary.replacements", rules)
        c.set("dictionary.hotwords", [word.strip() for word in self.hotwords_edit.text().split(",")
                                      if word.strip()])
        self._save_language_profiles()
        self._update_profile_mode()
        # The pairing may have moved the active profile; the list beside the
        # caption has to say so too.
        self._load_profile_list()
        if not self._carry_over_external_edits():
            return
        errs = c.errors()
        if errs:
            self.error_label.setText("; ".join(errs))
            return
        c.save()
        self.error_label.setText("")
        # After the file, and only after it: the desktop's store is the other
        # half of a portal trigger, and a rebind to a key the config does not
        # hold is a key that disappears on the next reload.
        rebind = self._apply_desktop_shortcuts()
        # Save leaves the window open, so the "the user decided this here" flags
        # must not outlive the save they belong to: a second language switch has
        # to move its profile too, and an external switch made after this save
        # still has to be carried over by the next one.
        self._active_changed = False
        self._language_changed = False
        self._inject_mode_changed = False
        self._pill_placement_changed = False
        if rebind:
            # Before `saved`: the daemon reloads and rebinds in one pass, so the
            # desktop's permission dialog cannot be asked for twice.
            self.shortcuts_rebound.emit()
        self.saved.emit()
