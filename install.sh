#!/bin/bash
set -e

INSTALL_DIR="$HOME/.tunetape"
BIN_LINK="/usr/local/bin/tunetape"

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
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
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
    git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || true
else
    echo "  [*] Downloading tunetape..."
    git clone https://github.com/oauramos/tunetape.git "$INSTALL_DIR"
fi

# Create venv and install
echo "  [*] Setting up Python environment..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/.venv/bin/pip" install "$INSTALL_DIR" -q

# Create wrapper script
echo "  [*] Creating tunetape command..."
sudo mkdir -p "$(dirname "$BIN_LINK")"
sudo tee "$BIN_LINK" > /dev/null << 'WRAPPER'
#!/bin/bash
exec "$HOME/.tunetape/.venv/bin/tunetape" "$@"
WRAPPER
sudo chmod +x "$BIN_LINK"

echo ""
echo "  [done] tunetape installed! Run it with:"
echo ""
echo "    tunetape"
echo ""
