"""The documentation has to keep agreeing with the program and with itself.

Nine code reviews on this branch found nothing here, because every one of them
was pointed at the code diff. Three pages told the owner to click buttons that
do not exist, the doctor checklist had drifted three checks behind, and the
packaged install shipped a front page of dead links. None of it was hard to
find - nothing was looking. These tests look.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")),
         ROOT / "packaging" / "README.md"]

#: The README is a landing page: what it is, what it puts on your machine, how
#: to install it, how to use it. It reached 958 lines once - requirements, every
#: config key, troubleshooting and the roadmap in one scroll - and nobody could
#: find the install command.
#:
#: This is a shape check, not a byte budget. Raise it when a section genuinely
#: belongs on the front page, as "what the package installs" did; do not raise
#: it to make room for detail that has a page of its own.
LANDING_PAGE_MAX_LINES = 240


def _slugs(text: str) -> set[str]:
    """GitHub's heading anchors for one page."""
    out = set()
    for found in re.finditer(r"^#{1,6}\s+(.*)$", text, re.M):
        title = re.sub(r"[^\w\s-]", "", found.group(1).strip().lower())
        out.add(re.sub(r"\s+", "-", title))
    return out


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_link_between_pages_resolves(page):
    """A rename used to break these silently; the split made ~30 of them."""
    text = page.read_text()
    broken = []
    for target, anchor in re.findall(r"\]\(([^)#\s]*)(?:#([^)\s]*))?\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        dest = page if not target else (page.parent / target)
        if not dest.exists():
            broken.append(f"{target} (no such file)")
            continue
        if anchor and anchor not in _slugs(dest.read_text()):
            broken.append(f"{target}#{anchor} (no such heading)")
    assert not broken, f"{page.relative_to(ROOT)} links nowhere: {broken}"


def test_every_doctor_check_is_documented():
    """`voice doctor` is what the README sends people to when something breaks.

    A check it prints and the docs never mention is a line the owner has to
    guess the meaning of.
    """
    from voice.doctor import default_probes

    page = (ROOT / "docs" / "troubleshooting.md").read_text()
    missing = [name for name in default_probes() if f"**{name}**" not in page]
    assert not missing, f"doctor prints these and troubleshooting.md never explains them: {missing}"


def test_optional_doctor_checks_are_marked_optional():
    """A `✘ cuda (optional)` run still exits 0.

    Documenting cuda without the tag told anyone on a CPU machine that a
    passing run had failed.
    """
    from voice.doctor import REQUIRED, default_probes

    page = (ROOT / "docs" / "troubleshooting.md").read_text()
    wrong = []
    for name in default_probes():
        if name in REQUIRED:
            continue
        entry = re.search(rf"^- \*\*{re.escape(name)}\*\*(.*)$", page, re.M)
        if entry and "*(optional)*" not in entry.group(1):
            wrong.append(name)
    assert not wrong, f"these do not affect the exit code but are documented as if they do: {wrong}"


def test_the_readme_stays_a_landing_page():
    lines = len((ROOT / "README.md").read_text().splitlines())
    assert lines <= LANDING_PAGE_MAX_LINES, (
        f"README.md is {lines} lines. It is the front page: what voice is, how to "
        f"install it, how to use it. Detail belongs on a page under docs/ with a "
        f"link from the table at the bottom.")


def test_the_docs_do_not_invent_buttons_the_settings_window_does_not_have():
    """Three pages told the owner to press "Open shortcut settings".

    The button says "Change…". Someone following the one KDE setup step hunted
    for a label that has never existed in the program.
    """
    from voice.ui import settings

    real = {settings.CHANGE, settings.SHORTCUT_SETTINGS_BUTTON, settings.ADVANCED}
    invented = ("Open shortcut settings", "Capture key", "Change in the desktop")
    for page in PAGES:
        text = page.read_text()
        for label in invented:
            assert label not in text, (
                f"{page.relative_to(ROOT)} names a button that does not exist: "
                f"{label!r}. The real ones are {sorted(real)}.")


def test_no_test_shells_out_without_a_bound():
    """One wedged helper must not take the whole suite with it.

    On Ubuntu 26.04 the coreutils these tests call are the Rust `uutils`
    rewrites, and two of them have segfaulted on the development machine.
    Unbounded, such a call hangs until pytest-timeout fires at 60 s and dumps
    every thread's stack - which reads like a crash in the tests, and sent a
    real investigation down the wrong path.
    """
    import ast

    unbounded = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "run"
                    and isinstance(getattr(node.func, "value", None), ast.Name)
                    and node.func.value.id == "subprocess"
                    and "timeout" not in {kw.arg for kw in node.keywords}):
                unbounded.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not unbounded, f"subprocess.run with no timeout=: {unbounded}"
