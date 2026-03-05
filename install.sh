#!/bin/bash
set -e

INSTALL_DIR="$HOME/.tunetape"
BIN_DIR="$HOME/.local/bin"
BIN_LINK="$BIN_DIR/tunetape"

echo ""
echo "  tunetape installer"
echo "  ==================="
echo ""

# Check macOS
if [[ "$(uname)" != "Darwin" ]]; then
    echo "  [!] This installer is for macOS only."
    exit 1
fi

# Check/install Homebrew
if ! command -v brew &>/dev/null; then
    echo "  [*] Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Source brew into current session
    if [[ -x /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -x /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
fi

# Install dependencies via brew
for dep in python3 mpv yt-dlp; do
    if ! command -v "$dep" &>/dev/null; then
        echo "  [*] Installing $dep..."
        brew install "$dep"
    else
        echo "  [ok] $dep found"
    fi
done

# Clone or update repo
if [[ -d "$INSTALL_DIR" ]]; then
    echo "  [*] Updating tunetape..."
    git -C "$INSTALL_DIR" pull --ff-only || echo "  [!] Update failed, using existing version"
else
    echo "  [*] Downloading tunetape..."
    git clone https://github.com/oauramos/tunetape.git "$INSTALL_DIR"
fi

# Create venv and install
echo "  [*] Setting up Python environment..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/.venv/bin/pip" install "$INSTALL_DIR" -q

# [#14] Install to ~/.local/bin instead of /usr/local/bin — no sudo needed
echo "  [*] Creating tunetape command..."
mkdir -p "$BIN_DIR"
cat > "$BIN_LINK" << 'WRAPPER'
#!/bin/bash
exec "$HOME/.tunetape/.venv/bin/tunetape" "$@"
WRAPPER
chmod +x "$BIN_LINK"

# Check if ~/.local/bin is in PATH
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    echo ""
    echo "  [!] Add ~/.local/bin to your PATH:"
    echo ""
    echo "      echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
    echo "      source ~/.zshrc"
fi

echo ""
echo "  [done] tunetape installed! Run it with:"
echo ""
echo "    tunetape"
echo ""
