#!/bin/bash
# macOS launcher for the TTS Reader.
# Double-click in Finder, or run from a terminal: ./run_tts_reader.command
#
# NOTE: global hotkeys and OCR need the *launching app* (e.g. Terminal) to have
# Accessibility, Input Monitoring, and Screen Recording permission. Grant these
# in System Settings > Privacy & Security on first run.

cd "$(dirname "$0")" || exit 1

# Secure Keyboard Entry blocks global hotkeys even with permissions granted —
# warn loudly if it's on so we don't spend half an hour wondering why.
sec_pid=$(ioreg -l -w 0 | grep -oE 'SecureInputPID"=[0-9]+' | head -1 | grep -oE '[0-9]+')
if [ -n "$sec_pid" ]; then
    sec_name=$(ps -p "$sec_pid" -o comm= 2>/dev/null | xargs basename 2>/dev/null)
    echo
    echo "WARNING: Secure Keyboard Entry is enabled (PID $sec_pid: ${sec_name:-unknown})."
    echo "Global hotkeys will NOT work until this is cleared. Fixes, in order:"
    echo "  1. Terminal menu (top-left): uncheck 'Secure Keyboard Entry'"
    echo "  2. Lock the screen with Ctrl+Cmd+Q, then unlock"
    echo "  3. Reboot (clears stuck loginwindow holds)"
    echo
fi

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="$(command -v python3.12 || command -v python3)"
fi

echo "Using Python: $PY"
exec "$PY" tts_reader.py
