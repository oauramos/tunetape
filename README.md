<p align="center">
  <img src="assets/logo.png" alt="tunetape logo" width="200">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-macOS-black?style=flat-square&logo=apple" alt="macOS">
  <img src="https://img.shields.io/badge/python-3.9+-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+">
  <img src="https://img.shields.io/github/license/oauramos/tunetape?style=flat-square" alt="License">
  <img src="https://img.shields.io/github/v/tag/oauramos/tunetape?style=flat-square&label=version" alt="Version">
</p>

<h1 align="center">tunetape</h1>

<p align="center">
  <b>Stream audio straight from your terminal.</b><br>
  <sub>YouTube. Video game soundtracks. No browser. No distractions. Just music.</sub>
</p>

<br>

<p align="center">
  <img src="assets/playlist.svg" alt="tunetape playlist player" width="600">
</p>

---

## How it works

```
tunetape → pick a source → paste a URL → music plays in your terminal
```

tunetape plays audio through **mpv** (no video) with a clean TUI and keyboard controls — all without leaving the terminal.

**Two sources:**
- **YouTube** — paste any YouTube URL. Audio extracted via **yt-dlp**.
- **KHInsider** — paste a [downloads.khinsider.com](https://downloads.khinsider.com) album URL. Full playlist with next/prev track controls.

---

## Install

One command. Installs everything automatically (Python, mpv, yt-dlp):

```bash
curl -fsSL https://raw.githubusercontent.com/oauramos/tunetape/main/install.sh | bash
```

That's it. Now run:

```bash
tunetape
```

<details>
<summary><b>Manual install</b></summary>

<br>

If you already have the dependencies:

```bash
brew install mpv yt-dlp
git clone https://github.com/oauramos/tunetape.git
cd tunetape
python3 -m venv .venv && source .venv/bin/activate
pip install .
```

> **Note:** `yt-dlp` is only required for YouTube. KHInsider works with just `mpv`.

</details>

<details>
<summary><b>Uninstall</b></summary>

<br>

```bash
rm -rf ~/.tunetape && sudo rm /usr/local/bin/tunetape
```

</details>

---

## Screens

### Main Menu

<p align="center">
  <img src="assets/menu.svg" alt="tunetape menu" width="600">
</p>

### YouTube Player

<p align="center">
  <img src="assets/player.svg" alt="tunetape youtube player" width="600">
</p>

### KHInsider Playlist

<p align="center">
  <img src="assets/playlist.svg" alt="tunetape playlist player" width="600">
</p>

### Error Handling

<p align="center">
  <img src="assets/error.svg" alt="tunetape error" width="600">
</p>

---

## Controls

### General

| Key | Action |
|:---:|--------|
| `space` | Play / Pause |
| `-->` | Seek forward 10s |
| `<--` | Seek backward 10s |
| `.` | Seek forward 30s |
| `,` | Seek backward 30s |
| `b` | Back to menu |
| `q` | Quit |

### Playlist Mode (KHInsider)

| Key | Action |
|:---:|--------|
| `n` | Next track |
| `p` | Previous track |

Tracks auto-advance when they finish.

---

## Built with

- [mpv](https://mpv.io/) — lightweight media player
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — YouTube audio extraction
- [rich](https://github.com/Textualize/rich) — terminal UI rendering

---

## Requirements

- macOS (uses Unix sockets + termios)
- Python 3.9+
- Homebrew (auto-installed if missing)

---

<p align="center">
  <sub>Made for terminal lovers who just want to listen.</sub>
</p>
