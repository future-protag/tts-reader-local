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
import time
import logging
import warnings
import threading
import winsound
import ctypes
import tkinter as tk

# Tell Windows we handle DPI ourselves — give us real pixel coordinates.
# Without this, multi-monitor setups with different scaling factors report
# wrong coordinates, causing screenshots to capture the wrong area.
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

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
# Which TTS engine to use: "kokoro" (desktop GPU) or "piper" (laptops)
TTS_ENGINE = "kokoro"

# Hotkeys
HOTKEY_READ = "ctrl+alt+r"          # Read selected text aloud
HOTKEY_OCR = "ctrl+alt+o"           # OCR a screen region, then read aloud
HOTKEY_SPEED_UP = "ctrl+alt+right"      # Increase speech speed
HOTKEY_SPEED_DOWN = "ctrl+alt+left"     # Decrease speech speed

# --- Kokoro settings (only used when TTS_ENGINE = "kokoro") ---
KOKORO_VOICE = "af_heart"      # Voice name (54 choices, see README)
KOKORO_LANG = "a"              # "a" = American English, "b" = British English
KOKORO_SPEED = 1.0             # Speech speed (1.0 = normal, 1.5 = faster)
KOKORO_SAMPLE_RATE = 24000     # Kokoro outputs audio at 24,000 Hz (don't change)

# --- Piper settings (only used when TTS_ENGINE = "piper") ---
PIPER_MODEL = "voices/en_US-lessac-medium.onnx"    # Path to the .onnx voice file
PIPER_DOWNLOAD_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium"

# --- OCR settings ---
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
current_speed = KOKORO_SPEED   # Current speech speed (can be changed with hotkeys)


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
        for gs, ps, audio in tts_engine_obj(text, voice=KOKORO_VOICE, speed=1.0):
            if hasattr(audio, "numpy"):
                audio = audio.numpy()
            if current_speed != 1.0:
                audio = time_stretch_wsola(audio, current_speed, sample_rate)
            yield audio
    elif TTS_ENGINE == "piper":
        for audio_bytes in tts_engine_obj.synthesize_stream_raw(text):
            int_data = np.frombuffer(audio_bytes, dtype=np.int16)
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


def play_audio_stream(chunks_generator, sample_rate):
    """Play audio chunks through the speakers as they arrive from the TTS engine."""
    global is_speaking
    is_speaking = True

    stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32")
    stream.start()

    try:
        for chunk in chunks_generator:
            # Break each TTS chunk into small pieces so Escape is responsive.
            # Kokoro can return 5+ seconds of audio in a single chunk — without
            # splitting, stream.write() blocks for that entire duration.
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

        text = clean_text_for_speech(text)
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

    # Span ALL monitors, not just the primary one.
    # "-fullscreen" only covers the primary monitor in tkinter.
    # Instead, we manually size the window to cover the entire virtual screen
    # (the bounding box of all monitors combined).
    screen_left = root.winfo_vrootx()
    screen_top = root.winfo_vrooty()

    # Use pyautogui to get the full virtual screen size (all monitors)
    total_width, total_height = pyautogui.size()

    # On multi-monitor setups, the virtual screen can start at negative coordinates
    # (if a monitor is to the left of the primary). We need the actual bounds.
    try:
        user32 = ctypes.windll.user32
        # SM_XVIRTUALSCREEN (76) = left edge, SM_YVIRTUALSCREEN (77) = top edge
        # SM_CXVIRTUALSCREEN (78) = total width, SM_CYVIRTUALSCREEN (79) = total height
        screen_left = user32.GetSystemMetrics(76)
        screen_top = user32.GetSystemMetrics(77)
        total_width = user32.GetSystemMetrics(78)
        total_height = user32.GetSystemMetrics(79)
    except Exception:
        pass  # Fall back to pyautogui.size() if this fails

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
    esc_hook = [None]       # Holds the keyboard hook reference (list so closures can modify it)

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
        if esc_hook[0] is not None:
            keyboard.unhook(esc_hook[0])
            esc_hook[0] = None
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

        # Take a screenshot of just that region
        screenshot = pyautogui.screenshot(region=(abs_x1, abs_y1, abs_x2 - abs_x1, abs_y2 - abs_y1))

        # OCR and speak in a background thread
        threading.Thread(target=ocr_and_speak, args=(screenshot,), daemon=True).start()

    def on_escape(event):
        close_overlay()
        log("OCR capture cancelled.")

    def on_overlay_escape(event):
        # The keyboard library calls this from a background thread.
        # Tkinter isn't safe to call from other threads, so we use
        # root.after() to schedule the close on tkinter's own thread.
        try:
            root.after(0, close_overlay)
        except Exception:
            pass

    canvas.bind("<ButtonPress-1>", on_mouse_down)
    canvas.bind("<B1-Motion>", on_mouse_drag)
    canvas.bind("<ButtonRelease-1>", on_mouse_up)
    root.bind("<Escape>", on_escape)
    # Also register with keyboard library as a backup — fires even without focus
    esc_hook[0] = keyboard.on_press_key("esc", on_overlay_escape)

    root.mainloop()

    # Clean up the keyboard hook if it wasn't already removed by close_overlay
    if esc_hook[0] is not None:
        keyboard.unhook(esc_hook[0])
        esc_hook[0] = None

    # Do NOT destroy root here. The _overlay_root reference keeps it alive,
    # preventing garbage collection on a random thread (which causes
    # "Tcl_AsyncDelete: async handler deleted by the wrong thread").
    # It gets destroyed on the main thread at the start of the NEXT OCR capture.


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

        text = clean_text_for_speech(text)
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
        winsound.Beep(1000 + int(current_speed * 200), 50)  # Higher pitch = faster
    else:
        log(f"Speed: {current_speed}x (already at maximum)")

def on_speed_down():
    """Hotkey handler: decrease speech speed."""
    global current_speed
    if current_speed > SPEED_MIN:
        current_speed = round(current_speed - SPEED_STEP, 2)
        log(f"Speed: {current_speed}x")
        winsound.Beep(1000 + int(current_speed * 200), 50)  # Lower pitch = slower
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
    print(f"  Press  Ctrl+Alt+R         to read selected text aloud")
    print(f"  Press  Ctrl+Alt+O         to OCR a screen region")
    print(f"  Press  Ctrl+Alt+Right     to speed up")
    print(f"  Press  Ctrl+Alt+Left      to slow down")
    print(f"  Press  Escape             to stop speaking")
    print(f"  Right-click tray icon to quit")
    print()
    log(f"Ready! Engine: {TTS_ENGINE}, Speed: {current_speed}x")
    print()

    # Register hotkeys
    keyboard.add_hotkey(HOTKEY_READ, on_read_selected, suppress=False)
    keyboard.add_hotkey(HOTKEY_OCR, on_ocr_region, suppress=False)
    keyboard.add_hotkey(HOTKEY_SPEED_UP, on_speed_up, suppress=False)
    keyboard.add_hotkey(HOTKEY_SPEED_DOWN, on_speed_down, suppress=False)
    keyboard.on_press_key("esc", on_stop)
    log(f"Hotkeys registered: {HOTKEY_READ}, {HOTKEY_OCR}, {HOTKEY_SPEED_UP}, {HOTKEY_SPEED_DOWN}, Escape")

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
