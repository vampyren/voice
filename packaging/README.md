# Packaging voice for Arch / CachyOS

`PKGBUILD` builds **one** package, and it is GPU-ready:

| what is in it | rough size |
| --- | --- |
| the app, its locked Python dependency set, the CLI, desktop entry, autostart entry, udev rule | ~1.2 GB installed (PySide6 alone is 650 MB) |
| the CUDA 12 runtime (cuBLAS, cuDNN, NVRTC) that puts transcription on the GPU | 1.4 GB of wheels, ~3 GB installed |

## Build and install

```
cd packaging
makepkg -si                       # builds and installs `voice`, GPU included
```

`makepkg` needs network access in `build()` (it downloads the locked wheels),
so build with plain `makepkg`, not in a clean chroot with networking off.

## Why the GPU stack is not optional, and not a second package

Local transcription on an NVIDIA card is the point of the local backend, so a
plain `makepkg -si` has to produce an install that uses it. The CUDA 12 runtime
is therefore *inside* `voice`, and `nvidia-utils` (which owns
`/usr/lib/libcuda.so.1`) is a hard `depends`.

The alternative was to keep a `voice-cuda` package and put `depends=('voice-cuda')`
on the main one. It was rejected on the removal criterion:

* **One `makepkg -si`** — both forms manage it (`makepkg -i` installs every
  package a split build produced), so this does not decide it.
* **`pacman -R`** — this does. `voice` depending on `voice-cuda` while
  `voice-cuda` depends on `voice=$pkgver-$pkgrel` is a dependency **cycle**:
  `pacman -R voice` is refused ("voice-cuda requires voice") and so is
  `pacman -R voice-cuda`, and getting rid of the app means naming both packages
  or reaching for `-Rdd`. Breaking the cycle the other way (voice-cuda depending
  on nothing) trades that for `pacman -R voice` leaving 3 GB of orphaned
  `/usr/lib/voice/cuda` behind. One package removes whole, which is the promise
  the README makes.
* **No duplicated payload** — neither form duplicates anything: the wheels are
  installed once in `build()` and copied once into `$pkgdir`. Folding them in
  removes the one drift risk the split had, a `voice` and a `voice-cuda` at
  different `pkgrel`.
* **A CPU-only machine** — handled by a build option, below, rather than by
  making the common case worse.

## The CPU-only build (the escape hatch)

On a machine with no NVIDIA card:

```
cd packaging
VOICE_GPU=0 makepkg -si
```

That drops the CUDA wheels (~3 GB) and the `nvidia-utils` dependency. Nothing
else changes: same package name, same paths, same wrapper, same desktop entry —
`pacman -Qi voice` just says `(CPU-only build)` in the description.
`LocalTranscriber._load` already falls back to CPU int8 with
`"CUDA not available; using CPU int8 (slower)"`, and `voice doctor`'s **cuda**
line explains why nothing is using a GPU.

It is a variant, not a second product. `VOICE_GPU` is only ever read at build
time, the default is `1`, and an invalid value aborts the build.

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
  wheel at all, which is why the package carries the PyPI CUDA 12 wheels.

So the package installs the locked, tested set under `/usr/lib/voice` and runs
it on `python312` (AUR). Everything the *desktop* owns - PipeWire,
wl-clipboard, libnotify, the portal, PyGObject/GTK 4/pycairo/gtk4-layer-shell
for the recording pill - comes from pacman as a normal dependency.

## Layout

```
/usr/bin/voice                              wrapper: python312 + app + deps + cuda
/usr/bin/voice-overlay                      wrapper: system python3 + app (pill helper)
/usr/lib/voice/app/voice/...                this project, and nothing else
/usr/lib/voice/deps/...                     the locked third-party wheels (CPython 3.12)
/usr/lib/voice/cuda/nvidia/*/lib/*.so       the CUDA 12 runtime (absent in a VOICE_GPU=0 build)
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

## Known fat: cuDNN

The package ships what the project's `gpu` extra pins, which is what the app is
tested with. Two thirds of it may be dead weight: CTranslate2 4.8.2's own
binaries name `libcublas.so.12` and `libcuda.so.1` and contain no reference to
cuDNN at all (`strings ... | grep -ci cudnn` is 0), so `nvidia-cudnn-cu12`
(751 MB of wheel) is probably never loaded. Drop it from the `gpu` extra, run a
GPU transcription, and if it still works the package roughly halves.

## Keeping it current

* `pkgver` tracks `voice.__version__`; bump `pkgrel` for a rebuild of the same
  version.
* `conflicts`/`replaces=('voice-cuda')` exist only to retire the split build of
  `0.1.0-1`. Drop both once no machine still has it installed.
* `_branch` selects what is built. `source` points at the local checkout
  (`git+file://${startdir}/..`), so commit before building; switch to the
  commented GitHub line once the repository is published.
* The dependency set comes from `uv.lock` via `uv export --frozen`. Run
  `uv lock` in the project and rebuild to move it.
