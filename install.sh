#!/usr/bin/env bash
# voice installer: everything lives in this directory + ~/.config/voice; only the udev rule is system-wide.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=voice
# Must equal voice.APP_ID: the desktop portal resolves our app id (needed by the
# GlobalShortcuts hotkey backend) through a desktop entry with exactly this name.
APP_ID=io.github.vampyren.voice
BIN="$HOME/.local/bin/$APP"
APPS="$HOME/.local/share/applications/$APP_ID.desktop"
AUTOSTART="$HOME/.config/autostart/$APP_ID.desktop"
LEGACY_APPS="$HOME/.local/share/applications/$APP.desktop"
LEGACY_AUTOSTART="$HOME/.config/autostart/$APP.desktop"
RULE_SRC="$ROOT/packaging/70-voice-input.rules"
RULE_DST="/etc/udev/rules.d/70-voice-input.rules"
MODE=install; UDEV=1; EXTRA=auto; PURGE=ask

for arg in "$@"; do
  case "$arg" in
    --uninstall) MODE=uninstall ;;
    --purge) PURGE=1 ;;
    --keep-settings) PURGE=0 ;;
    --no-udev) UDEV=0 ;;
    --gpu) EXTRA=gpu ;;
    --cpu) EXTRA=cpu ;;
    -h|--help) echo "usage: install.sh [--uninstall [--purge|--keep-settings]] [--no-udev] [--gpu|--cpu]"; exit 0 ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done

run() { if [ "${DRY_RUN:-0}" = 1 ]; then echo "+ $*"; else echo "+ $*"; "$@"; fi; }
sudo_run() { if [ "${DRY_RUN:-0}" = 1 ]; then echo "+ sudo $*"; else echo "+ sudo $*"; sudo "$@"; fi; }

if [ "$MODE" = uninstall ]; then
  run rm -f "$BIN" "$APPS" "$AUTOSTART" "$LEGACY_APPS" "$LEGACY_AUTOSTART"
  [ -e "$RULE_DST" ] || [ "${DRY_RUN:-0}" = 1 ] && sudo_run rm -f "$RULE_DST" || true
  # Your settings are yours: never removed without being asked. With no
  # terminal to ask on - a script, CI - they are kept, which is the safe answer.
  if [ "$PURGE" = ask ]; then
    if [ -t 0 ] && [ "${DRY_RUN:-0}" != 1 ]; then
      echo
      echo "Delete your settings and dictation history as well?"
      echo "  $HOME/.config/$APP        settings (config.toml)"
      echo "  $HOME/.local/state/$APP   dictation history, portal permission token"
      printf "[y/N] "
      read -r _reply || _reply=""
      case "$_reply" in [yY]*) PURGE=1 ;; *) PURGE=0 ;; esac
    else
      PURGE=0
    fi
  fi

  if [ "$PURGE" = 1 ]; then
    run rm -rf "$HOME/.config/$APP" "$HOME/.local/state/$APP"
    echo "settings and history deleted"
  else
    echo "kept (delete by hand for a clean slate): $HOME/.config/$APP $HOME/.local/state/$APP"
  fi
  echo "kept regardless: $HOME/.cache/huggingface - the downloaded models, shared with"
  echo "  any other tool that uses Hugging Face. Remove just ours with:"
  echo "  rm -rf $HOME/.cache/huggingface/hub/models--Systran--faster-whisper-*"
  echo "a daemon started before this keeps running; stop it with: pkill -f 'voice daemon'"
  exit 0
fi

if ! command -v uv >/dev/null 2>&1 && [ "${DRY_RUN:-0}" != 1 ]; then
  echo "uv is required: sudo pacman -S uv   (or: curl -LsSf https://astral.sh/uv/install.sh | sh)" >&2
  exit 1
fi

if [ "$EXTRA" = auto ]; then
  if command -v nvidia-smi >/dev/null 2>&1; then EXTRA=gpu; else EXTRA=cpu; fi
fi
if [ "$EXTRA" = gpu ]; then run uv sync --project "$ROOT" --extra gpu; else run uv sync --project "$ROOT"; fi

run mkdir -p "$(dirname "$BIN")" "$(dirname "$APPS")" "$(dirname "$AUTOSTART")"
if [ "${DRY_RUN:-0}" = 1 ]; then
  echo "+ write $BIN"
else
  cat > "$BIN" <<EOF
#!/usr/bin/env bash
# CTranslate2 dlopens libcublas.so.12 and libcudnn at runtime, and nothing puts
# the --extra gpu wheels' copies on the loader path, so without this the GPU
# libraries are installed but never found and the local backend quietly falls
# back to CPU int8. Harmless when the wheels are absent: the glob matches
# nothing and LD_LIBRARY_PATH is left alone.
for dir in "$ROOT"/.venv/lib/python*/site-packages/nvidia/*/lib; do
  [ -d "\$dir" ] && LD_LIBRARY_PATH="\$dir\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
done
[ -n "\${LD_LIBRARY_PATH:-}" ] && export LD_LIBRARY_PATH
exec uv --project "$ROOT" run --no-sync $APP "\$@"
EOF
  chmod +x "$BIN"
fi
run sed "s|^Exec=.*|Exec=$BIN daemon|" "$ROOT/packaging/$APP.desktop" > /dev/null
if [ "${DRY_RUN:-0}" = 1 ]; then
  echo "+ install desktop entry -> $APPS and autostart -> $AUTOSTART"
else
  sed "s|^Exec=.*|Exec=$BIN daemon|" "$ROOT/packaging/$APP.desktop" > "$APPS"
  cp "$APPS" "$AUTOSTART"
fi
# From installs before the app id rename; through run() so the dry run says so too.
run rm -f "$LEGACY_APPS" "$LEGACY_AUTOSTART"

if [ "$UDEV" = 1 ]; then
  echo "installing udev rule for keyboard access (asks for sudo once)"
  sudo_run install -m 644 "$RULE_SRC" "$RULE_DST"
  sudo_run udevadm control --reload
  sudo_run udevadm trigger --subsystem-match=input
fi

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "note: add $HOME/.local/bin to PATH" ;; esac
echo "installed. start with: $APP    (autostarts at next login)"
[ "${DRY_RUN:-0}" = 1 ] || "$BIN" doctor || true
