#!/usr/bin/env bash
# install.sh — build and install ccc to PREFIX (default: /usr/local/bin)
set -euo pipefail

PREFIX="${PREFIX:-$HOME/.local/bin}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure bun is available
if ! command -v bun &>/dev/null; then
  echo "Installing bun..."
  curl -fsSL https://bun.sh/install | bash
  export PATH="$HOME/.bun/bin:$PATH"
fi

# Ensure tmux is available
if ! command -v tmux &>/dev/null; then
  if command -v brew &>/dev/null; then
    echo "Installing tmux via Homebrew..."
    brew install tmux
  else
    echo "Error: tmux not found. Install it first (e.g. apt install tmux / brew install tmux)" >&2
    exit 1
  fi
fi

cd "$SCRIPT_DIR"
bun install --frozen-lockfile
bun build src/cli.ts --compile --outfile ccc

cp -f ccc "$PREFIX/ccc"
chmod 755 "$PREFIX/ccc"
echo "Installed: $PREFIX/ccc"

# Add PREFIX to PATH in shell rc if not already present
for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
  if [ -f "$rc" ] && ! grep -q "$PREFIX" "$rc"; then
    echo "export PATH=\"$PREFIX:\$PATH\"" >> "$rc"
    echo "Added $PREFIX to PATH in $rc"
  fi
done

echo ""
echo "Run: source ~/.zshrc  (or open a new terminal)"
echo "Then: ccc --help"
