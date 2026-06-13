"""
Text-to-Speech Reader Tool
===========================
Select text and press Ctrl+Alt+R to hear it read aloud.
Or press Ctrl+Alt+O to OCR a screen region and read it.
Press Escape to stop speaking.

Usage:  python tts_reader.py
        Global hotkeys need elevated input access:
          - Windows: run the terminal as Administrator
          - macOS:   grant the terminal Accessibility + Input Monitoring, and
                     Screen Recording (for the OCR screenshot)
          - Linux:   run with sufficient privileges for input capture

Controls (default modifier: Ctrl+Alt on Windows/Linux, Ctrl+Cmd on macOS;
all rebindable in config.json):
    <mod>+R     - Read selected text aloud
    <mod>+O     - OCR a screen region, then read aloud
    <mod>+Q     - Quit
    Escape      - Stop speaking
    Tray        - Right-click the system tray icon for options and quit
                  (Windows/Linux; the tray is disabled on macOS)
"""

import os
import sys
import json
import queue
import time
import atexit
import asyncio
import logging
import warnings
import threading
import tkinter as tk

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
# This tool started life on Windows and now also runs on macOS (and Linux).
# The OS-specific pieces — global hotkeys, the "copy" shortcut, OCR, beeps,
# DPI — are branched on these flags. The TTS engine, audio streaming, and
# the WSOLA time-stretch are identical on every platform.
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# Windows only: declare per-monitor-v2 DPI awareness so screenshots report
# real pixel coordinates on multi-monitor / mixed-scaling setups.
#
# There are three levels of DPI awareness on Windows:
#   1. SetProcessDPIAware() — only knows the PRIMARY monitor's scaling.
#   2. SetProcessDpiAwareness(2) — per-monitor aware (Windows 8.1+).
#   3. SetProcessDpiAwarenessContext(-4) — per-monitor aware v2 (Windows 10 1703+).
# We try the best one first and fall back on older Windows. macOS and Linux
# already hand us correct coordinates, so this whole block is a no-op there.
if IS_WINDOWS:
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass

# Suppress noisy warnings from libraries before importing them
warnings.filterwarnings("ignore", category=UserWarning, module="torch")
warnings.filterwarnings("ignore", category=FutureWarning, module="torch")
warnings.filterwarnings("ignore", category=UserWarning, module="huggingface_hub")
logging.getLogger("transformers").setLevel(logging.ERROR)

import numpy as np
import sounddevice as sd
import pyperclip
import mss              # Cross-platform multi-monitor screenshots (Windows GDI / macOS CG / X11)
from PIL import Image   # Used to convert mss screenshots to PIL format for OCR

# Global-hotkey + synthetic-keystroke backend: `pynput` on every platform.
#   macOS / Linux: needs Accessibility / Input Monitoring permission.
#   Windows: the low-level hook needs no special permission to *detect* hotkeys
#            (Administrator is no longer required just for that). Note: sending
#            the synthetic copy into an already-elevated window still needs admin.
# Windows previously used the `keyboard` library, but it has been unmaintained
# since 2020 and forced the whole app to run elevated; pynput unifies the input
# path. pyautogui is still used only to simulate the copy shortcut on Windows
# (on macOS/Linux we synthesize the copy keystroke with pynput).
from pynput import keyboard as pynput_keyboard
if IS_WINDOWS:
    import pyautogui

# Try to import system tray libraries (optional — script works without them).
# NOTE: the tray is intentionally disabled on macOS (see main()) because pystray
# and tkinter both need the main thread's run loop there and can't share it.
try:
    from PIL import ImageDraw
    import pystray
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False
    print("Warning: pystray/Pillow not available. Running without system tray icon.")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Settings that can differ per PC are stored in config.json (not tracked by git).
# If config.json doesn't exist, a default one is created automatically.

# Where to find config.json — same folder as this script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

# Default global-hotkey modifier, per platform.
#   macOS: Ctrl+Cmd (⌃⌘) — comfortable and low-conflict. ("alt" = the Option
#          key on a Mac, which is awkward / often unlabelled, so we avoid it.)
#   Windows/Linux: Ctrl+Alt.
# These are just defaults — every hotkey can be overridden in config.json.
_DEFAULT_MOD = "ctrl+cmd" if IS_MAC else "ctrl+alt"

# Default values (used when config.json is missing or incomplete)
DEFAULTS = {
    "tts_engine": "kokoro",
    "kokoro_voice": "af_heart",
    "kokoro_speed": 1.0,
    # Device for Kokoro/PyTorch. Per machine:
    #   "cpu"  -> works everywhere; best choice on Apple Silicon (MPS is slower
    #             for an 82M model and shares the same unified RAM).
    #   "cuda" -> NVIDIA GPU (the Windows desktop) — runs in dedicated VRAM.
    #   "mps"  -> Apple GPU; supported but not recommended here (see above).
    # Default is platform-aware: cuda on Windows (the desktop has an NVIDIA GPU),
    # cpu elsewhere. Override per machine via config.json.
    "kokoro_device": "cuda" if IS_WINDOWS else "cpu",
    "piper_model": "voices/en_US-lessac-high.onnx",
    # Global hotkeys. Combo syntax: "+"-separated, e.g. "ctrl+cmd+r".
    # Recognized modifiers: ctrl, alt (Option on macOS), cmd (⌘), shift.
    # Keys: letters, or right/left/up/down/space/enter/tab.
    "hotkey_read": f"{_DEFAULT_MOD}+r",          # Read selected text aloud
    "hotkey_ocr": f"{_DEFAULT_MOD}+o",           # OCR a screen region, then read
    "hotkey_speed_up": f"{_DEFAULT_MOD}+right",  # Increase speech speed
    "hotkey_speed_down": f"{_DEFAULT_MOD}+left", # Decrease speech speed
    "hotkey_quit": f"{_DEFAULT_MOD}+q",          # Quit
    # Pause other apps' media (Spotify, video, etc.) while we speak, then resume
    # when we finish. Windows only (uses the system media controls); a no-op
    # elsewhere. Set to false to leave other audio playing.
    "pause_other_media": True,
}

def load_config():
    """Load settings from config.json. Creates a default file if it doesn't exist."""
    if not os.path.exists(CONFIG_PATH):
        # First run — create a default config.json
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULTS, f, indent=4)
        print(f"Created default config file: {CONFIG_PATH}")
        print("Edit config.json to change settings (e.g. tts_engine, kokoro_voice).")
        return dict(DEFAULTS)

    with open(CONFIG_PATH, "r") as f:
        user_config = json.load(f)

    # Start with defaults, then override with whatever the user put in config.json
    config = dict(DEFAULTS)
    config.update(user_config)
    return config

_config = load_config()

