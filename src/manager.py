"""
manager.py  -  FANUC IoT Edge Multi-Machine Manager
=====================================================
Main entry point for the edge device.

What it does:
  - Reads all *.env configs from machines/ folder
  - Starts one collector.py process per machine
  - Auto-restarts any collector that crashes (after 10s delay)
  - Starts local GUI on http://localhost:8765
  - Handles graceful shutdown on Ctrl+C / service stop

Usage:
  python manager.py              -> start all machines + GUI
  python manager.py --no-gui    -> start collectors only (headless)
  python manager.py --gui-only  -> start GUI only (manage manually)
"""

import os
import sys
import time
import signal
import argparse
import threading
import subprocess
from datetime import datetime

# -- Paths ---------------------------------------------------------------------
# True only when running as the compiled FanucIoTEdge.exe (not python.exe/pythonw.exe)
_IS_EXE = os.path.splitext(os.path.basename(sys.executable))[0].lower() == "fanuciotedge"
BASE_DIR = os.path.dirname(sys.executable) if _IS_EXE else os.path.dirname(os.path.abspath(__file__))

# Machines dir: ProgramData when installed, ../machines when in dev
if _IS_EXE:
    MACHINES_DIR = os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "FanucIoTEdge", "machines")
else:
    MACHINES_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "machines"))

def _collector_cmd(config_path: str) -> list:
    if _IS_EXE:
        return [sys.executable, "--mode", "collector", "--config", config_path]
    return [sys.executable, os.path.join(BASE_DIR, "collector.py"), "--config", config_path]

def _gui_cmd() -> list:
    if _IS_EXE:
        return [sys.executable, "--mode", "gui"]
    return [sys.executable, os.path.join(BASE_DIR, "local_gui.py")]

RESTART_DELAY = 10    # seconds before restarting a crashed collector
MAX_RESTARTS  = 20    # max restarts per session before giving up

# -- Args ----------------------------------------------------------------------
parser = argparse.ArgumentParser(description="FANUC IoT Edge Manager")
parser.add_argument("--no-gui",   action="store_true", help="Run without local GUI")
parser.add_argument("--gui-only", action="store_true", help="Run GUI only, no collectors")
args, _ = parser.parse_known_args()

# -- State ---------------------------------------------------------------------
_running  = True
_procs    = {}          # machine_id -> {"proc": Popen, "restarts": int, "config": path}
_lock     = threading.Lock()


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _log(msg: str):
    print(f"[{_ts()}] [MANAGER] {msg}", flush=True)


# -- Discover machine configs --------------------------------------------------
def discover_machines() -> list:
    """Return list of (machine_id, config_path) tuples."""
    os.makedirs(MACHINES_DIR, exist_ok=True)
    if not os.path.isdir(MACHINES_DIR):
        return []
    found = []
    for f in sorted(os.listdir(MACHINES_DIR)):
        if not f.endswith(".env") or f == "example.env":
            continue
        path = os.path.join(MACHINES_DIR, f)
        mid = None
        try:
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if line.startswith("MACHINE_ID="):
                    mid = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
        if mid:
            found.append((mid, path))
        else:
            _log(f"[WARN] Skipping {f} - no MACHINE_ID found")
    return found


# -- Start one collector -------------------------------------------------------
def start_collector(machine_id: str, config_path: str):
    """Start collector for one machine in a subprocess."""
    try:
        proc = subprocess.Popen(
            _collector_cmd(config_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with _lock:
            if machine_id not in _procs:
                _procs[machine_id] = {"proc": None, "restarts": 0, "config": config_path}
            _procs[machine_id]["proc"] = proc
        _log(f"[START] Collector [{machine_id}] PID={proc.pid}")
        return proc
    except Exception as e:
        _log(f"[ERR] Failed to start [{machine_id}]: {e}")
        return None


# -- Watchdog thread for one machine ------------------------------------------
def watchdog(machine_id: str, config_path: str):
    """Runs in its own thread. Restarts collector if it exits."""
    restarts = 0
    while _running:
        proc = start_collector(machine_id, config_path)
        if proc is None:
            time.sleep(RESTART_DELAY)
            continue

        proc.wait()

        if not _running:
            break

        restarts += 1
        with _lock:
            if machine_id in _procs:
                _procs[machine_id]["restarts"] = restarts

        if restarts >= MAX_RESTARTS:
            _log(f"[STOP] [{machine_id}] reached max restarts ({MAX_RESTARTS}) - giving up")
            break

        exit_code = proc.returncode
        _log(f"[WARN] [{machine_id}] exited (code {exit_code}) - restart #{restarts} in {RESTART_DELAY}s...")
        time.sleep(RESTART_DELAY)

    _log(f"[{machine_id}] watchdog stopped")


# -- Start local GUI ----------------------------------------------------------
def start_gui():
    """Start local GUI as a subprocess."""
    try:
        log_dir = os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "FanucIoTEdge", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "gui.log")
        log_file = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            _gui_cmd(),
            stdout=log_file,
            stderr=log_file,
        )
        _log(f"[GUI] Local GUI started - http://localhost:8765  (PID={proc.pid})")
        return proc
    except Exception as e:
        _log(f"[ERR] Failed to start GUI: {e}")
        return None


# -- Graceful shutdown --------------------------------------------------------
def shutdown(signum=None, frame=None):
    global _running
    _running = False
    _log("Shutting down - stopping all collectors...")
    with _lock:
        for mid, info in _procs.items():
            proc = info.get("proc")
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                _log(f"  [OK] Stopped [{mid}]")
    _log("All collectors stopped. Goodbye.")
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT,  shutdown)


# -- Main ---------------------------------------------------------------------
def main():
    print(flush=True)
    print("==========================================", flush=True)
    print("   FANUC IoT Edge Manager  v2             ", flush=True)
    print("   Multi-machine collector orchestrator   ", flush=True)
    print("==========================================", flush=True)
    print(flush=True)

    gui_proc = None

    if not args.no_gui:
        gui_proc = start_gui()

    if not args.gui_only:
        machines = discover_machines()

        if not machines:
            _log("[WARN] No machine configs found in machines/ folder")
            _log("  Open http://localhost:8765 to add machines via GUI")
        else:
            _log(f"Found {len(machines)} machine config(s):")
            for mid, path in machines:
                _log(f"  * {mid}  ({os.path.basename(path)})")
            print(flush=True)

            threads = []
            for mid, path in machines:
                t = threading.Thread(
                    target=watchdog,
                    args=(mid, path),
                    name=f"watchdog-{mid}",
                    daemon=True,
                )
                t.start()
                threads.append(t)
                time.sleep(0.3)

    _log("Manager running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(30)
            with _lock:
                running = [(m, i) for m, i in _procs.items()
                           if i["proc"] and i["proc"].poll() is None]
                stopped = [(m, i) for m, i in _procs.items()
                           if not i["proc"] or i["proc"].poll() is not None]
            if running or stopped:
                _log(f"Status: {len(running)} running, {len(stopped)} stopped")
                for mid, info in stopped:
                    _log(f"  [WARN] [{mid}] not running (restarts: {info['restarts']})")
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
