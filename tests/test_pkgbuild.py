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

#: The Python that Arch's `python` package is, checked against
#: https://archlinux.org/packages/search/json/?name=python on 2026-09-11
#: (core/python 3.14.7). The package installs the locked dependency set on this
#: interpreter and runs it there, so the project has to support it.
ARCH_SYSTEM_PYTHON = (3, 14)

#: Every package name the PKGBUILD is allowed to declare, each one checked on
#: 2026-09-11 against https://archlinux.org/packages/search/json/?name=<pkg>
#: and found in core/ or extra/.
#:
#: `makepkg -s` installs dependencies from the configured repositories and from
#: nowhere else - it does not fetch from the AUR. A name that is not here is
#: either a typo or an AUR package, and both fail the very first `makepkg -si`
#: on a clean machine with an error the owner cannot act on. Adding a
#: dependency means re-running that query and adding the name here.
OFFICIAL_REPO_PACKAGES = {
    "python", "pipewire", "pipewire-audio", "wl-clipboard", "libnotify",
    "xdg-desktop-portal", "python-gobject", "gtk4", "python-cairo",
    "gtk4-layer-shell", "nvidia-utils", "wtype", "ydotool",
    "ttf-jetbrains-mono", "dconf", "git", "uv",
}

#: Not packages but virtual names that official packages satisfy through
#: `provides`, so a name search returns nothing while pacman resolves them
#: fine: xdg-desktop-portal-kde, -gnome, -gtk, -wlr and eight others provide
#: this one (checked the same day).
OFFICIAL_VIRTUAL_PROVIDES = {"xdg-desktop-portal-impl"}

#: Interpreter packages that have only ever existed in the AUR. `python312` was
#: a hard dependency until this package was retargeted at Arch's own `python`;
#: naming any of them again reintroduces the failure described above.
AUR_ONLY_INTERPRETERS = {"python311", "python312", "python313"}


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


def shell_vars(env: dict | None = None) -> dict:
    """Source the PKGBUILD the way makepkg does and read back what it defined.

    `env` adds environment variables. That is how the CPU-only build option is
    selected - `VOICE_GPU=0 makepkg -si` - because makepkg sources the PKGBUILD
    in a shell that inherits the caller's environment, exactly as this does.
    """
    script = (
        'startdir=/tmp/voice-build/packaging; source "$1" >/dev/null;'
        'declare -p pkgname pkgver pkgrel arch license source makedepends options'
        ' depends optdepends install _prefix _appid _py _gpu _branch;'
        'echo "FUNCS: $(declare -F | sed "s/^declare -f //" | tr "\\n" " ")"'
    )
    done = subprocess.run(["bash", "-c", script, "_", str(PKGBUILD)],
                          capture_output=True, text=True,
                          env={**os.environ, **(env or {})})
    assert done.returncode == 0, done.stderr
    out = {}
    for line in done.stdout.splitlines():
        if line.startswith("FUNCS: "):
            out["_funcs"] = line[len("FUNCS: "):].split()
        elif line.startswith("declare -"):
            name, _, value = line.split(" ", 2)[2].partition("=")
            out[name] = value
    return out


def shell_array(name: str, env: dict | None = None) -> list[str]:
    """One array from the sourced PKGBUILD, element by element."""
    script = (f'startdir=/tmp/voice-build/packaging; source "$1" >/dev/null;'
              f'printf "%s\\n" "${{{name}[@]}}"')
    done = subprocess.run(["bash", "-c", script, "_", str(PKGBUILD)],
                          capture_output=True, text=True,
                          env={**os.environ, **(env or {})})
    assert done.returncode == 0, done.stderr
    return [line for line in done.stdout.splitlines() if line]


@pytest.mark.parametrize("path", [PKGBUILD, SCRIPTLET, WRAPPER, OVERLAY_WRAPPER])
def test_every_shipped_shell_file_parses(path):
    assert path.exists(), f"{path} is missing"
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0


