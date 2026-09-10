#!/usr/bin/env bash
# voice installer: everything lives in this directory + ~/.config/voice; only the udev rule is system-wide.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=voice
BIN="$HOME/.local/bin/$APP"
APPS="$HOME/.local/share/applications/$APP.desktop"
AUTOSTART="$HOME/.config/autostart/$APP.desktop"
RULE_SRC="$ROOT/packaging/70-voice-input.rules"
RULE_DST="/etc/udev/rules.d/70-voice-input.rules"
MODE=install; UDEV=1; EXTRA=auto

for arg in "$@"; do
  case "$arg" in
    --uninstall) MODE=uninstall ;;
    --no-udev) UDEV=0 ;;
    --gpu) EXTRA=gpu ;;
    --cpu) EXTRA=cpu ;;
    -h|--help) echo "usage: install.sh [--uninstall] [--no-udev] [--gpu|--cpu]"; exit 0 ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done

run() { if [ "${DRY_RUN:-0}" = 1 ]; then echo "+ $*"; else echo "+ $*"; "$@"; fi; }
sudo_run() { if [ "${DRY_RUN:-0}" = 1 ]; then echo "+ sudo $*"; else echo "+ sudo $*"; sudo "$@"; fi; }

if [ "$MODE" = uninstall ]; then
  run rm -f "$BIN" "$APPS" "$AUTOSTART"
  [ -e "$RULE_DST" ] || [ "${DRY_RUN:-0}" = 1 ] && sudo_run rm -f "$RULE_DST" || true
  echo "left in place (delete if you want a clean slate): $HOME/.config/$APP $HOME/.local/state/$APP $HOME/.cache/huggingface"
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

if [ "$UDEV" = 1 ]; then
  echo "installing udev rule for keyboard access (asks for sudo once)"
  sudo_run install -m 644 "$RULE_SRC" "$RULE_DST"
  sudo_run udevadm control --reload
  sudo_run udevadm trigger --subsystem-match=input
fi

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "note: add $HOME/.local/bin to PATH" ;; esac
echo "installed. start with: $APP    (autostarts at next login)"
[ "${DRY_RUN:-0}" = 1 ] || "$BIN" doctor || true
