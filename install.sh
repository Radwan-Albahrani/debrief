#!/usr/bin/env bash
# One-shot setup: dependencies, a local venv with debrief installed, a `debrief` command on your PATH, the models.
# Extra arguments go to `debrief setup`, e.g. `./install.sh -w turbo`.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"
BIN="${DEBRIEF_BIN:-$HOME/.local/bin}"

if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
  echo "debrief needs an Apple Silicon Mac: Whisper runs on the Neural Engine through WhisperKit." >&2
  exit 1
fi
command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh" >&2; exit 1; }

command -v whisperkit-cli >/dev/null || brew install whisperkit-cli
command -v ollama >/dev/null || brew install ollama
# `debrief setup` pulls the summary model, so Ollama has to be running by then
curl -sf http://localhost:11434/api/version >/dev/null || brew services start ollama >/dev/null 2>&1 || true

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || {
  echo "debrief needs Python 3.10 or newer (python3 is $(python3 --version 2>&1)). Try: brew install python" >&2
  exit 1
}
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .  # editable: a `git pull` updates the command too

mkdir -p "$BIN"
if [[ -e "$BIN/debrief" && ! -L "$BIN/debrief" ]]; then
  echo "$BIN/debrief already exists and is not ours; move it away and run ./install.sh again." >&2
  exit 1
fi
ln -sf "$REPO/.venv/bin/debrief" "$BIN/debrief"
echo "Installed $BIN/debrief"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Add it to your PATH:  echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.zshrc && exec zsh" ;;
esac

"$BIN/debrief" setup "$@"