def test_pkgbuild_has_the_fields_makepkg_requires():
    v = shell_vars()
    assert v["pkgname"].strip("\"'") == "voice"
    assert re.search(r"\d+\.\d+\.\d+", v["pkgver"])
    assert v["pkgrel"].strip("\"'").isdigit()
    assert "x86_64" in v["arch"]
    assert v["license"].strip() not in ("", "''", '""')
    for fn in ("build", "package"):
        assert fn in v["_funcs"], f"PKGBUILD defines no {fn}()"


def test_the_gpu_runtime_is_not_an_optional_extra():
    """The owner's decision: `makepkg -si` produces a GPU-ready install.

    Nothing about the GPU may be phrased as something to add afterwards - not a
    second package to install, not an optdepend. `nvidia-utils` (which owns
    /usr/lib/libcuda.so.1) is a hard dependency, and the CUDA 12 wheels the
    CTranslate2 build dlopens ship inside this package.
    """
    assert shell_array("pkgname") == ["voice"], "one package, not a split build"
    depends = shell_array("depends")
    assert "nvidia-utils" in depends, f"libcuda.so.1 is not a dependency: {depends}"
    for entry in shell_array("optdepends"):
        low = entry.lower()
        assert "cuda" not in low and "gpu" not in low and "nvidia" not in low, \
            f"the GPU is still offered as optional: {entry!r}"


def test_the_cuda_runtime_ships_inside_the_package(tmp_path):
    """Not a `voice-cuda` to install afterwards: the wheels are in `voice`."""
    text = PKGBUILD.read_text()
    assert "package_voice-cuda" not in text
    pkg = _stage(tmp_path)
    assert (pkg / "usr/lib/voice/cuda/nvidia/cublas/lib/libcublas.so.12").exists()


def test_the_cpu_only_build_is_available_and_is_not_the_default(tmp_path):
    """The escape hatch for a machine with no NVIDIA card - one build variable,
    the same package name, and off unless it is asked for."""
    assert shell_vars()["_gpu"].strip("\"'") == "1", "the default build is GPU-ready"
    cpu = {"VOICE_GPU": "0"}
    assert shell_vars(cpu)["_gpu"].strip("\"'") == "0"
    assert "nvidia-utils" not in shell_array("depends", cpu)
    assert shell_array("pkgname", cpu) == ["voice"], "not a second product"
    pkg = _stage(tmp_path, env=cpu)
    assert not (pkg / "usr/lib/voice/cuda").exists(), \
        "a CPU-only build must not carry the CUDA wheels"
    assert (pkg / "usr/lib/voice/app/voice/__init__.py").exists(), "everything else is identical"


def test_the_packaged_wrapper_and_the_development_launcher_agree_on_the_gpu_libs():
    """install.sh grew this in f41213c; the package had it first. The two routes
    must expose the wheels the same way or the GPU works on only one of them."""
    assert "nvidia/*/lib" in WRAPPER.read_text()
    assert "nvidia/*/lib" in (ROOT / "install.sh").read_text()
    assert "LD_LIBRARY_PATH" in (ROOT / "install.sh").read_text()


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


def _declared_dependency_names(env: dict | None = None) -> set[str]:
    """Every package name the PKGBUILD declares as a dependency of any kind."""
    names = set()
    for var in ("depends", "makedepends", "optdepends"):
        for entry in shell_array(var, env):
            name = entry.split(":", 1)[0].strip()        # optdepends: "pkg: why"
            name = re.split(r"[<>=]", name, 1)[0].strip()  # any version pin
            if name:
                names.add(name)
    return names


