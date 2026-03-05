"""
Text-to-Speech Reader Tool
===========================
Select text and press Ctrl+Alt+R to hear it read aloud.
Or press Ctrl+Alt+O to OCR a screen region and read it.
Press Escape to stop speaking.

Usage:  python tts_reader.py
        (Run your terminal as Administrator for global hotkey support)

Controls:
    Ctrl+Alt+R  - Read selected text aloud
    Ctrl+Alt+O  - OCR a screen region, then read aloud
    Escape      - Stop speaking
    Tray        - Right-click the system tray icon for options and quit
"""

import os
import sys
import json
import queue
import time
import logging
import warnings
import threading
import winsound
import tkinter as tk

# Suppress noisy warnings from libraries before importing them
warnings.filterwarnings("ignore", category=UserWarning, module="torch")
warnings.filterwarnings("ignore", category=FutureWarning, module="torch")
warnings.filterwarnings("ignore", category=UserWarning, module="huggingface_hub")
logging.getLogger("transformers").setLevel(logging.ERROR)

import numpy as np
import sounddevice as sd
import keyboard
import pyperclip
import pyautogui

# Try to import system tray libraries (optional — script works without them)
try:
    from PIL import Image, ImageDraw
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

# Default values (used when config.json is missing or incomplete)
DEFAULTS = {
    "tts_engine": "kokoro",
    "kokoro_voice": "af_heart",
    "kokoro_speed": 1.0,
    "piper_model": "voices/en_US-lessac-high.onnx",
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
PIPER_MODEL = _config["piper_model"]

# Hotkeys (same on all PCs)
HOTKEY_READ = "ctrl+alt+r"     # Read selected text aloud
HOTKEY_OCR = "ctrl+alt+o"      # OCR a screen region, then read aloud

# --- Other settings (not in config.json) ---
KOKORO_LANG = "a"              # "a" = American English, "b" = British English
KOKORO_SAMPLE_RATE = 24000     # Kokoro outputs audio at 24,000 Hz (don't change)
PIPER_DOWNLOAD_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/high"
OCR_LANGUAGE = "en"            # Language for Windows OCR


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
is_speaking = False            # Is audio currently playing?
is_processing = False          # Is TTS generation or OCR running?
tts_engine_obj = None          # The loaded TTS model (Kokoro pipeline or Piper voice)
tray_icon = None               # System tray icon
should_quit = False            # Signal to exit the program
ocr_requested = False          # Flag: main loop should open the region selector


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------
def log(message):
    """Print a timestamped message to the console."""
    timestamp = time.strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")


# ---------------------------------------------------------------------------
# Sound feedback
# ---------------------------------------------------------------------------
def play_start_sound():
    """Two quick rising tones — starting to speak."""
    def _beep():
        winsound.Beep(880, 80)
        time.sleep(0.03)
        winsound.Beep(1100, 80)
    threading.Thread(target=_beep, daemon=True).start()

def play_done_sound():
    """Short high click — finished speaking."""
    threading.Thread(target=lambda: winsound.Beep(1200, 50), daemon=True).start()

def play_stop_sound():
    """Descending tone — speech stopped by user."""
    def _beep():
        winsound.Beep(900, 80)
        time.sleep(0.03)
        winsound.Beep(600, 80)
    threading.Thread(target=_beep, daemon=True).start()

def play_error_sound():
    """Quick double low-beep — something went wrong or no text found."""
    def _beep():
        winsound.Beep(200, 100)
        time.sleep(0.05)
        winsound.Beep(200, 100)
    threading.Thread(target=_beep, daemon=True).start()

def play_ocr_ready_sound():
    """Three quick ascending tones — OCR overlay opened."""
    def _beep():
        winsound.Beep(700, 50)
        time.sleep(0.03)
        winsound.Beep(900, 50)
        time.sleep(0.03)
        winsound.Beep(1100, 50)
    threading.Thread(target=_beep, daemon=True).start()


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

def build_tray_menu():
    """Build the right-click menu for the tray icon."""
    return pystray.Menu(
        pystray.MenuItem("TTS Reader", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda: quit_from_tray()),
    )

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
            tts_engine_obj = KPipeline(lang_code=KOKORO_LANG, repo_id="hexgrad/Kokoro-82M")
            log(f"Kokoro loaded! Voice: {KOKORO_VOICE}")
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
# Audio chunk generators (one per engine, same output format)
# ---------------------------------------------------------------------------
def generate_audio_chunks(text):
    """Yield float32 numpy arrays of audio, one chunk at a time."""
    if TTS_ENGINE == "kokoro":
        for gs, ps, audio in tts_engine_obj(text, voice=KOKORO_VOICE, speed=KOKORO_SPEED):
            # Kokoro returns a torch tensor — convert to numpy for sounddevice
            if hasattr(audio, "numpy"):
                audio = audio.numpy()
            yield audio
    elif TTS_ENGINE == "piper":
        for chunk in tts_engine_obj.synthesize(text):
            int_data = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
            yield int_data.astype(np.float32) / 32768.0


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
    # Memory cost is tiny (~2 MB max), and the extra buffer covers short
    # sentences or bullet points where playback outpaces generation.
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

    stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32")
    stream.start()

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
        import traceback
        traceback.print_exc()
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

        # Release Ctrl and Alt so they don't interfere with the Ctrl+C we're
        # about to simulate. The user's fingers may still be on these keys
        # from the Ctrl+Alt+R hotkey combo. Without this, the OS might see
        # Ctrl+Alt+C instead of Ctrl+C, which isn't a copy shortcut.
        keyboard.release("ctrl")
        keyboard.release("alt")
        log("  Released modifier keys")

        # Save the current clipboard so we can restore it after
        try:
            old_clipboard = pyperclip.paste()
        except Exception:
            old_clipboard = ""
        log("  Saved clipboard")

        # Clear the clipboard first — this way we can tell if Ctrl+C actually copied
        # something new, vs. just reading whatever was already on the clipboard
        try:
            pyperclip.copy("")
        except Exception:
            pass

        # Simulate Ctrl+C to copy whatever is selected
        log("  Simulating Ctrl+C...")
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.25)  # Wait for the clipboard to update (slightly longer for safety)

        # Read the copied text
        try:
            text = pyperclip.paste()
        except Exception:
            text = ""
        log(f'  Clipboard after Ctrl+C: "{text[:80] if text else ""}"')

        # Restore the original clipboard
        try:
            pyperclip.copy(old_clipboard)
        except Exception:
            pass

        # Check if we got anything useful
        if not text or not text.strip():
            log("No text selected (clipboard was empty after Ctrl+C).")
            play_error_sound()
            return

        text = text.strip()
        log(f"Got text ({len(text)} chars)")

        # Speak it in a background thread
        threading.Thread(target=speak_text, args=(text,), daemon=True).start()

    except Exception as e:
        log(f"ERROR in on_read_selected: {e}")
        import traceback
        traceback.print_exc()
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
        import traceback
        traceback.print_exc()


