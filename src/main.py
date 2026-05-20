"""
main.py  —  FANUC IoT Edge  Entry Point
=========================================
Single file compiled by Nuitka into the final .exe.

Modes:
  (no flag)              → manager: starts GUI + all collectors
  --mode collector       → run one collector (needs --config path)
  --mode gui             → run local GUI only
  --no-gui               → manager without browser GUI
  --gui-only             → GUI only, no collectors
"""

import sys
import os

# Ensure src/ is in path when running as script (dev mode)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _get_mode():
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--mode" and i + 1 < len(sys.argv) - 1:
            return sys.argv[i + 2]
    return "manager"


if __name__ == "__main__":
    mode = _get_mode()

    if mode == "collector":
        from collector import main as collector_main
        collector_main()

    elif mode == "gui":
        from local_gui import app, _migrate_encodings, _log, GUI_PORT
        import threading, time
        _migrate_encodings()
        _log(f"FANUC IoT Edge GUI starting on http://localhost:{GUI_PORT}")
        # Only open browser when NOT running as a Windows Service (LocalSystem has no desktop)
        running_as_service = os.environ.get("USERNAME", "").upper() in ("SYSTEM", "")
        if not running_as_service:
            import webbrowser
            def _open():
                time.sleep(1.5)
                webbrowser.open(f"http://localhost:{GUI_PORT}")
            threading.Thread(target=_open, daemon=True).start()
        app.run(host="0.0.0.0", port=GUI_PORT, debug=False, threaded=True)

    else:
        from manager import main
        main()
