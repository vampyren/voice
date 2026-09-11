"""The Arch package: what `makepkg` will read, and what it will install where.

`makepkg` cannot run here (this is not an Arch machine), so these tests hold the
PKGBUILD to the shape makepkg expects and to the paths the *code* depends on:
the desktop entry the portal resolves our app id through, the directory
`voice.ui.overlay_client.repo_root()` derives from the installed package, and
the interpreter the pill helper is probed on.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"
PKGBUILD = PKG / "PKGBUILD"
SCRIPTLET = PKG / "voice.install"
WRAPPER = PKG / "voice.wrapper"
OVERLAY_WRAPPER = PKG / "voice-overlay.wrapper"

#: Where the package puts the two halves of the Python install. `app` holds the
#: `voice` package and nothing else, which is what keeps repo_root() honest.
APP_DIR = "/usr/lib/voice/app"
DEPS_DIR = "/usr/lib/voice/deps"


def code(path: Path) -> str:
    """`path` without its comment lines: what it actually runs."""
    return "\n".join(l for l in path.read_text().splitlines()
                      if not l.lstrip().startswith("#"))


def run_wrapper(prefix: str, show: str) -> str:
    """The wrapper with its final exec replaced by an echo of `show`, run.

    The only way to know what a shell wrapper exports is to run it; reading it
    missed that an unmatched `cuda/nvidia/*/lib` glob under `set -e` killed the
    command before it ever reached python.
    """
    import tempfile
    body = re.sub(r"(?m)^exec .*$", f'printf "%s" "${{{show}:-unset}}"',
                  WRAPPER.read_text()).replace("prefix=/usr/lib/voice", f"prefix={prefix}")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "voice.sh"
        script.write_text(body)
        done = subprocess.run(["sh", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def scriptlet_code() -> str:
    """The scriptlet with its message heredocs removed: what it actually runs."""
    out, skipping = [], False
    for line in SCRIPTLET.read_text().splitlines():
        if skipping:
            skipping = line.strip() != "MSG"
            continue
        if "<<'MSG'" in line:
            skipping = True
            continue
        out.append(line)
    return "\n".join(out)


def shell_vars() -> dict:
    """Source the PKGBUILD the way makepkg does and read back what it defined."""
    script = (
        'startdir=/tmp/voice-build/packaging; source "$1" >/dev/null;'
        'declare -p pkgbase pkgname pkgver pkgrel arch license source makedepends options _prefix _appid _py;'
        'echo "FUNCS: $(declare -F | sed "s/^declare -f //" | tr "\\n" " ")"'
    )
    done = subprocess.run(["bash", "-c", script, "_", str(PKGBUILD)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    out = {}
    for line in done.stdout.splitlines():
        if line.startswith("FUNCS: "):
            out["_funcs"] = line[len("FUNCS: "):].split()
        elif line.startswith("declare -"):
            name, _, value = line.split(" ", 2)[2].partition("=")
            out[name] = value
    return out


@pytest.mark.parametrize("path", [PKGBUILD, SCRIPTLET, WRAPPER, OVERLAY_WRAPPER])
def test_every_shipped_shell_file_parses(path):
    assert path.exists(), f"{path} is missing"
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0


def test_pkgbuild_has_the_fields_makepkg_requires():
    v = shell_vars()
    assert v["pkgbase"] == '"voice"' or "voice" in v["pkgbase"]
    assert "'voice'" in v["pkgname"] or '"voice"' in v["pkgname"]
    assert re.search(r"\d+\.\d+\.\d+", v["pkgver"])
    assert v["pkgrel"].strip("\"'").isdigit()
    assert "x86_64" in v["arch"]
    assert v["license"].strip() not in ("", "''", '""')
    for fn in ("build", "package_voice"):
        assert fn in v["_funcs"], f"PKGBUILD defines no {fn}()"


def test_the_gpu_libraries_are_a_separate_package():
    """CPU transcription must not cost the user the multi-gigabyte CUDA wheels."""
    v = shell_vars()
    assert "voice-cuda" in v["pkgname"]
    assert "package_voice-cuda" in v["_funcs"]


def test_the_desktop_entry_is_installed_under_the_app_id():
    """The portal resolves our app id through this file name; anything else and
    GlobalShortcuts refuses the session with "An app id is required"."""
    from voice import APP_ID
    assert shell_vars()["_appid"].strip("\"'") == APP_ID
    text = PKGBUILD.read_text()
    assert '/usr/share/applications/$_appid.desktop' in text
    assert '/etc/xdg/autostart/$_appid.desktop' in text
    assert (PKG / "voice.desktop").read_text().startswith("[Desktop Entry]")


def test_the_udev_rule_ships_under_usr_lib_and_matches_the_file_in_the_repo():
    text = PKGBUILD.read_text()
    rule = "70-voice-input.rules"
    assert (PKG / rule).exists()
    assert f"/usr/lib/udev/rules.d/{rule}" in text
    assert "/etc/udev/rules.d" not in text, "/etc is the admin's, not the package's"


def test_nothing_is_written_into_a_user_home_at_package_time():
    """A package owns /usr, never the user's home - and a scriptlet that
    deletes config or a multi-gigabyte model cache is exactly the removal
    behaviour a package must not have."""
    text = code(PKGBUILD)
    assert "$HOME" not in text and "~/." not in text
    for line in scriptlet_code().splitlines():
        assert not re.match(r"\s*(rm|rmdir|find)\s", line), f"scriptlet runs: {line!r}"
        assert "$HOME" not in line and "~/." not in line, f"scriptlet touches a home: {line!r}"


def test_the_dependency_set_comes_from_the_lock_file():
    """A build that re-resolves is not the tested dependency set."""
    text = PKGBUILD.read_text()
    assert "uv export" in text and "--frozen" in text
    assert (ROOT / "uv.lock").exists()


def test_the_wrapper_runs_the_projects_console_script_entry_point():
    import tomlkit
    pyproject = tomlkit.parse((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["scripts"]["voice"] == "voice.cli:main"
    wrapper = WRAPPER.read_text()
    assert "voice.cli" in wrapper
    assert "python3.12" in wrapper, "the app runs on the interpreter the lock resolves for"
    assert " -P " in wrapper, "-P keeps a stray ./voice directory off sys.path"


def test_the_wrapper_puts_the_package_where_repo_root_expects_it():
    """`overlay_client.repo_root()` is parents[2] of overlay_client.py, and the
    daemon hands it to the pill helper as PYTHONPATH. With the wheel installed
    into APP_DIR that resolves to APP_DIR - which therefore has to hold the
    `voice` package directly, and nothing built for the wrong interpreter."""
    from voice.ui.overlay_client import repo_root
    assert (repo_root() / "voice" / "ui" / "overlay_client.py").exists()
    v = shell_vars()
    assert v["_prefix"].strip("\"'") == "/usr/lib/voice"
    text = PKGBUILD.read_text()
    assert 'cp -a "$srcdir/app" "$pkgdir$_prefix/app"' in text
    assert 'cp -a "$srcdir/deps" "$pkgdir$_prefix/deps"' in text
    assert '--target "$srcdir/app" --no-deps' in text, "the app dir holds the wheel alone"
    assert run_wrapper("/usr/lib/voice", "PYTHONPATH") == f"{APP_DIR}:{DEPS_DIR}"


def test_the_wrapper_puts_the_cuda_runtime_on_the_loader_path(tmp_path):
    """CTranslate2 dlopens libcublas.so.12 and nothing else sets this up; with
    no voice-cuda installed the variable must be left alone, not emptied."""
    (tmp_path / "cuda" / "nvidia" / "cublas" / "lib").mkdir(parents=True)
    assert run_wrapper(str(tmp_path), "LD_LIBRARY_PATH") == \
        str(tmp_path / "cuda" / "nvidia" / "cublas" / "lib")
    assert run_wrapper(str(tmp_path / "absent"), "LD_LIBRARY_PATH") == "unset"


def test_the_wrapper_survives_a_cuda_directory_with_no_libraries(tmp_path):
    """`set -e` plus an unmatched glob used to exit before reaching python."""
    (tmp_path / "cuda" / "nvidia").mkdir(parents=True)
    assert run_wrapper(str(tmp_path), "PYTHONPATH").endswith("/deps")


def test_the_overlay_wrapper_uses_the_system_interpreter():
    """The pill needs PyGObject and pycairo, which are distro packages; the
    app's own 3.12 has neither, and `probe_helper()` tries this path first."""
    from voice.ui.overlay_client import SYSTEM_PYTHON
    body = code(OVERLAY_WRAPPER)
    assert SYSTEM_PYTHON in body
    assert "voice.ui.overlay" in body
    assert APP_DIR in body
    assert DEPS_DIR not in body, "the helper must not see the 3.12 extension modules"


