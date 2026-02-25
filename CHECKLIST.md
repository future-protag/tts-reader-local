# TTS Reader Tool — Build Checklist

A local text-to-speech tool that reads text aloud. Select text and press a hotkey,
or draw a box on screen to OCR and read non-selectable text.

**Engines:** Kokoro (desktop GPU) | Piper (laptop CPU)
**Python:** 3.12 (not 3.14 — Kokoro doesn't support it yet)
**Hotkeys:** Ctrl+Alt+R (read selected), Ctrl+Alt+O (OCR region), Escape (stop)

---

## Phase 0: Project Setup
- [x] Create folder structure (`tts-reader/`, `voices/`)
- [x] Create this CHECKLIST.md
- [x] Create `.gitignore` (exclude models, cache, venv)
- [x] Initialize git repo

## Phase 1: Core Read-Aloud (Kokoro)
- [x] Create `tts_reader.py` with configuration section, logging, sound feedback
- [x] Load Kokoro TTS engine at startup
- [x] Implement Ctrl+Alt+R: grab selected text via clipboard
- [x] Implement streaming audio playback via sounddevice
- [x] Implement Escape to stop speaking mid-sentence
- [ ] Test: select text in Notepad, press Ctrl+Alt+R, hear it spoken (needs live test)

## Phase 2: System Tray + Launcher
- [x] Add system tray icon (green=ready, blue=speaking, yellow=processing)
- [x] Add tray menu (quit)
- [x] Create `run_tts_reader.bat` (Admin elevation, uses `py -3.12`)
- [ ] Test: launch via batch file, verify tray icon works (needs live test)

## Phase 3: OCR Mode (Screen Region)
- [x] Implement tkinter fullscreen overlay (dimmed, crosshair cursor)
- [x] Implement drag-to-select rectangle (red outline)
- [x] Implement screenshot capture of selected region
- [x] Implement OCR via winocr (Windows built-in OCR)
- [x] Wire up: Ctrl+Alt+O → overlay → drag → screenshot → OCR → speak
- [ ] Test: OCR text from an image or document (needs live test)

## Phase 4: Piper Engine (Laptop Support)
- [x] Add Piper TTS engine wrapper (same interface as Kokoro)
- [x] Add auto-download for Piper voice models from HuggingFace
- [ ] Test: switch TTS_ENGINE to "piper", verify it works without GPU (needs laptop)

## Phase 5: Documentation
- [x] Write README.md (setup for desktop + laptop, usage, troubleshooting)
- [x] Final update to this checklist

---

## Status: Ready for live testing

All code is written and dependencies are installed. Kokoro engine loads
successfully and generates audio. Remaining items are live tests that
require running the full tool interactively (with Administrator privileges
for global hotkeys).

**To test:** Double-click `run_tts_reader.bat` or run `py -3.12 tts_reader.py`
as Administrator.

---

## Setup Requirements

### Desktop (Kokoro — needs GPU)
1. Install Python 3.12: `py install 3.12`
2. `py -3.12 -m pip install torch --index-url https://download.pytorch.org/whl/cu128`
3. `py -3.12 -m pip install kokoro>=0.9.4 soundfile sounddevice numpy keyboard pyperclip pyautogui pystray Pillow winocr`
4. espeak-ng is bundled with kokoro's `espeakng-loader` package (no separate install needed)

### Laptop (Piper — CPU only)
1. Install Python 3.12: `py install 3.12`
2. `py -3.12 -m pip install piper-tts sounddevice numpy keyboard pyperclip pyautogui pystray Pillow winocr`
3. Change `TTS_ENGINE = "piper"` at top of `tts_reader.py`
4. Voice model auto-downloads on first run

---

## Architecture Notes
- Single-file script (`tts_reader.py`) — matches dictation tool pattern
- Engine abstraction: both Kokoro and Piper yield float32 numpy audio chunks
- Playback streams chunks as they arrive (no waiting for full generation)
- Models excluded from git — Kokoro auto-downloads via HuggingFace, Piper auto-downloads at startup
- Change `TTS_ENGINE = "piper"` at top of file for laptop use