def test_no_dependency_comes_from_the_aur():
    """`makepkg -s` does not fetch from the AUR.

    The package used to depend on `python312`, which exists only there, so the
    first `makepkg -si` on a clean machine died on a missing dependency that
    the owner could not act on without reading the error. Checked for the GPU
    build and the CPU-only one, since they declare different `depends`.
    """
    known = OFFICIAL_REPO_PACKAGES | OFFICIAL_VIRTUAL_PROVIDES
    for env in (None, {"VOICE_GPU": "0"}):
        declared = _declared_dependency_names(env)
        assert declared, "no dependencies were read out of the PKGBUILD at all"
        for name in sorted(declared):
            assert name not in AUR_ONLY_INTERPRETERS, \
                f"{name} is AUR-only; `makepkg -s` cannot install it"
            assert name in known, (
                f"{name!r} is not in the list of names checked against the "
                f"official repositories. If it really is in core/extra add it "
                f"to OFFICIAL_REPO_PACKAGES; if it is an AUR package it cannot "
                f"be a dependency.")


def test_the_package_depends_on_arch_s_own_python_pinned_to_its_abi():
    """There is no pinned-interpreter alternative that avoids the AUR: none of
    python311, python312 or python313 is in the official repositories.

    So the dependency is Arch's `python` - pinned to the minor version the
    bundled wheels were built for. Unpinned it was a silent breakage waiting
    for the next Arch python bump: the native modules stop importing, voice
    will not start, and pacman says nothing because `python` is still
    satisfied. Pinned, the upgrade stops and names voice as the reason.
    """
    for env in (None, {"VOICE_GPU": "0"}):
        entries = [d for d in shell_array("depends", env)
                   if re.split(r"[<>=]", d, 1)[0].strip() == "python"]
        assert entries, "the package must depend on Arch's own python"
        joined = " ".join(entries)
        assert ">=" in joined and "<" in joined, (
            f"python has to be pinned to the minor version the bundled wheels "
            f"were built for, or an Arch upgrade breaks voice silently: {entries}")


def test_the_install_message_never_claims_a_cuda_runtime_it_may_not_have():
    """The scriptlet ships in both builds, and said CUDA was bundled in both.

    The release download is the CPU build, so every installer was told their
    GPU would be used. What differs between the builds has to be decided from
    what actually landed on disk.
    """
    text = (ROOT / "packaging" / "voice.install").read_text()
    guard = text.find("[ -d /usr/lib/voice/cuda ]")
    assert guard != -1, "nothing checks whether the CUDA runtime is actually installed"
    claim = text.find("bundles the CUDA 12 runtime")
    otherwise = text.find("else", guard)
    assert guard < claim < otherwise, (
        "the CUDA claim has to sit inside the branch that checked for it")
    assert "CPU-only build" in text, "the other branch has to say what this build is"


def test_the_project_supports_the_interpreter_arch_will_run_it_on():
    """`requires-python` has to admit Arch's `python`, because that is what the
    wheels are resolved and built for and what /usr/bin/voice execs. It must
    still admit 3.12, which costs nothing - the lock resolves the same
    versions either way."""
    import tomlkit
    spec = tomlkit.parse((ROOT / "pyproject.toml").read_text())
    spec = str(spec["project"]["requires-python"])
    low = tuple(int(n) for n in re.search(r">=\s*(\d+)\.(\d+)", spec).groups())
    high = tuple(int(n) for n in re.search(r"<\s*(\d+)\.(\d+)", spec).groups())
    assert low <= ARCH_SYSTEM_PYTHON < high, \
        f"requires-python {spec!r} excludes Arch's python " \
        f"{'.'.join(str(n) for n in ARCH_SYSTEM_PYTHON)}"
    assert low <= (3, 12), "3.12 support is free to keep; do not drop it"


def test_the_wrapper_runs_the_very_interpreter_the_package_builds_against():
    """The wheels under deps/ are CPython extension modules built for `_py`.
    If /usr/bin/voice execs anything else they are the wrong ABI and every
    import of numpy or ctranslate2 fails on first use - at runtime, on the
    owner's machine, long after a build that reported success."""
    py = shell_vars()["_py"].strip("\"'")
    assert py == "/usr/bin/python3", \
        f"_py is {py!r}: the package builds against Arch's `python`"
    assert re.search(rf"(?m)^exec {re.escape(py)}\s", code(WRAPPER)), \
        f"the wrapper does not exec {py}"


