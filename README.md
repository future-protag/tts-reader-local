# TTS Reader Tool

A local text-to-speech tool that reads text aloud. Two modes:

1. **Read selected text** — Select text in any window, press `Ctrl+Alt+R`, and hear it spoken
2. **OCR screen region** — Press `Ctrl+Alt+O`, drag a box over text on screen, and hear it read (works on images, games, non-selectable text)

Runs 100% offline. Uses **Kokoro** (high-quality, GPU) on desktop or **Piper** (lightweight, CPU) on laptops.

## Controls

| Key | What it does |
|-----|-------------|
| `Ctrl+Alt+R` | Read selected text aloud |
| `Ctrl+Alt+O` | OCR a screen region, then read aloud |
| `Escape` | Stop speaking |
| Tray icon | Right-click to quit |

## Setup — Desktop (GPU)

Your desktop uses Kokoro, which needs an NVIDIA GPU and Python 3.12.

### 1. Install Python 3.12

If you don't have it yet:
```
py install 3.12
```

### 2. Install PyTorch with CUDA

```
py -3.12 -m pip install torch --index-url https://download.pytorch.org/whl/cu128
```

### 3. Install the other packages

```
py -3.12 -m pip install kokoro>=0.9.4 soundfile sounddevice numpy keyboard pyperclip pyautogui mss pystray Pillow winocr
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
py -3.12 -m pip install piper-tts sounddevice numpy keyboard pyperclip pyautogui mss pystray Pillow winocr
```

### 3. Edit the config

Open `tts_reader.py` and change this line near the top:
```python
TTS_ENGINE = "piper"
```

### 4. Run it

```
py -3.12 tts_reader.py
```

The first run downloads the Piper voice model (~60 MB) automatically.

## Configuration

All settings are at the top of `tts_reader.py`:

```python
TTS_ENGINE = "kokoro"          # "kokoro" (desktop) or "piper" (laptop)
KOKORO_VOICE = "af_heart"      # Kokoro voice name (see voice list below)
KOKORO_SPEED = 1.0             # Speech speed (1.0 = normal)
PIPER_MODEL = "voices/en_US-lessac-medium.onnx"  # Piper voice file
```

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
You select text in any window. When you press Ctrl+Alt+R, the tool copies the selected text (simulates Ctrl+C), sends it to the TTS engine, and plays the audio through your speakers.

**Mode 2 (OCR screen region):**
When you press Ctrl+Alt+O, the screen dims and your cursor becomes a crosshair. Drag a rectangle over any text (even in images or games). When you release, the tool takes a screenshot of that region, runs OCR (using Windows' built-in text recognition), and reads the result aloud.

## Troubleshooting

**"No text selected" error beep:**
Make sure text is actually selected (highlighted) before pressing Ctrl+Alt+R.

**Hotkeys don't work:**
The tool needs Administrator privileges for global hotkeys. Run via `run_tts_reader.bat` or start your terminal as Administrator.

**OCR gives wrong text:**
Windows OCR works best with clear, high-contrast text. Very small text or stylized game fonts may not OCR well.

**Kokoro fails to load:**
Make sure `espeak-ng` dependencies are installed. Kokoro's `espeakng-loader` package should handle this automatically, but if not, install espeak-ng manually from: https://github.com/espeak-ng/espeak-ng/releases
