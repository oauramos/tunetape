# tunetape

Terminal YouTube audio player for macOS.

## Install

One command — installs everything (Python, mpv, yt-dlp):

```bash
curl -fsSL https://raw.githubusercontent.com/oauramos/tunetape/main/install.sh | bash
```

### Uninstall

```bash
rm -rf ~/.tunetape && sudo rm /usr/local/bin/tunetape
```

### Manual install

Requires Python 3.9+, [mpv](https://mpv.io/), and [yt-dlp](https://github.com/yt-dlp/yt-dlp).

```bash
brew install mpv yt-dlp
pip install .
```

## Usage

```bash
tunetape
```

1. Select "Play URL" from the menu
2. Paste a YouTube URL
3. Control playback with keyboard shortcuts:

| Key | Action |
|-----|--------|
| Space | Play / Pause |
| Right / Left | Seek +/- 10s |
| `.` / `,` | Seek +/- 30s |
| `b` | Back to menu |
| `q` | Quit |