def test_the_scriptlet_reloads_udev_and_points_the_user_at_the_next_step():
    text = SCRIPTLET.read_text()
    for fn in ("post_install", "post_upgrade", "post_remove"):
        assert f"{fn}()" in text
    assert text.count("udevadm") >= 2, "reload on install and on removal"
    assert "voice doctor" in text
    assert "Keyboard" in text, "say where the shortcut is assigned"


def test_the_scriptlet_says_what_pacman_leaves_behind():
    text = SCRIPTLET.read_text()
    for leftover in (".config/voice", ".local/state/voice", ".cache/huggingface"):
        assert leftover in text


@pytest.mark.skipif(not os.path.exists("/usr/bin/bash"), reason="no bash")
def test_the_cuda_package_depends_on_the_base_package():
    text = PKGBUILD.read_text()
    body = text[text.index("package_voice-cuda"):]
    assert "depends=" in body and "voice=" in body


def test_every_file_the_package_installs_exists_in_the_repository():
    """`install -Dm644 packaging/typo.desktop` fails at package time, on the
    owner's machine, minutes into a multi-gigabyte build."""
    text = PKGBUILD.read_text()
    sources = re.findall(r'^\s*install -Dm[0-7]{3} (\S+) ', text, re.M)
    assert len(sources) >= 5, sources
    for rel in sources:
        assert (ROOT / rel).exists(), f"PKGBUILD installs a missing file: {rel}"


