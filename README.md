# TTS Reader Tool

A local text-to-speech tool that reads text aloud. Two modes:

1. **Read selected text** — Select text in any window, press `Ctrl+Alt+R`, and hear it spoken
2. **OCR screen region** — Press `Ctrl+Alt+O`, drag a box over text on screen, and hear it read (works on images, games, non-selectable text)

Runs 100% offline. Uses **Kokoro** (high-quality) or **Piper** (lightweight, CPU).

Works on **Windows, macOS (Apple Silicon), and Linux** from a single script. The OS-specific bits — global hotkeys, the copy shortcut, OCR, and beeps — are handled per platform automatically.

## Controls

The default modifier is **Ctrl+Alt** on Windows/Linux and **Ctrl+Cmd (⌃⌘)** on macOS.
Every hotkey is configurable in `config.json` (see [Configuration](#configuration)).

| Key (Win/Linux) | Key (macOS) | What it does |
|-----|-----|-------------|
| `Ctrl+Alt+R` | `Ctrl+Cmd+R` | Read selected text aloud |
| `Ctrl+Alt+O` | `Ctrl+Cmd+O` | OCR a screen region, then read aloud |
| `Ctrl+Alt+→` | `Ctrl+Cmd+→` | Speed up |
| `Ctrl+Alt+←` | `Ctrl+Cmd+←` | Slow down |
| `Ctrl+Alt+Q` | `Ctrl+Cmd+Q` | Quit |
| `Escape` | `Escape` | Stop speaking |
| Tray icon | _(disabled on macOS)_ | Right-click for voice picker and quit |

## Setup — Desktop (GPU)

Your desktop uses Kokoro, which needs an NVIDIA GPU and Python 3.12.

> **Why 3.12 and not newer?** As of June 2026 the `kokoro` and `misaki` packages still cap at Python 3.12 — 3.13+ won't install. Stay on 3.12. (If you ever truly need 3.13, the third-party `kokoro-onnx` package supports it, but that's an engine swap, not a drop-in.)

### 1. Install Python 3.12

If you don't have it yet:
```
py install 3.12
```

### 2. Install PyTorch with CUDA

```
py -3.12 -m pip install torch --index-url https://download.pytorch.org/whl/cu128
```

> `cu128` works fine on the RTX 5080. For a fresh install you can also use the newer `cu130` index (PyTorch 2.9+); both work, so there's no need to reinstall just to switch.

### 3. Install the other packages

```
py -3.12 -m pip install kokoro>=0.9.4 soundfile sounddevice numpy pyperclip pyautogui pynput mss pystray Pillow winocr winrt-Windows.Media.Control
```

### 4. Run it

Double-click `run_tts_reader.bat` (it will ask for Administrator privileges — needed for global hotkeys).

Or from a terminal (run as Administrator):
```
py -3.12 tts_reader.py
```

The first run downloads the Kokoro voice model (~350 MB) automatically.

## Setup — Laptop (CPU only)

Your laptops use Piper, which runs on CPU without a GPU.

### 1. Install Python 3.12

```
py install 3.12
```

### 2. Install packages

```
py -3.12 -m pip install piper-tts sounddevice numpy pyperclip pyautogui pynput mss pystray Pillow winocr winrt-Windows.Media.Control
```

### 3. Edit the config

Open `config.json` (created automatically on first run) and change `tts_engine` to `"piper"`:
```json
{
    "tts_engine": "piper"
}
```

### 4. Run it

```
py -3.12 tts_reader.py
```

The first run downloads the Piper voice model (~60 MB) automatically.

## Setup — macOS (Apple Silicon)

On a Mac, Kokoro runs on the **CPU** (Apple's MPS GPU backend is actually slower for
this small model and shares the same unified RAM). Expect ~2 GB resident while running,
generating speech ~5× faster than realtime.

### 1. Install Python 3.12

The system Python (3.9) is too old. Either:
```
brew install python@3.12
```
or with [uv](https://docs.astral.sh/uv/):
```
uv python install 3.12
```

### 2. Create a venv and install packages

```
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements-macos.txt
```

### 3. Grant permissions

The first time you run it, macOS will prompt for permissions. Grant them to the app you
launch from (e.g. **Terminal**) in **System Settings → Privacy & Security**:

| Permission | Why |
|-----------|-----|
| **Accessibility** | Global hotkeys + simulating the copy shortcut |
| **Input Monitoring** | Detecting `Ctrl+Alt+...` and `Escape` globally |
| **Screen Recording** | Capturing the screen region for OCR |

### 4. Run it

```
./run_tts_reader.command
```
(or `./.venv/bin/python tts_reader.py`)

The first run downloads the Kokoro voice model (~350 MB) and an English text-processing
model automatically.

**macOS notes:**
- The **system tray is disabled on macOS** (pystray and the OCR overlay can't share the
  main thread). Quit with **Ctrl+Cmd+Q** or `Ctrl+C` in the terminal, and pick a voice by
  editing `config.json`.
- OCR uses **Apple's Vision framework** (built in — no Tesseract needed).
- The copy hotkey simulates **⌘C** instead of Ctrl+C.

## Configuration

Per-PC settings are stored in `config.json` (in the same folder as the script). This file is created automatically on first run with default values. It is not tracked by git, so each PC keeps its own copy.

```json
{
    "tts_engine": "kokoro",
    "kokoro_voice": "af_heart",
    "kokoro_speed": 1.0,
    "kokoro_device": "cpu",
    "piper_model": "voices/en_US-lessac-high.onnx",
    "pause_other_media": true,
    "hotkey_read": "ctrl+cmd+r",
    "hotkey_ocr": "ctrl+cmd+o",
    "hotkey_speed_up": "ctrl+cmd+right",
    "hotkey_speed_down": "ctrl+cmd+left",
    "hotkey_quit": "ctrl+cmd+q"
}
```

| Setting | What it does | Options |
|---------|-------------|---------|
| `tts_engine` | Which TTS engine to use | `"kokoro"` (high quality) or `"piper"` (lightweight CPU) |
| `kokoro_voice` | Kokoro voice name | See voice list below |
| `kokoro_speed` | Speech speed for Kokoro | `1.0` = normal, `1.5` = faster |
| `kokoro_device` | Compute device for Kokoro | `"cuda"` (NVIDIA desktop), `"cpu"` (macOS / laptops), `"mps"` (Apple GPU, not recommended) |
| `piper_model` | Path to Piper voice file | Default: `voices/en_US-lessac-high.onnx` |
| `pause_other_media` | While reading, pause other apps' playing media (Spotify, videos…) and resume them when done. **Windows only** — no-op on macOS/Linux. | `true` (default) or `false` |
| `hotkey_*` | Global hotkey bindings | Combo string like `"ctrl+cmd+r"`. Modifiers: `ctrl`, `alt` (Option on macOS), `cmd` (⌘), `shift`. Keys: letters or `right`/`left`/`up`/`down`. Defaults: Ctrl+Cmd on macOS, Ctrl+Alt on Windows/Linux. |

You only need to include settings you want to change — any missing settings use their defaults.

Your voice and speed choices are saved back to `config.json` automatically when you change them via the tray menu or speed hotkeys.

### Kokoro voice names

The voice name format is: `{accent}{gender}_{name}`
- `a` = American, `b` = British
- `f` = female, `m` = male

Some examples:
- `af_heart` — American female (default, warm tone)
- `af_bella` — American female
- `am_adam` — American male
- `bf_emma` — British female
- `bm_george` — British male

Full list: https://huggingface.co/hexgrad/Kokoro-82M

## How it works

**Mode 1 (Read selected text):**
You select text in any window. When you press Ctrl+Alt+R, the tool copies the selected text (simulates Ctrl+C, or ⌘C on macOS), sends it to the TTS engine, and plays the audio through your speakers.

**Mode 2 (OCR screen region):**
When you press Ctrl+Alt+O, the screen dims and your cursor becomes a crosshair. Drag a rectangle over any text (even in images or games). When you release, the tool takes a screenshot of that region, runs OCR (Windows built-in OCR, Apple Vision on macOS, or Tesseract on Linux), and reads the result aloud.

## Troubleshooting

**"No text selected" error beep:**
Make sure text is actually selected (highlighted) before pressing Ctrl+Alt+R.

**Hotkeys don't work:**
Global hotkeys need elevated input access. On **Windows**, run via `run_tts_reader.bat` or start your terminal as Administrator. On **macOS**, grant the launching app (e.g. Terminal) **Accessibility** and **Input Monitoring** in System Settings → Privacy & Security, then restart the app.

**macOS: OCR captures a blank/black image:**
Grant the launching app **Screen Recording** permission in System Settings → Privacy & Security, then restart it.

**OCR gives wrong text:**
Windows OCR works best with clear, high-contrast text. Very small text or stylized game fonts may not OCR well.

**Kokoro fails to load:**
For English you normally don't need to do anything — Kokoro pulls in `espeak-ng` automatically via its `espeakng-loader` dependency. Only if Kokoro still fails to load, install espeak-ng manually from: https://github.com/espeak-ng/espeak-ng/releases (this is mainly relevant for some non-English languages).
