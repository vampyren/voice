import os
import subprocess
from pathlib import Path

#: A bound on every helper this file runs - `install.sh` shells out, and on
#: Ubuntu 26.04 the coreutils it calls are the Rust `uutils` rewrites, which
#: have segfaulted on this machine. Unbounded, one wedged helper hangs the
#: suite until pytest-timeout fires at 60 s and dumps every thread's stack.
HELPER_TIMEOUT_S = 120

ROOT = Path(__file__).resolve().parents[1]


def run(*args, env=None):
    e = {**os.environ, "DRY_RUN": "1", "HOME": "/tmp/voice-home", **(env or {})}
    return subprocess.run(["bash", str(ROOT / "install.sh"), *args], capture_output=True, text=True, env=e, timeout=HELPER_TIMEOUT_S)


def test_script_parses_and_dry_run_lists_steps():
    assert subprocess.run(["bash", "-n", str(ROOT / "install.sh")], timeout=HELPER_TIMEOUT_S).returncode == 0
    cp = run("--cpu", "--no-udev")
    assert cp.returncode == 0, cp.stderr
    assert "uv sync" in cp.stdout and "--extra gpu" not in cp.stdout
    assert "/tmp/voice-home/.local/bin/voice" in cp.stdout
    assert "autostart" in cp.stdout and "udev" not in cp.stdout.lower().replace("no-udev", "")


def test_gpu_flag_and_udev_step():
    cp = run("--gpu")
    assert "--extra gpu" in cp.stdout and "70-voice-input.rules" in cp.stdout and "udevadm" in cp.stdout


def test_uninstall_lists_removals():
    cp = run("--uninstall")
    assert cp.returncode == 0
    assert "rm" in cp.stdout and "autostart" in cp.stdout and ".config/voice" in cp.stdout


def test_packaging_files_reference_app():
    rules = (ROOT / "packaging" / "70-voice-input.rules").read_text()
    assert 'SUBSYSTEM=="input"' in rules and "uaccess" in rules
    desktop = (ROOT / "packaging" / "voice.desktop").read_text()
    assert "Exec=voice" in desktop and "X-GNOME-Autostart" not in desktop


def test_desktop_entry_is_installed_under_the_app_id():
    """The portal resolves our app id through this file name; anything else and
    GlobalShortcuts refuses the session with "An app id is required"."""
    from voice import APP_ID
    cp = run("--cpu", "--no-udev")
    assert f"/tmp/voice-home/.local/share/applications/{APP_ID}.desktop" in cp.stdout
    assert f"/tmp/voice-home/.config/autostart/{APP_ID}.desktop" in cp.stdout


def test_dry_run_reports_the_legacy_entry_removal():
    """The dry run is the script's contract; a real install deletes these two."""
    cp = run("--cpu", "--no-udev")
    assert "/tmp/voice-home/.local/share/applications/voice.desktop" in cp.stdout
    assert "/tmp/voice-home/.config/autostart/voice.desktop" in cp.stdout


def test_uninstall_removes_the_app_id_entry_and_the_legacy_name():
    from voice import APP_ID
    cp = run("--uninstall")
    assert f"{APP_ID}.desktop" in cp.stdout
    assert "applications/voice.desktop" in cp.stdout      # pre-app-id installs


def _wrapper_body():
    """The heredoc install.sh writes into ~/.local/bin/voice."""
    src = (ROOT / "install.sh").read_text()
    start = src.index('cat > "$BIN" <<EOF')
    body = src[src.index("\n", start) + 1:src.index("\nEOF", start)]
    return body


def test_the_wrapper_exposes_the_gpu_libraries_it_installed():
    """`--extra gpu` puts cuBLAS and cuDNN in the venv; nothing found them.

    CTranslate2 dlopens libcublas.so.12 at runtime, so without the loader path
    the wheels sit there unused and the local backend falls back to CPU int8 on
    a machine with a GPU - silently, because that fallback is by design.
    """
    body = _wrapper_body()
    assert "site-packages/nvidia" in body, "the wrapper must look where the wheels land"
    assert "LD_LIBRARY_PATH" in body
    assert "--no-sync" in body, "and still run the project's own environment"


def test_the_wrapper_survives_a_cpu_only_install(tmp_path):
    """No wheels, no glob match, no empty entry pushed onto the loader path."""
    import subprocess

    body = _wrapper_body()
    body = body[:body.index("exec uv")] + 'echo "LDPATH=[${LD_LIBRARY_PATH:-}]"\n'
    body = body.replace("$ROOT", str(tmp_path))
    out = subprocess.run(["bash", "-c", body], capture_output=True, text=True, check=True, timeout=HELPER_TIMEOUT_S)
    assert out.stdout.strip() == "LDPATH=[]", out.stdout