def test_the_license_text_the_package_ships_is_not_empty():
    """There is no LICENSE file upstream; the package carves the statement that
    stands in for one out of the README, so that section has to stay there."""
    text = subprocess.run(["sed", "-n", "/^## License/,$p", str(ROOT / "README.md")],
                          capture_output=True, text=True).stdout
    assert "## License" in text and len(text.split()) > 5


def _stage(tmp_path: Path) -> Path:
    """Run the PKGBUILD's package functions against a stub $srcdir and return
    the $pkgdir they filled, which is exactly what pacman would ship.

    makepkg cannot run here, but the package functions are plain bash and the
    file layout they produce is the thing most worth checking: it is where a
    wrong path, a missing file or a group-writable mode would land.
    """
    src, pkg = tmp_path / "src", tmp_path / "pkg"
    for sub in ("app/voice/ui", "deps/numpy", "cuda/nvidia/cublas/lib"):
        (src / sub).mkdir(parents=True)
    (src / "voice").symlink_to(ROOT)
    (src / "app/voice/__init__.py").write_text("__version__ = '0.0.0'\n")
    (src / "app/voice/ui/__init__.py").write_text("x = 1\n")
    (src / "deps/numpy/__init__.py").write_text("y = 2\n")
    (src / "cuda/nvidia/cublas/lib/libcublas.so.12").write_bytes(b"")
    pkg.mkdir()
    script = (
        'umask 0022;'                       # makepkg sets this before it builds
        f'startdir={PKG}; source "{PKGBUILD}";'
        f'export srcdir="{src}" pkgdir="{pkg}";'
        # python3.12 is the *target* interpreter and need not exist on the
        # machine writing the package; only compileall uses it here.
        '_py=$(command -v python3.12 || command -v python3);'
        'package_voice; package_voice-cuda'
    )
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return pkg


def test_the_package_functions_produce_the_documented_layout(tmp_path):
    pkg = _stage(tmp_path)
    from voice import APP_ID
    for rel in (
        "usr/bin/voice",
        "usr/bin/voice-overlay",
        f"usr/share/applications/{APP_ID}.desktop",
        f"etc/xdg/autostart/{APP_ID}.desktop",
        "usr/lib/udev/rules.d/70-voice-input.rules",
        "usr/share/doc/voice/README.md",
        "usr/share/doc/voice/packaging.md",
        "usr/share/licenses/voice/LICENSE",
        "usr/lib/voice/app/voice/__init__.py",
        "usr/lib/voice/deps/numpy/__init__.py",
        "usr/lib/voice/cuda/nvidia/cublas/lib/libcublas.so.12",
    ):
        assert (pkg / rel).exists(), f"the package does not ship {rel}"
    assert (pkg / "usr/share/licenses/voice/LICENSE").read_text().startswith("## License")
    # Nothing outside the four trees a package is allowed to own.
    tops = {p.name for p in pkg.iterdir()}
    assert tops <= {"usr", "etc"}, tops


def test_the_packaged_files_are_not_group_writable(tmp_path):
    """`cp -a` keeps the build user's umask; pacman would ship 0664 files."""
    pkg = _stage(tmp_path)
    bad = [str(p.relative_to(pkg)) for p in pkg.rglob("*") if p.stat().st_mode & 0o022]
    assert not bad, f"group/other-writable in the package: {bad[:5]}"


def test_the_app_directory_holds_only_this_project(tmp_path):
    """repo_root() hands this directory to the pill helper on the *system*
    interpreter; a third-party module built for 3.12 must not be in it."""
    pkg = _stage(tmp_path)
    assert {p.name for p in (pkg / "usr/lib/voice/app").iterdir()} == {"voice"}