# Per-PC settings (from config.json)
TTS_ENGINE = _config["tts_engine"]
KOKORO_VOICE = _config["kokoro_voice"]
KOKORO_SPEED = _config["kokoro_speed"]
KOKORO_DEVICE = _config["kokoro_device"]
PIPER_MODEL = _config["piper_model"]
PAUSE_OTHER_MEDIA = _config["pause_other_media"]

# Hotkeys (configurable per machine via config.json; defaults set above)
HOTKEY_READ = _config["hotkey_read"]
HOTKEY_OCR = _config["hotkey_ocr"]
HOTKEY_SPEED_UP = _config["hotkey_speed_up"]
HOTKEY_SPEED_DOWN = _config["hotkey_speed_down"]
HOTKEY_QUIT = _config["hotkey_quit"]

# --- Other settings (not in config.json) ---
KOKORO_LANG = "a"              # "a" = American English, "b" = British English
KOKORO_SAMPLE_RATE = 24000     # Kokoro outputs audio at 24,000 Hz (don't change)
PIPER_DOWNLOAD_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/high"
OCR_LANGUAGE = "en"            # Language for Windows OCR

# Error log file — records crashes and errors for debugging
ERROR_LOG = os.path.join(SCRIPT_DIR, "error_log.txt")

# --- Available Kokoro voices (grouped by accent and gender) ---
# Each key is a submenu label, each value is a list of (display_name, voice_id) pairs.
# The accent letter ("a" or "b") is used to determine if the Kokoro pipeline
# needs to be reloaded when switching between American and British voices.
KOKORO_VOICES = {
    "American Female": [
        ("Alloy",   "af_alloy"),
        ("Aoede",   "af_aoede"),
        ("Bella",   "af_bella"),
        ("Heart",   "af_heart"),
        ("Jessica", "af_jessica"),
        ("Kore",    "af_kore"),
        ("Nicole",  "af_nicole"),
        ("Nova",    "af_nova"),
        ("River",   "af_river"),
        ("Sarah",   "af_sarah"),
        ("Sky",     "af_sky"),
    ],
    "American Male": [
        ("Adam",    "am_adam"),
        ("Echo",    "am_echo"),
        ("Eric",    "am_eric"),
        ("Fenrir",  "am_fenrir"),
        ("Liam",    "am_liam"),
        ("Michael", "am_michael"),
        ("Onyx",    "am_onyx"),
        ("Puck",    "am_puck"),
        ("Santa",   "am_santa"),
    ],
    "British Female": [
        ("Alice",    "bf_alice"),
        ("Emma",     "bf_emma"),
        ("Isabella", "bf_isabella"),
        ("Lily",     "bf_lily"),
    ],
    "British Male": [
        ("Daniel", "bm_daniel"),
        ("Fable",  "bm_fable"),
        ("George", "bm_george"),
        ("Lewis",  "bm_lewis"),
    ],
}


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
is_speaking = False            # Is audio currently playing?
is_processing = False          # Is TTS generation or OCR running?
tts_engine_obj = None          # The loaded TTS model (Kokoro pipeline or Piper voice)
tray_icon = None               # System tray icon
should_quit = False            # Signal to exit the program
ocr_requested = False          # Flag: main loop should open the region selector
current_speed = KOKORO_SPEED   # Current speech speed (can be changed with hotkeys)
current_voice = KOKORO_VOICE   # Current Kokoro voice (can be changed from tray menu)
current_lang = KOKORO_LANG     # Current Kokoro accent/language code


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------
def log(message):
    """Print a timestamped message to the console."""
    timestamp = time.strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")


def write_error_log(error):
    """Append an error entry to the error log file."""
    import traceback
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(f"{timestamp} | {error}\n")
        f.write(traceback.format_exc() + "\n")


# ---------------------------------------------------------------------------
# Pause other apps' media while we speak (Windows only)
# ---------------------------------------------------------------------------
# When we start speaking, pause every app that is *currently playing* (Spotify,
# a browser video, etc.) and remember which ones we paused; when we finish (or
# stop, or quit) resume exactly those — so we never un-pause something the user
# had paused themselves. Uses the Windows System Media Transport Controls (SMTC)
# via WinRT; no Administrator needed. No-op on macOS/Linux (macOS has no clean
# per-app pause API — see the project notes).
_paused_app_ids = []   # apps WE paused, so we can resume only those

# The Windows media-control API lives in winrt, which we import LAZILY (on first
# use) instead of at startup. Reason: on Windows, importing winrt before torch
# makes torch's c10.dll fail to initialise — a native DLL load-order conflict.
# Kokoro loads torch at startup, so by the time we first pause media during a
# read, torch is already up and winrt loads safely afterwards.
_MediaManager = None        # filled in by _ensure_media_manager() on first use
_media_pause_ready = None   # None = not tried yet; True/False after the attempt


def _ensure_media_manager():
    """Import the winrt media API on first use and cache the result.
    Returns True if media auto-pause is available."""
    global _MediaManager, _media_pause_ready
    if _media_pause_ready is not None:
        return _media_pause_ready
    if not (IS_WINDOWS and PAUSE_OTHER_MEDIA):
        _media_pause_ready = False
        return False
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as _MM,
        )
        _MediaManager = _MM
        _media_pause_ready = True
    except Exception:
        print("Note: media auto-pause is off — install it with: "
              "py -3.12 -m pip install winrt-Windows.Media.Control")
        _media_pause_ready = False
    return _media_pause_ready

_PLAYING_STATUS = 4   # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING


async def _pause_playing_sessions():
    """Pause every currently-playing session; return the app ids we paused."""
    mgr = await _MediaManager.request_async()
    paused = []
    for session in mgr.get_sessions():
        try:
            if int(session.get_playback_info().playback_status) == _PLAYING_STATUS:
                if await session.try_pause_async():
                    paused.append(session.source_app_user_model_id)
        except Exception:
            pass  # one stubborn app must not block the rest
    return paused


async def _resume_sessions(app_ids):
    """Resume the sessions whose app id is in app_ids."""
    wanted = set(app_ids)
    mgr = await _MediaManager.request_async()
    for session in mgr.get_sessions():
        try:
            if session.source_app_user_model_id in wanted:
                await session.try_play_async()
        except Exception:
            pass


def pause_other_media():
    """Pause other apps' currently-playing media (Windows). Remembers what it paused."""
    global _paused_app_ids
    if not _ensure_media_manager():
        return
    try:
        _paused_app_ids = asyncio.run(_pause_playing_sessions())
        if _paused_app_ids:
            log(f"Paused other media: {', '.join(_paused_app_ids)}")
    except Exception as e:
        log(f"Could not pause other media: {e}")
        _paused_app_ids = []


