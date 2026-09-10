import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, env=None):
    e = {**os.environ, "DRY_RUN": "1", "HOME": "/tmp/voice-home", **(env or {})}
    return subprocess.run(["bash", str(ROOT / "install.sh"), *args], capture_output=True, text=True, env=e)


def test_script_parses_and_dry_run_lists_steps():
    assert subprocess.run(["bash", "-n", str(ROOT / "install.sh")]).returncode == 0
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
