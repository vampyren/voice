# Packaging voice for Arch / CachyOS

`PKGBUILD` builds two packages from this repository:

| package | what it is | rough size |
| --- | --- | --- |
| `voice` | the app, its locked Python dependency set, the CLI, desktop entry, autostart entry and udev rule | ~1.6 GB installed |
| `voice-cuda` | the CUDA 12 runtime (cuBLAS, cuDNN, NVRTC) that turns on GPU transcription | ~2.4 GB installed |

Install `voice` alone and transcription runs on the CPU (int8, with a warning
from `voice doctor`). Add `voice-cuda` and the same install uses the GPU.

## Build and install

```
cd packaging
makepkg -si                       # builds both, installs `voice`
sudo pacman -U voice-cuda-*.pkg.tar.zst   # the GPU half, if you want it
```

`makepkg` needs network access in `build()` (it downloads the locked wheels),
so build with plain `makepkg`, not in a clean chroot with networking off.

## Why the app is not in /usr/lib/python3.14/site-packages

The usual Arch way - install the Python package for the system interpreter and
list every dependency as a `python-*` package - cannot be done here today:

* Arch's `python` is **3.14**; this project pins `requires-python >=3.12,<3.13`
  and `uv.lock` resolves the entire dependency set for CPython 3.12. That
  resolution is what the test suite runs against.
* **onnxruntime**, which `faster-whisper` needs for the Silero VAD
  (`voice/audio/vad.py`), has no package in the official repositories and none
  usable in the AUR (only an out-of-date `onnxruntime-git`).
* **ctranslate2** and **python-faster-whisper** are AUR-only, and the AUR
  `ctranslate2` is built with `WITH_CUDA` commented out ("Only supports up to
  CUDA 12.4"). Depending on it would mean CPU-only transcription on a 4090.
* Arch's `cuda` is **13.x**, so it ships `libcublas.so.13`; the CTranslate2
  wheel dlopens `libcublas.so.12`. The distro CUDA packages cannot serve this
  wheel at all, which is why `voice-cuda` carries the PyPI CUDA 12 wheels.

So the package installs the locked, tested set under `/usr/lib/voice` and runs
it on `python312` (AUR). Everything the *desktop* owns - PipeWire,
wl-clipboard, libnotify, the portal, PyGObject/GTK 4/pycairo/gtk4-layer-shell
for the recording pill - comes from pacman as a normal dependency.

## Layout

```
/usr/bin/voice                              wrapper: python312 + app + deps (+ cuda)
/usr/bin/voice-overlay                      wrapper: system python3 + app (pill helper)
/usr/lib/voice/app/voice/...                this project, and nothing else
/usr/lib/voice/deps/...                     the locked third-party wheels (CPython 3.12)
/usr/lib/voice/cuda/nvidia/*/lib/*.so       voice-cuda only
/usr/share/applications/io.github.vampyren.voice.desktop
/etc/xdg/autostart/io.github.vampyren.voice.desktop
/usr/lib/udev/rules.d/70-voice-input.rules
/usr/share/doc/voice/README.md
/usr/share/licenses/voice/LICENSE
```

`app` and `deps` are separate directories on purpose. The daemon spawns the
recording pill on the *system* interpreter and hands it
`voice.ui.overlay_client.repo_root()` - the parent of the `voice` package - as
`PYTHONPATH`. With the app alone in that directory, no CPython 3.12 extension
module can end up on the 3.14 interpreter's path.

The desktop entry's file name is the app id (`voice.APP_ID`): the
GlobalShortcuts portal resolves the app through it, and any other name makes it
refuse the session.

## What `pacman -R voice` leaves behind

Everything under the user's home, deliberately - a package must not delete a
user's data:

```
~/.config/voice          config.toml
~/.local/state/voice     history.jsonl, portal restore token
~/.cache/huggingface     downloaded models (shared with every other HF tool)
```

The scriptlet prints these on removal.

## Keeping it current

* `pkgver` tracks `voice.__version__`; bump `pkgrel` for a rebuild of the same
  version.
* `_branch` selects what is built. `source` points at the local checkout
  (`git+file://${startdir}/..`), so commit before building; switch to the
  commented GitHub line once the repository is published.
* The dependency set comes from `uv.lock` via `uv export --frozen`. Run
  `uv lock` in the project and rebuild to move it.