def resume_other_media():
    """Resume the media we paused earlier (Windows). Safe to call more than once."""
    global _paused_app_ids
    if not _paused_app_ids:
        return
    to_resume, _paused_app_ids = _paused_app_ids, []
    try:
        asyncio.run(_resume_sessions(to_resume))
        log(f"Resumed other media: {', '.join(to_resume)}")
    except Exception as e:
        log(f"Could not resume other media: {e}")


# Safety net: if the program exits any normal way (finish, Esc, quit, Ctrl+C,
# window close, or an uncaught error), make sure we un-pause whatever we paused.
# (A hard force-kill / taskkill /F can't run this — nothing can.)
atexit.register(resume_other_media)


def save_preferences():
    """Save current voice and speed back into config.json."""
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
        config["kokoro_voice"] = current_voice
        config["kokoro_speed"] = current_speed
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=4)
    except Exception:
        pass  # If config.json is somehow missing or broken, don't crash


def load_preferences():
    """Load voice and speed from config.json (already loaded at startup, but
    this updates the runtime globals in case they differ from the module-level constants)."""
    global current_voice, current_lang, current_speed
    current_voice = KOKORO_VOICE
    current_lang = KOKORO_VOICE[0]  # First character is accent letter ("a" or "b")
    current_speed = KOKORO_SPEED
    log(f"Voice: {current_voice}, Speed: {current_speed}x")


# ---------------------------------------------------------------------------
# Platform input layer (global hotkeys + synthetic "copy" keystroke)
# ---------------------------------------------------------------------------
# All platforms use `pynput`. These helpers keep the rest of the code
# platform-agnostic (they previously hid a Windows-only `keyboard` path).
_pynput_hotkeys = None   # pynput GlobalHotKeys listener (all platforms)
_kb_controller = None    # pynput Controller for synthesizing key presses

def _controller():
    """Lazily create the pynput keyboard Controller (all platforms)."""
    global _kb_controller
    if _kb_controller is None:
        _kb_controller = pynput_keyboard.Controller()
    return _kb_controller

def _combo_to_pynput(combo):
    """Convert a 'ctrl+alt+r' style combo to pynput's '<ctrl>+<alt>+r' syntax."""
    specials = {"ctrl", "alt", "shift", "cmd", "right", "left", "up", "down",
                "esc", "space", "enter", "tab"}
    return "+".join(f"<{p}>" if p in specials else p for p in combo.split("+"))

def _pretty_combo(combo):
    """Human-readable form of a combo, e.g. 'ctrl+cmd+r' -> 'Ctrl+Cmd+R'."""
    names = {"ctrl": "Ctrl", "alt": "Alt", "cmd": "Cmd", "shift": "Shift",
             "right": "Right", "left": "Left", "up": "Up", "down": "Down",
             "space": "Space", "enter": "Enter", "tab": "Tab", "esc": "Esc"}
    return "+".join(names.get(p, p.upper()) for p in combo.split("+"))

def register_hotkeys(specs, esc_callback):
    """Register global hotkeys. `specs` is a list of (combo, callback) pairs.
    `esc_callback` fires when Escape is pressed (it is passed one argument,
    which it may ignore — kept for backwards compatibility with the call site)."""
    global _pynput_hotkeys
    mapping = {_combo_to_pynput(combo): cb for combo, cb in specs}
    mapping["<esc>"] = lambda: esc_callback(None)
    _pynput_hotkeys = pynput_keyboard.GlobalHotKeys(mapping)
    _pynput_hotkeys.start()

def unregister_hotkeys():
    """Tear down all registered global hotkeys."""
    if _pynput_hotkeys is not None:
        _pynput_hotkeys.stop()

def release_modifiers():
    """Release the hotkey modifiers so they don't contaminate the synthetic
    copy shortcut. The user may still be physically holding them when the
    callback fires; we send synthetic key-ups so the OS sees a clean copy.
    On macOS we also release Cmd, because the hotkey itself uses Cmd and the
    copy we're about to send is Cmd+C — we want a fresh Cmd press for that."""
    mods = [pynput_keyboard.Key.ctrl, pynput_keyboard.Key.alt,
            pynput_keyboard.Key.shift]
    if IS_MAC:
        mods.append(pynput_keyboard.Key.cmd)
    for k in mods:
        try:
            _controller().release(k)
        except Exception:
            pass

def send_copy():
    """Simulate the platform copy shortcut: Cmd+C on macOS, Ctrl+C elsewhere."""
    if IS_WINDOWS:
        pyautogui.hotkey("ctrl", "c")
    else:
        mod = pynput_keyboard.Key.cmd if IS_MAC else pynput_keyboard.Key.ctrl
        c = _controller()
        with c.pressed(mod):
            c.press("c")
            c.release("c")


