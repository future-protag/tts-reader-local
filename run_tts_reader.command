#!/bin/bash
# macOS launcher for the TTS Reader.
# Double-click in Finder, or run from a terminal: ./run_tts_reader.command
#
# NOTE: global hotkeys and OCR need the *launching app* (e.g. Terminal) to have
# Accessibility, Input Monitoring, and Screen Recording permission. Grant these
# in System Settings > Privacy & Security on first run.

cd "$(dirname "$0")" || exit 1

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="$(command -v python3.12 || command -v python3)"
fi

echo "Using Python: $PY"
exec "$PY" tts_reader.py
