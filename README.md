<p align="center">
  <img src="https://img.shields.io/badge/platform-macOS-black?style=flat-square&logo=apple" alt="macOS">
  <img src="https://img.shields.io/badge/python-3.9+-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+">
  <img src="https://img.shields.io/github/license/oauramos/tunetape?style=flat-square" alt="License">
  <img src="https://img.shields.io/github/v/tag/oauramos/tunetape?style=flat-square&label=version" alt="Version">
</p>

<h1 align="center">tunetape</h1>

<p align="center">
  <b>Stream YouTube audio straight from your terminal.</b><br>
  <sub>No browser. No tabs. No distractions. Just music.</sub>
</p>

<br>

<p align="center">
  <img src="assets/player.svg" alt="tunetape player" width="600">
</p>

---

## How it works

```
tunetape → paste a YouTube URL → audio plays in your terminal
```

tunetape grabs the audio stream with **yt-dlp**, plays it through **mpv** (no video), and gives you a clean TUI with playback controls — all without leaving the terminal.

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

### Player

<p align="center">
  <img src="assets/player.svg" alt="tunetape player" width="600">
</p>

### Error Handling

<p align="center">
  <img src="assets/error.svg" alt="tunetape error" width="600">
</p>

---

## Controls

| Key | Action |
|:---:|--------|
| `space` | Play / Pause |
| `-->` | Seek forward 10s |
| `<--` | Seek backward 10s |
| `.` | Seek forward 30s |
| `,` | Seek backward 30s |
| `b` | Back to menu |
| `q` | Quit |

---

## Built with

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — YouTube audio extraction
- [mpv](https://mpv.io/) — lightweight media player
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