# ---------------------------------------------------------------------------
# Sound feedback
# ---------------------------------------------------------------------------
# Originally used winsound.Beep (Windows-only). Replaced with sine tones
# synthesized via numpy and played through sounddevice, so the exact same
# feedback works on Windows, macOS, and Linux.
def _tone(freq, ms, volume=0.2):
    """Play a single short sine-wave beep (blocking). Never raises.
    Refreshes PortAudio and retries once if the first attempt fails (the
    AirPods-reconnect case — see _refresh_audio_devices)."""
    try:
        sr = 44100
        n = int(sr * ms / 1000.0)
        if n <= 0:
            return
        t = np.arange(n, dtype=np.float32) / sr
        wave = (volume * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
        # Short fade in/out to avoid audible clicks at the edges.
        fade = min(int(sr * 0.005), n // 2)
        if fade > 0:
            env = np.ones(n, dtype=np.float32)
            env[:fade] = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            env[-fade:] = np.linspace(1.0, 0.0, fade, dtype=np.float32)
            wave *= env
        try:
            sd.play(wave, sr, blocking=True)
            sd.wait()
        except Exception:
            _refresh_audio_devices()
            sd.play(wave, sr, blocking=True)
            sd.wait()
    except Exception:
        pass  # A failed beep must never crash the tool.

def _beep_sequence(notes):
    """Play a list of (freq_hz, duration_ms) tones in a daemon thread."""
    def _run():
        for freq, ms in notes:
            _tone(freq, ms)
            time.sleep(0.02)
    threading.Thread(target=_run, daemon=True).start()

def play_start_sound():
    """Two quick rising tones — starting to speak."""
    _beep_sequence([(880, 80), (1100, 80)])

def play_done_sound():
    """Short high click — finished speaking."""
    _beep_sequence([(1200, 50)])

def play_stop_sound():
    """Descending tone — speech stopped by user."""
    _beep_sequence([(900, 80), (600, 80)])

def play_error_sound():
    """Quick double low-beep — something went wrong or no text found."""
    _beep_sequence([(200, 100), (200, 100)])

def play_ocr_ready_sound():
    """Three quick ascending tones — OCR overlay opened."""
    _beep_sequence([(700, 50), (900, 50), (1100, 50)])


# ---------------------------------------------------------------------------
# System tray icon
# ---------------------------------------------------------------------------
def create_icon_image(color):
    """Create a small colored rounded-square image for the tray icon (distinct from dictation tool's circles)."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # Rounded rectangle — visually different from the dictation tool's circle
    draw.rounded_rectangle([4, 4, size - 4, size - 4], radius=10, fill=color, outline=(40, 40, 40), width=2)
    # Draw a small speaker/sound icon in the center
    draw.polygon([(20, 24), (28, 24), (36, 16), (36, 48), (28, 40), (20, 40)], fill=(255, 255, 255, 180))
    return image

# Pre-build icon images so we don't recreate them every time
if TRAY_AVAILABLE:
    ICON_READY = create_icon_image((0, 150, 136))          # Teal
    ICON_SPEAKING = create_icon_image((156, 39, 176))       # Purple
    ICON_PROCESSING = create_icon_image((255, 152, 0))      # Orange
    ICON_ERROR = create_icon_image((120, 120, 120))          # Dark gray


def update_tray_icon(state):
    """Change the tray icon color. state: 'ready', 'speaking', 'processing', or 'error'."""
    if not TRAY_AVAILABLE or tray_icon is None:
        return
    icons = {
        "ready": ICON_READY,
        "speaking": ICON_SPEAKING,
        "processing": ICON_PROCESSING,
        "error": ICON_ERROR,
    }
    tray_icon.icon = icons.get(state, ICON_READY)
    labels = {
        "ready": "TTS Reader - Ready",
        "speaking": "TTS Reader - Speaking...",
        "processing": "TTS Reader - Processing...",
        "error": "TTS Reader - Error",
    }
    tray_icon.title = labels.get(state, "TTS Reader")


def quit_from_tray():
    """Quit the application from the tray menu."""
    global should_quit
    should_quit = True
    log("Quit requested from tray menu.")
    if tray_icon is not None:
        tray_icon.stop()

def change_voice(voice_id):
    """Switch to a different Kokoro voice. Reloads the pipeline if the accent changed."""
    global current_voice, current_lang, tts_engine_obj

    if voice_id == current_voice:
        return  # Already using this voice

    old_lang = current_lang
    new_lang = voice_id[0]  # First character is the accent letter ("a" or "b")

    current_voice = voice_id
    current_lang = new_lang
    log(f"Voice changed to: {voice_id}")
    save_preferences()

    # If the accent changed (e.g. American → British), we need to reload the
    # Kokoro pipeline because the accent is set when the pipeline is created.
    # Voice-only changes (same accent) take effect immediately — the voice is
    # passed each time we generate audio, so no reload needed.
    if new_lang != old_lang:
        log(f"Accent changed ({old_lang} → {new_lang}), reloading Kokoro pipeline...")
        def reload_pipeline():
            global tts_engine_obj
            try:
                from kokoro import KPipeline
                tts_engine_obj = KPipeline(lang_code=new_lang, repo_id="hexgrad/Kokoro-82M",
                                           device=KOKORO_DEVICE)
                log(f"Pipeline reloaded for accent '{new_lang}'.")
            except Exception as e:
                log(f"Failed to reload pipeline: {e}")
        threading.Thread(target=reload_pipeline, daemon=True).start()

    # Rebuild the tray menu so the checkmark moves to the new voice
    if tray_icon is not None:
        tray_icon.menu = build_tray_menu()


def build_tray_menu():
    """Build the right-click menu for the tray icon."""
    menu_items = [
        pystray.MenuItem("TTS Reader", None, enabled=False),
        pystray.Menu.SEPARATOR,
    ]

    # Add voice submenu only for Kokoro engine
    if TTS_ENGINE == "kokoro":
        # Build a submenu for each accent/gender group
        voice_submenus = []
        for group_name, voices in KOKORO_VOICES.items():
            voice_items = []
            for display_name, voice_id in voices:
                # Use a factory function to capture voice_id correctly in the closure.
                # Without this, all menu items would use the LAST voice_id from the loop
                # (a common Python gotcha with closures in loops).
                def make_action(vid):
                    return lambda: change_voice(vid)
                def make_checked(vid):
                    return lambda item: current_voice == vid

                voice_items.append(
                    pystray.MenuItem(
                        display_name,
                        make_action(voice_id),
                        checked=make_checked(voice_id),
                        radio=True,
                    )
                )
            voice_submenus.append(
                pystray.MenuItem(group_name, pystray.Menu(*voice_items))
            )

        menu_items.append(
            pystray.MenuItem("Voice", pystray.Menu(*voice_submenus))
        )
        menu_items.append(pystray.Menu.SEPARATOR)

    menu_items.append(pystray.MenuItem("Quit", lambda: quit_from_tray()))

    return pystray.Menu(*menu_items)

def start_tray_icon():
    """Start the system tray icon in a background thread."""
    global tray_icon
    tray_icon = pystray.Icon(
        name="tts_reader",
        icon=ICON_READY,
        title="TTS Reader - Ready",
        menu=build_tray_menu(),
    )
    tray_thread = threading.Thread(target=tray_icon.run, daemon=True)
    tray_thread.start()


# ---------------------------------------------------------------------------
# TTS engine loading
# ---------------------------------------------------------------------------
def load_tts_engine():
    """Load the configured TTS engine. Called once at startup."""
    global tts_engine_obj

    if TTS_ENGINE == "kokoro":
        log("Loading Kokoro TTS engine...")
        try:
            from kokoro import KPipeline
            tts_engine_obj = KPipeline(lang_code=current_lang, repo_id="hexgrad/Kokoro-82M",
                                       device=KOKORO_DEVICE)
            log(f"Kokoro loaded! Voice: {current_voice}, Device: {KOKORO_DEVICE}")
        except Exception as e:
            log(f"Failed to load Kokoro: {e}")
            log("Make sure kokoro and espeak-ng are installed (see README).")
            sys.exit(1)

    elif TTS_ENGINE == "piper":
        # Make sure the voice model exists, download if not
        ensure_piper_model()
        log("Loading Piper TTS engine...")
        try:
            from piper.voice import PiperVoice
            tts_engine_obj = PiperVoice.load(PIPER_MODEL)
            log(f"Piper loaded! Model: {PIPER_MODEL}")
            log(f"  Sample rate: {tts_engine_obj.config.sample_rate} Hz")
        except Exception as e:
            log(f"Failed to load Piper: {e}")
            log("Make sure piper-tts is installed (see README).")
            sys.exit(1)

    else:
        log(f"ERROR: Unknown TTS engine '{TTS_ENGINE}'")
        log("Set TTS_ENGINE to 'kokoro' or 'piper' at the top of this file.")
        sys.exit(1)


def ensure_piper_model():
    """Download the Piper voice model if it doesn't exist yet."""
    if os.path.exists(PIPER_MODEL):
        return

    log(f"Piper voice model not found at: {PIPER_MODEL}")
    log("Downloading voice model from HuggingFace (this only happens once)...")

    import urllib.request

    # Create the voices folder if needed
    os.makedirs(os.path.dirname(PIPER_MODEL), exist_ok=True)

    # The model filename (e.g. "en_US-lessac-medium.onnx")
    model_filename = os.path.basename(PIPER_MODEL)
    config_filename = model_filename + ".json"

    # Download the .onnx model file
    model_url = f"{PIPER_DOWNLOAD_URL}/{model_filename}"
    log(f"  Downloading {model_filename}...")
    urllib.request.urlretrieve(model_url, PIPER_MODEL)

    # Download the .onnx.json config file
    config_url = f"{PIPER_DOWNLOAD_URL}/{config_filename}"
    config_path = PIPER_MODEL + ".json"
    log(f"  Downloading {config_filename}...")
    urllib.request.urlretrieve(config_url, config_path)

    log("Voice model downloaded!")


# ---------------------------------------------------------------------------
# Text cleanup (prepare text for natural-sounding speech)
# ---------------------------------------------------------------------------
import re

def clean_text_for_speech(text):
    """Clean up text so line breaks become natural pauses when spoken.

    The TTS engine treats line breaks as just a space, so text like:
        "Line one\\nLine two"
    gets read as one long sentence. This function adds punctuation at
    line endings so the TTS engine pauses naturally.
    """
    # Split into lines, keeping track of blank lines (paragraph breaks)
    lines = text.splitlines()

    # Characters that already signal a pause to the TTS engine
    pause_punctuation = ".!?;:,"

    cleaned_parts = []
    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip blank lines but mark a paragraph break
        if not stripped:
            # Only add a paragraph break if we already have some text
            if cleaned_parts:
                cleaned_parts.append("\n\n")
            continue

        # If the line doesn't end with punctuation, add a period
        if stripped and stripped[-1] not in pause_punctuation:
            stripped += "."

        cleaned_parts.append(stripped)

    # Join everything with spaces (paragraph breaks are already "\n\n")
    result = ""
    for part in cleaned_parts:
        if part == "\n\n":
            result += "\n\n"
        elif result and not result.endswith("\n"):
            result += " " + part
        else:
            result += part

    return result.strip()


# ---------------------------------------------------------------------------
# Time-stretching (speed up audio without changing pitch or losing clarity)
# ---------------------------------------------------------------------------
def time_stretch_wsola(audio, rate, sample_rate):
    """Speed up or slow down audio using WSOLA (Waveform Similarity Overlap-Add).

    Unlike the phase vocoder method (used by librosa), WSOLA works directly
    on the sound wave rather than in the frequency domain. This preserves the
    natural timbre of the voice — no hollow or metallic artifacts.

    How it works:
    1. Split the audio into overlapping windows (50ms each)
    2. For each window, search nearby for the best splice point where the
       waveform naturally lines up (using cross-correlation)
    3. Overlap-add the windows at closer spacing (for speedup) or wider
       spacing (for slowdown)

    The result is faster speech that sounds natural — same pitch, same voice
    quality, just less time between syllables.
    """
    if rate == 1.0 or len(audio) < 1024:
        return audio

    # Parameters tuned for speech
    window_size = int(sample_rate * 0.05)       # 50ms windows — matches typical speech patterns
    window_size += window_size % 2              # Make even for clean math
    seek_size = int(sample_rate * 0.015)        # Search ±15ms for best splice point

    hop_out = window_size // 2                  # Output spacing (synthesis hop)
    hop_in = int(hop_out * rate)                # Input spacing (analysis hop)

    # Hann window for smooth crossfading between overlapping segments
    hann = np.hanning(window_size).astype(np.float32)

    output_len = int(len(audio) / rate) + window_size
    output = np.zeros(output_len, dtype=np.float32)
    norm = np.zeros(output_len, dtype=np.float32)

    in_pos = 0
    out_pos = 0

    while in_pos + window_size < len(audio) and out_pos + window_size < output_len:
        best_pos = in_pos

        # For frames after the first, search for the best overlap point
        if out_pos > 0:
            search_start = max(0, in_pos - seek_size)
            search_end = min(len(audio) - window_size, in_pos + seek_size)

            if search_end > search_start:
                # Compare candidates against what's already in the output
                ref = output[out_pos:out_pos + window_size]
                search_audio = audio[search_start:search_end + window_size]

                # Cross-correlation finds where the waveforms line up best
                if len(search_audio) >= len(ref):
                    corr = np.correlate(search_audio, ref, mode='valid')
                    best_pos = search_start + np.argmax(corr)

        # Overlap-add this window into the output
        frame = audio[best_pos:best_pos + window_size] * hann
        output[out_pos:out_pos + window_size] += frame
        norm[out_pos:out_pos + window_size] += hann

        # Advance by the fixed stride, NOT from best_pos.
        # If we used "in_pos = best_pos + hop_in", the search adjustment
        # would accumulate over many iterations, causing us to race through
        # the input too fast and miss the end.
        in_pos += hop_in
        out_pos += hop_out

    # Append any remaining audio that the loop didn't process
    # (the loop stops when there isn't a full window left, so the tail gets lost)
    remaining = audio[in_pos:]
    if len(remaining) > 0 and out_pos < output_len:
        copy_len = min(len(remaining), output_len - out_pos)
        output[out_pos:out_pos + copy_len] += remaining[:copy_len]
        norm[out_pos:out_pos + copy_len] += 1.0

    # Normalize to prevent amplitude changes from the overlap-add
    mask = norm > 1e-8
    output[mask] /= norm[mask]

    # Trim to the last sample that actually has audio content
    # (rather than guessing the length, just find where we actually wrote audio)
    nonzero = np.flatnonzero(norm > 1e-8)
    if len(nonzero) > 0:
        return output[:nonzero[-1] + 1]
    return output[:0]


# ---------------------------------------------------------------------------
# Audio chunk generators (one per engine, same output format)
# ---------------------------------------------------------------------------
def generate_audio_chunks(text):
    """Yield float32 numpy arrays of audio, one chunk at a time.

    Audio is generated at normal speed (1.0x), then time-stretched to
    the user's chosen speed using WSOLA. This preserves all syllables,
    keeps the original pitch, and maintains natural voice quality.
    """
    sample_rate = get_sample_rate()

    if TTS_ENGINE == "kokoro":
        for gs, ps, audio in tts_engine_obj(text, voice=current_voice, speed=1.0):
            if hasattr(audio, "numpy"):
                audio = audio.numpy()
            if current_speed != 1.0:
                audio = time_stretch_wsola(audio, current_speed, sample_rate)
            yield audio
    elif TTS_ENGINE == "piper":
        for chunk in tts_engine_obj.synthesize(text):
            int_data = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
            audio = int_data.astype(np.float32) / 32768.0
            if current_speed != 1.0:
                audio = time_stretch_wsola(audio, current_speed, sample_rate)
            yield audio


def get_sample_rate():
    """Return the sample rate for the current engine."""
    if TTS_ENGINE == "kokoro":
        return KOKORO_SAMPLE_RATE
    elif TTS_ENGINE == "piper":
        return tts_engine_obj.config.sample_rate


# ---------------------------------------------------------------------------
# Audio playback (streaming — plays chunks as they arrive)
# ---------------------------------------------------------------------------
class StopSpeaking(Exception):
    """Raised when the user presses Escape to stop speech."""
    pass


# How many samples to send to the speaker at a time.
# Smaller = more responsive to Escape, but slightly more CPU overhead.
# 0.3 seconds at 24kHz = 7200 samples — Escape responds within ~0.3s.
PLAYBACK_CHUNK_SAMPLES = 7200


def _refresh_audio_devices():
    """Tear down and re-initialize PortAudio so it picks up the *current* set
    of audio devices. PortAudio enumerates devices once at startup and caches
    them; if the device set changes mid-process (most commonly on macOS when a
    Bluetooth device — AirPods, headphones — disconnects and reconnects) those
    cached handles go stale and stream-open fails with CoreAudio -10851 /
    PortAudio -9986. Reinitializing is the supported recovery."""
    try:
        sd._terminate()
    except Exception:
        pass
    try:
        sd._initialize()
    except Exception:
        pass


def _open_output_stream(sample_rate):
    """Open a sounddevice OutputStream, refreshing PortAudio's device list and
    retrying once if the first attempt fails (typical after a Bluetooth
    disconnect/reconnect on macOS — see _refresh_audio_devices)."""
    try:
        s = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32")
        s.start()
        return s
    except Exception as first:
        log(f"Audio device open failed ({first.__class__.__name__}: {first}); "
            f"refreshing audio devices and retrying...")
        _refresh_audio_devices()
        s = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32")
        s.start()
        return s


def play_audio_stream(chunks_generator, sample_rate):
    """Play audio chunks through the speakers as they arrive from the TTS engine.

    Uses a background thread to generate the next chunk while the current one
    is playing, so there's no gap between sentences.
    """
    global is_speaking
    is_speaking = True

    # The queue lets the TTS engine work ahead — while one sentence is playing
    # through the speakers, the next sentence is already being generated.
    # maxsize=5 means up to 5 sentences can be pre-generated and waiting.
    audio_queue = queue.Queue(maxsize=5)
    sentinel = object()  # Special marker meaning "no more audio"

    def producer():
        """Generate audio chunks in a background thread."""
        try:
            for chunk in chunks_generator:
                if not is_speaking:
                    return
                audio_queue.put(chunk)
        except Exception:
            pass
        finally:
            audio_queue.put(sentinel)

    threading.Thread(target=producer, daemon=True).start()

    # Open the speaker. _open_output_stream auto-recovers from stale PortAudio
    # device caches (the AirPods reconnect case on macOS).
    stream = _open_output_stream(sample_rate)

    try:
        while True:
            # Get the next chunk. Timeout lets us check for Escape while waiting.
            try:
                chunk = audio_queue.get(timeout=0.1)
            except queue.Empty:
                if not is_speaking:
                    raise StopSpeaking()
                continue

            if chunk is sentinel:
                break

            # Break each TTS chunk into small pieces so Escape is responsive.
            offset = 0
            while offset < len(chunk):
                if not is_speaking:
                    raise StopSpeaking()
                end = offset + PLAYBACK_CHUNK_SAMPLES
                stream.write(chunk[offset:end])
                offset = end

        # Write a short block of silence to flush the audio buffer.
        # Without this, stream.stop() cuts off audio still in the buffer,
        # clipping the last words.
        silence = np.zeros(PLAYBACK_CHUNK_SAMPLES, dtype=np.float32)
        stream.write(silence)

    except StopSpeaking:
        raise
    finally:
        stream.stop()
        stream.close()
        is_speaking = False


# ---------------------------------------------------------------------------
# Main speak function
# ---------------------------------------------------------------------------
def speak_text(text):
    """Convert text to speech and play it. Runs in a background thread."""
    global is_processing, is_speaking

    is_processing = True
    was_stopped = False
    try:
        update_tray_icon("speaking")
        pause_other_media()   # pause Spotify/video/etc. while we speak
        play_start_sound()
        log(f'Speaking: "{text[:80]}{"..." if len(text) > 80 else ""}"')

        sample_rate = get_sample_rate()
        chunks = generate_audio_chunks(text)
        play_audio_stream(chunks, sample_rate)
        # play_audio_stream sets is_speaking=False when done, but we need
        # to know if it was interrupted or finished naturally
        # If it was interrupted, is_speaking was set False by on_stop()
        # before play_audio_stream's finally block ran.
        # We can't distinguish easily, so we use a separate flag.

    except StopSpeaking:
        was_stopped = True
        log("Speech interrupted.")

    except Exception as e:
        log(f"TTS error: {e}")
        write_error_log(e)
        play_error_sound()
        update_tray_icon("error")
        time.sleep(2)

    else:
        if not was_stopped:
            play_done_sound()
            log("Finished speaking.")

    finally:
        is_processing = False
        is_speaking = False
        resume_other_media()  # let Spotify/video/etc. carry on
        update_tray_icon("ready")
        log("(Ready for next command)")


# ---------------------------------------------------------------------------
# Mode 1: Read selected text
# ---------------------------------------------------------------------------
def on_read_selected():
    """Hotkey handler: grab the currently selected text and speak it."""
    log(">>> Hotkey callback fired!")  # First thing — confirms the keypress was detected
    try:
        if is_speaking or is_processing:
            log(f"(Hotkey ignored — is_speaking={is_speaking}, is_processing={is_processing})")
            return

        log("Grabbing selected text...")

        # Release Ctrl and Alt so they don't interfere with the copy shortcut
        # we're about to simulate. The user's fingers may still be on these
        # keys from the Ctrl+Alt+R combo. Without this, the OS might see
        # Ctrl+Alt+C instead of a clean copy.
        release_modifiers()
        log("  Released modifier keys")

        # Save the current clipboard so we can restore it after
        try:
            old_clipboard = pyperclip.paste()
        except Exception:
            old_clipboard = ""
        log("  Saved clipboard")

        # Clear the clipboard first — this way we can tell if the copy actually
        # grabbed something new, vs. just reading whatever was already there.
        try:
            pyperclip.copy("")
        except Exception:
            pass

        # Simulate the copy shortcut (Cmd+C on macOS, Ctrl+C elsewhere) to
        # copy whatever is selected.
        log("  Simulating copy shortcut...")
        send_copy()
        time.sleep(0.25)  # Wait for the clipboard to update (slightly longer for safety)

        # Read the copied text
        try:
            text = pyperclip.paste()
        except Exception:
            text = ""
        log(f'  Clipboard after copy: "{text[:80] if text else ""}"')

        # Restore the original clipboard
        try:
            pyperclip.copy(old_clipboard)
        except Exception:
            pass

        # Check if we got anything useful
        if not text or not text.strip():
            log("No text copied (clipboard empty after copy). If macOS printed "
                "'process is not trusted', grant this app Accessibility permission.")
            play_error_sound()
            return

        text = clean_text_for_speech(text)
        log(f"Got text ({len(text)} chars)")

        # Speak it in a background thread
        threading.Thread(target=speak_text, args=(text,), daemon=True).start()

    except Exception as e:
        log(f"ERROR in on_read_selected: {e}")
        write_error_log(e)
        play_error_sound()


# ---------------------------------------------------------------------------
# Mode 2: OCR screen region
# ---------------------------------------------------------------------------
def on_ocr_region():
    """Hotkey handler: signal the main loop to open the region selector."""
    global ocr_requested
    try:
        if is_speaking or is_processing:
            log("(OCR hotkey ignored — already speaking or processing)")
            return
        log("Ctrl+Alt+O pressed — opening region selector...")
        ocr_requested = True
    except Exception as e:
        log(f"ERROR in on_ocr_region: {e}")
        write_error_log(e)


_overlay_root = None  # Module-level reference prevents premature garbage collection

def open_region_selector():
    """Open a fullscreen overlay where the user drags a rectangle to capture."""
    global _overlay_root
    play_ocr_ready_sound()

    # If a previous overlay root exists, destroy it now (on the main thread)
    if _overlay_root is not None:
        try:
            _overlay_root.destroy()
        except Exception:
            pass

    root = tk.Tk()
    _overlay_root = root  # Keep a reference so GC doesn't clean it up on a random thread

    # Span ALL monitors, not just the primary one. tkinter's "-fullscreen"
    # only covers the primary monitor, so we size the window to the entire
    # virtual screen (the bounding box of every monitor combined).
    #
    # mss.monitors[0] is exactly that bounding box on every platform, and it
    # correctly handles monitors positioned to the left of / above the primary
    # (which start at negative coordinates). This replaces the old Windows-only
    # GetSystemMetrics path and works identically on Windows, macOS, and Linux.
    with mss.mss() as _sct:
        vmon = _sct.monitors[0]
    screen_left = vmon["left"]
    screen_top = vmon["top"]
    total_width = vmon["width"]
    total_height = vmon["height"]

    root.overrideredirect(True)  # Remove window borders/title bar
    root.geometry(f"{total_width}x{total_height}+{screen_left}+{screen_top}")
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.3)          # 30% opacity — screen looks dimmed
    root.configure(cursor="crosshair")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    # State for drag tracking
    drag_state = {"start_x": None, "start_y": None, "rect_id": None}
    overlay_closed = False  # Prevents double-closing from multiple handlers

    def close_overlay():
        """Safely close the overlay (only runs once, always on tkinter's thread).

        We hide the window and tell mainloop to stop, but we do NOT destroy
        the window here. Destruction happens after mainloop exits, on the
        main thread, to avoid 'Tcl_AsyncDelete: async handler deleted by
        the wrong thread' errors.
        """
        nonlocal overlay_closed
        if overlay_closed:
            return
        overlay_closed = True
        root.withdraw()  # Hide the window immediately (so it's not in screenshots)
        root.quit()      # Tell mainloop to stop (actual destroy happens after mainloop exits)

    def on_mouse_down(event):
        drag_state["start_x"] = event.x
        drag_state["start_y"] = event.y

    def on_mouse_drag(event):
        if drag_state["rect_id"]:
            canvas.delete(drag_state["rect_id"])
        drag_state["rect_id"] = canvas.create_rectangle(
            drag_state["start_x"], drag_state["start_y"],
            event.x, event.y,
            outline="red", width=3,
        )

    def on_mouse_up(event):
        # Calculate the rectangle coordinates (relative to the overlay window)
        x1 = min(drag_state["start_x"], event.x)
        y1 = min(drag_state["start_y"], event.y)
        x2 = max(drag_state["start_x"], event.x)
        y2 = max(drag_state["start_y"], event.y)

        # Convert to absolute screen coordinates (needed for pyautogui screenshot)
        abs_x1 = x1 + screen_left
        abs_y1 = y1 + screen_top
        abs_x2 = x2 + screen_left
        abs_y2 = y2 + screen_top

        # Close the overlay first (so it's not in the screenshot)
        close_overlay()

        # Skip if the rectangle is too small (accidental click)
        if (abs_x2 - abs_x1) < 10 or (abs_y2 - abs_y1) < 10:
            log("Selection too small, cancelled.")
            play_error_sound()
            return

        # Small delay so the overlay fully disappears before screenshot
        time.sleep(0.2)

        # Take a screenshot of just that region.
        # We use mss instead of pyautogui.screenshot() because pyautogui only
        # captures the primary monitor. mss uses the Windows GDI API directly
        # and works on all monitors, including ones with negative coordinates
        # (e.g. a monitor positioned to the left of the primary).
        width = abs_x2 - abs_x1
        height = abs_y2 - abs_y1
        with mss.mss() as sct:
            region = {"left": abs_x1, "top": abs_y1, "width": width, "height": height}
            raw = sct.grab(region)
            # Convert from mss's BGRA format to a PIL RGB Image (which winocr expects)
            screenshot = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        # OCR and speak in a background thread
        threading.Thread(target=ocr_and_speak, args=(screenshot,), daemon=True).start()

    def on_escape(event):
        close_overlay()
        log("OCR capture cancelled.")

    canvas.bind("<ButtonPress-1>", on_mouse_down)
    canvas.bind("<B1-Motion>", on_mouse_drag)
    canvas.bind("<ButtonRelease-1>", on_mouse_up)
    root.bind("<Escape>", on_escape)
    # Force keyboard focus onto the overlay so its <Escape> binding reliably
    # catches the keypress on every platform. overrideredirect/topmost windows
    # don't always get focus automatically — this is why Windows previously
    # needed a separate global Esc hook (now removed). The global pynput Esc
    # hotkey stays registered, but during OCR it's a no-op (nothing is speaking),
    # so this tkinter binding is what actually cancels the capture.
    root.focus_force()

    root.mainloop()

    # Do NOT destroy root here. The _overlay_root reference keeps it alive,
    # preventing garbage collection on a random thread (which causes
    # "Tcl_AsyncDelete: async handler deleted by the wrong thread").
    # It gets destroyed on the main thread at the start of the NEXT OCR capture.


def run_ocr(pil_image):
    """Recognize text in a PIL image using the platform's OCR engine.

    Windows: winocr (Windows.Media.Ocr).
    macOS:   Apple's Vision framework (built in, high quality, no extra binary).
    Linux:   Tesseract via pytesseract (requires the `tesseract` binary).
    """
    if IS_WINDOWS:
        from winocr import recognize_pil_sync
        return recognize_pil_sync(pil_image, lang=OCR_LANGUAGE)["text"].strip()
    if IS_MAC:
        return _macos_vision_ocr(pil_image)
    import pytesseract  # Linux
    return pytesseract.image_to_string(pil_image).strip()


def _macos_vision_ocr(pil_image):
    """OCR via Apple's Vision framework (VNRecognizeTextRequest)."""
    import io
    import Quartz
    import Vision
    from Foundation import NSData

    # Hand the image to Vision as PNG bytes -> CGImage.
    buf = io.BytesIO()
    pil_image.convert("RGB").save(buf, format="PNG")
    png = buf.getvalue()
    data = NSData.dataWithBytes_length_(png, len(png))
    src = Quartz.CGImageSourceCreateWithData(data, None)
    if src is None:
        return ""
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(1)          # 1 = accurate, 0 = fast
    request.setUsesLanguageCorrection_(True)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
    handler.performRequests_error_([request], None)

    lines = []
    for observation in (request.results() or []):
        candidates = observation.topCandidates_(1)
        if candidates and len(candidates) > 0:
            lines.append(candidates[0].string())
    return "\n".join(lines).strip()


def ocr_and_speak(screenshot_image):
    """Run OCR on a screenshot image and speak the result."""
    global is_processing

    is_processing = True
    update_tray_icon("processing")
    log("Running OCR on captured region...")

    try:
        text = run_ocr(screenshot_image).strip()

        if not text:
            log("OCR found no text in the selected region.")
            play_error_sound()
            is_processing = False
            update_tray_icon("ready")
            return

        text = clean_text_for_speech(text)
        log(f'OCR result: "{text[:80]}{"..." if len(text) > 80 else ""}"')
        speak_text(text)

    except Exception as e:
        log(f"OCR error: {e}")
        write_error_log(e)
        play_error_sound()
        is_processing = False
        update_tray_icon("ready")


# ---------------------------------------------------------------------------
# Stop speaking
# ---------------------------------------------------------------------------
def on_stop(event):
    """Hotkey handler: stop speaking immediately."""
    global is_speaking
    log(">>> Esc pressed!")  # Always log so we know the Esc hotkey is being detected
    if is_speaking:
        is_speaking = False
        play_stop_sound()
        log("Speech stopped by user.")


# ---------------------------------------------------------------------------
# Speed control
# ---------------------------------------------------------------------------
SPEED_MIN = 0.5    # Slowest allowed speed
SPEED_MAX = 3.0    # Fastest allowed speed
SPEED_STEP = 0.25  # How much each press changes the speed

def on_speed_up():
    """Hotkey handler: increase speech speed."""
    global current_speed
    if current_speed < SPEED_MAX:
        current_speed = round(current_speed + SPEED_STEP, 2)
        log(f"Speed: {current_speed}x")
        save_preferences()
        _beep_sequence([(1000 + int(current_speed * 200), 50)])  # Higher pitch = faster
    else:
        log(f"Speed: {current_speed}x (already at maximum)")

def on_speed_down():
    """Hotkey handler: decrease speech speed."""
    global current_speed
    if current_speed > SPEED_MIN:
        current_speed = round(current_speed - SPEED_STEP, 2)
        log(f"Speed: {current_speed}x")
        save_preferences()
        _beep_sequence([(1000 + int(current_speed * 200), 50)])  # Lower pitch = slower
    else:
        log(f"Speed: {current_speed}x (already at minimum)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global should_quit, ocr_requested

    print("=" * 50)
    print("  Text-to-Speech Reader Tool")
    print("=" * 50)
    print()

    # Load saved preferences (voice, speed) before starting the engine
    load_preferences()

    # Load the TTS engine
    load_tts_engine()
    print()

    # Start the system tray icon.
    # On macOS the tray is intentionally disabled: pystray's AppKit backend and
    # tkinter both need the main thread's run loop and can't share it, and the
    # OCR overlay needs that main thread. Quit with the quit hotkey (or Ctrl+C
    # in this terminal) and pick a voice by editing config.json.
    tray_enabled = TRAY_AVAILABLE and not IS_MAC
    if tray_enabled:
        start_tray_icon()
        log("System tray icon started (look near your clock).")
    elif IS_MAC:
        log(f"Tray disabled on macOS — quit with {_pretty_combo(HOTKEY_QUIT)} or "
            f"Ctrl+C; set voice in config.json.")
    else:
        log("Running without tray icon.")

    # Print controls (reflect the actual configured hotkeys)
    print(f"  {_pretty_combo(HOTKEY_READ):<16} read selected text aloud")
    print(f"  {_pretty_combo(HOTKEY_OCR):<16} OCR a screen region")
    print(f"  {_pretty_combo(HOTKEY_SPEED_UP):<16} speed up")
    print(f"  {_pretty_combo(HOTKEY_SPEED_DOWN):<16} slow down")
    print(f"  {_pretty_combo(HOTKEY_QUIT):<16} quit")
    print(f"  {'Esc':<16} stop speaking")
    if not IS_MAC and TRAY_AVAILABLE:
        print(f"  {'Tray':<16} right-click to quit or change voice")
    print()
    log(f"Ready! Engine: {TTS_ENGINE}, Speed: {current_speed}x")
    print()

    # Register global hotkeys (via the platform input layer).
    register_hotkeys(
        [
            (HOTKEY_READ, on_read_selected),
            (HOTKEY_OCR, on_ocr_region),
            (HOTKEY_SPEED_UP, on_speed_up),
            (HOTKEY_SPEED_DOWN, on_speed_down),
            (HOTKEY_QUIT, quit_from_tray),
        ],
        on_stop,
    )
    log(f"Hotkeys registered: {HOTKEY_READ}, {HOTKEY_OCR}, {HOTKEY_SPEED_UP}, "
        f"{HOTKEY_SPEED_DOWN}, {HOTKEY_QUIT}, Escape")

    # Main loop
    try:
        while not should_quit:
            # Check if OCR region capture was requested
            if ocr_requested:
                ocr_requested = False
                open_region_selector()  # Runs tkinter on the main thread
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass

    # Cleanup
    unregister_hotkeys()
    resume_other_media()  # if we quit mid-speech, let other apps resume
    if tray_icon is not None:
        try:
            tray_icon.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
