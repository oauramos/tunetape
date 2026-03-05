#!/bin/bash
set -e

INSTALL_DIR="$HOME/.tunetape"
BIN_DIR="$HOME/.local/bin"
BIN_LINK="$BIN_DIR/tunetape"

# ── Uninstall ────────────────────────────────────────────────
if [[ "$1" == "uninstall" ]]; then
    echo ""
    echo "  tunetape uninstaller"
    echo "  ====================="
    echo ""
    removed=false
    if [[ -d "$INSTALL_DIR" ]]; then
        rm -rf "$INSTALL_DIR"
        echo "  [ok] Removed $INSTALL_DIR"
        removed=true
    fi
    if [[ -f "$BIN_LINK" ]]; then
        rm -f "$BIN_LINK"
        echo "  [ok] Removed $BIN_LINK"
        removed=true
    fi
    if $removed; then
        echo ""
        echo "  [done] tunetape has been uninstalled."
    else
        echo "  [!] tunetape is not installed."
    fi
    echo ""
    exit 0
fi

# ── Install / Update ────────────────────────────────────────
echo ""
if [[ -d "$INSTALL_DIR" ]]; then
    echo "  tunetape updater"
    echo "  ================="
else
    echo "  tunetape installer"
    echo "  ==================="
fi
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

# Add ~/.local/bin to PATH if not already there
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    SHELL_NAME="$(basename "$SHELL")"
    case "$SHELL_NAME" in
        zsh)  RC_FILE="$HOME/.zshrc" ;;
        bash) RC_FILE="$HOME/.bash_profile" ;;
        *)    RC_FILE="$HOME/.profile" ;;
    esac

    EXPORT_LINE='export PATH="$HOME/.local/bin:$PATH"'

    if ! grep -qF '.local/bin' "$RC_FILE" 2>/dev/null; then
        echo "" >> "$RC_FILE"
        echo "# Added by tunetape installer" >> "$RC_FILE"
        echo "$EXPORT_LINE" >> "$RC_FILE"
        echo "  [ok] Added ~/.local/bin to PATH in $RC_FILE"
    fi

    # Make it available in the current session too
    export PATH="$BIN_DIR:$PATH"
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
    version=$(git -C "$INSTALL_DIR" describe --tags 2>/dev/null || git -C "$INSTALL_DIR" rev-parse --short HEAD)
else
    version="latest"
fi

echo ""
echo "  [done] tunetape ($version) installed! Run it with:"
echo ""
echo "    tunetape"
echo ""
echo "  To uninstall later:"
echo ""
echo "    curl -fsSL https://raw.githubusercontent.com/oauramos/tunetape/main/install.sh | bash -s uninstall"
echo ""