def test_no_shipped_code_names_a_pinned_interpreter():
    """Comments and prose may explain the history; the code must not run it."""
    for path in (PKGBUILD, WRAPPER, OVERLAY_WRAPPER):
        body = code(path)
        assert "python3.12" not in body, f"{path.name} still runs python3.12"
        for name in AUR_ONLY_INTERPRETERS:
            assert name not in body, f"{path.name} still declares {name}"
    assert "python3.12" not in scriptlet_code()


def test_the_wrapper_runs_the_projects_console_script_entry_point():
    import tomlkit
    pyproject = tomlkit.parse((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["scripts"]["voice"] == "voice.cli:main"
    wrapper = WRAPPER.read_text()
    assert "voice.cli" in wrapper
    assert "/usr/bin/python3" in wrapper, "the app runs on Arch's own interpreter"
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
    """The pill needs PyGObject and pycairo, which are distro packages, and
    `probe_helper()` tries this path first. The app runs on that same
    interpreter now, so what this pins is the *path*: the helper gets `app`
    and not `deps`, whose gigabytes it has no use for."""
    from voice.ui.overlay_client import SYSTEM_PYTHON
    body = code(OVERLAY_WRAPPER)
    assert SYSTEM_PYTHON in body
    assert "voice.ui.overlay" in body
    assert APP_DIR in body
    assert DEPS_DIR not in body, "the pill has no use for the bundled wheels"


def test_the_scriptlet_reloads_udev_and_points_the_user_at_the_next_step():
    text = SCRIPTLET.read_text()
    for fn in ("post_install", "post_upgrade", "post_remove"):
        assert f"{fn}()" in text
    assert text.count("udevadm") >= 2, "reload on install and on removal"
    assert "voice doctor" in text
    assert "Keyboard" in text, "say where the shortcut is assigned"


def test_no_shipped_text_tells_the_user_to_install_the_gpu_separately():
    """The scriptlet used to end with "unless the voice-cuda package is
    installed as well". With the GPU in the package that sentence is a lie.

    packaging/README.md is exempt from the name check and only from it: it is
    the design note, and it has to be able to say which shape was rejected.
    """
    for path in (SCRIPTLET, ROOT / "README.md"):
        assert "voice-cuda" not in path.read_text(), \
            f"{path.name} still points the user at a separate GPU package"
    assert "GPU" in SCRIPTLET.read_text(), "say that the GPU runtime is included"
    assert "not optional" in (PKG / "README.md").read_text().lower(), \
        "the packaging note must record that the GPU stack is not an extra"


def test_both_readmes_document_the_cpu_only_build():
    """The escape hatch is useless if nobody without a 4090 can find it."""
    for path in (PKG / "README.md", ROOT / "README.md"):
        assert "VOICE_GPU=0" in path.read_text(), f"{path} does not document it"


def test_the_scriptlet_says_what_pacman_leaves_behind():
    text = SCRIPTLET.read_text()
    for leftover in (".config/voice", ".local/state/voice", ".cache/huggingface"):
        assert leftover in text


def test_the_package_is_built_from_the_release_branch():
    """`makepkg -si` clones a branch of this repository, by name.

    It named a feature branch for nine commits after that branch merged, so the
    documented install produced a build missing every one of them - silently,
    because the package built and installed perfectly.
    """
    branch = shell_vars()["_branch"].strip('"')
    assert branch == "main", (
        f"the package is built from {branch!r}. Releases are cut from main, so "
        f"anyone running `makepkg -si` is getting whatever that branch last had.")


def test_every_file_the_package_installs_exists_in_the_repository():
    """`install -Dm644 packaging/typo.desktop` fails at package time, on the
    owner's machine, minutes into a multi-gigabyte build."""
    text = PKGBUILD.read_text()
    sources: list[str] = []
    for line in text.splitlines():
        found = re.match(r"\s*install -Dm[0-7]{3} (.*)$", line)
        if not found:
            continue
        args = found.group(1).split()
        if args[:1] == ["-t"]:
            sources.extend(args[2:])         # install -Dm644 -t DIR src...
        elif args:
            sources.append(args[0])          # install -Dm644 src dst
    assert len(sources) >= 5, sources
    for rel in sources:
        rel = rel.strip('"')
        if "$" in rel:                       # a destination, not a source
            continue
        hits = sorted(ROOT.glob(rel)) if any(c in rel for c in "*?[") else [ROOT / rel]
        assert hits, f"PKGBUILD installs a pattern that matches nothing: {rel}"
        for hit in hits:
            assert hit.exists(), f"PKGBUILD installs a missing file: {rel}"


def test_the_license_text_the_package_ships_is_not_empty():
    """There is no LICENSE file upstream; the package carves the statement that
    stands in for one out of the README, so that section has to stay there."""
    text = subprocess.run(["sed", "-n", "/^## License/,$p", str(ROOT / "README.md")],
                          capture_output=True, text=True).stdout
    assert "## License" in text and len(text.split()) > 5


def _stage(tmp_path: Path, env: dict | None = None) -> Path:
    """Run the PKGBUILD's package() against a stub $srcdir and return the
    $pkgdir it filled, which is exactly what pacman would ship.

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
        # No _py override: the PKGBUILD names /usr/bin/python3, which is the
        # interpreter this staging run should actually use for compileall.
        'package'
    )
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})
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
        "usr/share/doc/voice/packaging/README.md",
        "usr/share/doc/voice/docs/install.md",
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


def test_the_package_ships_every_page_the_readme_links_to(tmp_path):
    """The README is a landing page whose links are relative.

    The package shipped README.md alone, so on an installed machine every one
    of them pointed at nothing - and `docs/install.md` tells the owner to delete
    the clone afterwards, leaving no configuration or troubleshooting reference
    at all.
    """
    pkg = _stage(tmp_path) / "usr/share/doc/voice"
    readme = (ROOT / "README.md").read_text()
    linked = {t for t, _ in re.findall(r"\]\(([^)#\s]+)(?:#([^)\s]*))?\)", readme)
              if not t.startswith(("http://", "https://", "mailto:"))}
    assert linked, "no relative links were read out of the README at all"
    missing = sorted(rel for rel in linked if not (pkg / rel).exists())
    assert not missing, f"the README links these and the package does not ship them: {missing}"


def test_the_packaged_files_are_not_group_writable(tmp_path):
    """`cp -a` keeps the build user's umask; pacman would ship 0664 files."""
    pkg = _stage(tmp_path)
    bad = [str(p.relative_to(pkg)) for p in pkg.rglob("*") if p.stat().st_mode & 0o022]
    assert not bad, f"group/other-writable in the package: {bad[:5]}"


def test_the_app_directory_holds_only_this_project(tmp_path):
    """repo_root() hands this directory to the pill helper; it must hold this
    project and nothing else, or the helper picks up bundled wheels."""
    pkg = _stage(tmp_path)
    assert {p.name for p in (pkg / "usr/lib/voice/app").iterdir()} == {"voice"}


def test_the_readme_download_url_matches_the_package_it_would_build():
    """The README's one-command install names an exact release asset.

    A pkgrel bump that leaves the URL behind gives everyone the previous build -
    which is how the CPU release came to tell people their GPU was in use.
    """
    readme = (ROOT / "README.md").read_text()
    urls = re.findall(r"releases/download/[^/]+/(voice-[\d.]+-\d+-\w+\.pkg\.tar\.zst)", readme)
    assert urls, "the README no longer links a release asset to install from"
    var = shell_vars()
    pkgver, pkgrel = var["pkgver"].strip('"'), var["pkgrel"].strip('"')
    expected = f"voice-{pkgver}-{pkgrel}-x86_64.pkg.tar.zst"
    for name in urls:
        assert name == expected, (
            f"the README installs {name!r} but this PKGBUILD builds {expected!r}")
