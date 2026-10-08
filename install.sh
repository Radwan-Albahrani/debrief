#!/usr/bin/env bash
# One-shot setup: dependencies, a local venv, a `debrief` command on your PATH, then the models.
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
curl -sf http://localhost:11434/api/version >/dev/null || brew services start ollama >/dev/null 2>&1 || true

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

mkdir -p "$BIN"
cat > "$BIN/debrief" <<EOF
#!/bin/sh
exec "$REPO/.venv/bin/python" "$REPO/debrief.py" "\$@"
EOF
chmod +x "$BIN/debrief"
echo "Installed $BIN/debrief"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Add it to your PATH:  echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.zshrc && exec zsh" ;;
esac

"$BIN/debrief" setup "$@"