def open_region_selector():
    """Open a fullscreen overlay where the user drags a rectangle to capture."""
    play_ocr_ready_sound()

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.3)          # 30% opacity — screen looks dimmed
    root.configure(cursor="crosshair")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    # State for drag tracking
    drag_state = {"start_x": None, "start_y": None, "rect_id": None}

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
        # Calculate the rectangle coordinates
        x1 = min(drag_state["start_x"], event.x)
        y1 = min(drag_state["start_y"], event.y)
        x2 = max(drag_state["start_x"], event.x)
        y2 = max(drag_state["start_y"], event.y)

        # Close the overlay first (so it's not in the screenshot)
        root.destroy()

        # Skip if the rectangle is too small (accidental click)
        if (x2 - x1) < 10 or (y2 - y1) < 10:
            log("Selection too small, cancelled.")
            play_error_sound()
            return

        # Small delay so the overlay fully disappears before screenshot
        time.sleep(0.2)

        # Take a screenshot of just that region
        screenshot = pyautogui.screenshot(region=(x1, y1, x2 - x1, y2 - y1))

        # OCR and speak in a background thread
        threading.Thread(target=ocr_and_speak, args=(screenshot,), daemon=True).start()

    def on_escape(event):
        root.destroy()
        log("OCR capture cancelled.")

    canvas.bind("<ButtonPress-1>", on_mouse_down)
    canvas.bind("<B1-Motion>", on_mouse_drag)
    canvas.bind("<ButtonRelease-1>", on_mouse_up)
    root.bind("<Escape>", on_escape)

    root.mainloop()


def ocr_and_speak(screenshot_image):
    """Run OCR on a screenshot image and speak the result."""
    global is_processing

    is_processing = True
    update_tray_icon("processing")
    log("Running OCR on captured region...")

    try:
        from winocr import recognize_pil_sync
        result = recognize_pil_sync(screenshot_image, lang=OCR_LANGUAGE)
        text = result["text"].strip()

        if not text:
            log("OCR found no text in the selected region.")
            play_error_sound()
            is_processing = False
            update_tray_icon("ready")
            return

        log(f'OCR result: "{text[:80]}{"..." if len(text) > 80 else ""}"')
        speak_text(text)

    except Exception as e:
        log(f"OCR error: {e}")
        play_error_sound()
        is_processing = False
        update_tray_icon("ready")


# ---------------------------------------------------------------------------
# Stop speaking
# ---------------------------------------------------------------------------
def on_stop(event):
    """Hotkey handler: stop speaking immediately."""
    global is_speaking
    log(">>> Esc pressed!")  # Always log so we know the keyboard library is working
    if is_speaking:
        is_speaking = False
        play_stop_sound()
        log("Speech stopped by user.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global should_quit, ocr_requested

    print("=" * 50)
    print("  Text-to-Speech Reader Tool")
    print("=" * 50)
    print()

    # Load the TTS engine
    load_tts_engine()
    print()

    # Start the system tray icon
    if TRAY_AVAILABLE:
        start_tray_icon()
        log("System tray icon started (look near your clock).")
    else:
        log("Running without tray icon.")

    # Print controls
    print(f"  Press  Ctrl+Alt+R  to read selected text aloud")
    print(f"  Press  Ctrl+Alt+O  to OCR a screen region")
    print(f"  Press  Escape      to stop speaking")
    print(f"  Right-click tray icon to quit")
    print()
    log(f"Ready! Engine: {TTS_ENGINE}")
    print()

    # Register hotkeys
    # trigger_on_release=True means the callback fires when the last key in
    # the combo is released, not pressed. This is more reliable for multi-key
    # combos because it avoids issues where the callback fires while modifier
    # keys are still held down (which can interfere with the Ctrl+C simulation).
    keyboard.add_hotkey(HOTKEY_READ, on_read_selected, suppress=False)
    keyboard.add_hotkey(HOTKEY_OCR, on_ocr_region, suppress=False)
    keyboard.on_press_key("esc", on_stop)
    log(f"Hotkeys registered: {HOTKEY_READ}, {HOTKEY_OCR}, Escape")

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
    keyboard.unhook_all()
    if tray_icon is not None:
        try:
            tray_icon.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
