"""
local_gui.py  —  FANUC IoT Edge Local GUI
==========================================
Runs a local web server on http://localhost:8765
Provides browser-based UI for:
  - Add / edit / delete machine configs
  - Ping CNC, test server, test FOCAS connection
  - Test PMC signals (read any bit live)
  - View live log stream (SSE)
  - Start / stop individual machine collectors

Run standalone:  python local_gui.py
"""

import os
import sys
import json
import time
import queue
import socket
import hashlib
import secrets
import threading
import subprocess
import ipaddress
import requests
from datetime import datetime
from urllib.parse import urlparse
from functools import wraps
from flask import Flask, Response, request, jsonify, session, redirect
from dotenv import dotenv_values, set_key

# ── Paths ─────────────────────────────────────────────────────────────────────
# When compiled as Nuitka onefile .exe:
#   sys.executable = installed .exe path  (e.g. C:\Program Files\FANUC IoT Edge\FanucIoTEdge.exe)
#   __file__       = temp extraction dir  (wrong for finding data files!)
# When running as plain Python script (dev):
#   sys.executable = python.exe
#   __file__       = this script's path   (correct)

def _resolve_paths():
    is_exe = os.path.splitext(sys.executable)[1].lower() == ".exe"
    if is_exe:
        data_dir = os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "FanucIoTEdge")
        base     = os.path.dirname(sys.executable)
    else:
        base     = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.abspath(os.path.join(base, ".."))
    return (
        base,
        os.path.join(data_dir, "machines"),
        os.path.join(data_dir, "auth.json"),
        os.path.join(data_dir, "status"),
        os.path.join(data_dir, "secret.key"),
    )

BASE_DIR, MACHINES_DIR, AUTH_FILE, STATUS_DIR, SECRET_KEY_FILE = _resolve_paths()
os.makedirs(MACHINES_DIR, exist_ok=True)
os.makedirs(STATUS_DIR,   exist_ok=True)
print(f"[EDGE] Machines folder: {MACHINES_DIR}", flush=True)

APP_VERSION  = "2.0.0"
GUI_PORT     = 8765

app = Flask(__name__)

# ── Persistent session secret key ─────────────────────────────────────────────
def _load_secret_key() -> bytes:
    try:
        if os.path.exists(SECRET_KEY_FILE):
            return open(SECRET_KEY_FILE, "rb").read()
    except Exception:
        pass
    key = secrets.token_bytes(32)
    try:
        with open(SECRET_KEY_FILE, "wb") as f:
            f.write(key)
    except Exception:
        pass
    return key

app.secret_key = _load_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = 30 * 24 * 3600  # 30-day sessions


# ── Auth helpers ──────────────────────────────────────────────────────────────
def _hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 260_000)
    return f"pbkdf2:{salt}:{h.hex()}"

def _verify_password(stored: str, pw: str) -> bool:
    try:
        _, salt, stored_h = stored.split(":")
        h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 260_000)
        return secrets.compare_digest(h.hex(), stored_h)
    except Exception:
        return False

def _load_auth() -> dict:
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_auth(data: dict):
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── Auth guard — every route except /login /setup /logout requires login ───────
@app.before_request
def _require_login():
    if request.endpoint in ("login", "setup", "logout"):
        return None
    auth = _load_auth()
    if not auth.get("username"):
        # No account created yet — first-time setup
        if request.path.startswith("/api/"):
            return jsonify({"error": "Setup required"}), 401
        return redirect("/setup")
    if not session.get("logged_in"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect("/login")


def _migrate_encodings():
    """Re-save any machine .env files that aren't valid UTF-8 → convert to UTF-8."""
    for f in _list_machine_files():
        try:
            with open(f, "r", encoding="utf-8") as fh:
                fh.read()
        except UnicodeDecodeError:
            try:
                with open(f, "r", encoding="cp1252") as fh:
                    content = fh.read()
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(content)
                print(f"[EDGE] Migrated {os.path.basename(f)} to UTF-8", flush=True)
            except Exception as e:
                print(f"[EDGE] WARNING: could not migrate {f}: {e}", flush=True)

# ── Log queue (collector processes write here, SSE streams it out) ─────────────
_log_queue   = queue.Queue(maxsize=500)
_procs       = {}   # machine_id → subprocess.Popen

def _log(msg: str):
    ts  = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except (AttributeError, OSError):
        pass  # stdout is None when running as console-less .exe subprocess
    try: _log_queue.put_nowait(line)
    except queue.Full: pass


# ── Machine config helpers ────────────────────────────────────────────────────
def _list_machine_files() -> list:
    files = []
    for f in os.listdir(MACHINES_DIR):
        if f.endswith(".env") and f != "example.env":
            files.append(os.path.join(MACHINES_DIR, f))
    return sorted(files)


# ── Security helpers ──────────────────────────────────────────────────────────

def _strip(val) -> str:
    """Strip whitespace and newline chars — prevents .env injection."""
    return str(val).replace("\n", "").replace("\r", "").strip()


def _safe_int(val, default: int, min_val: int, max_val: int) -> int:
    """Clamp integer within allowed bounds; return default on bad input."""
    try:
        return max(min_val, min(max_val, int(val)))
    except (ValueError, TypeError):
        return default


def _safe_machine_path(machine_id: str):
    """Resolve machine_id → .env path and verify it stays inside MACHINES_DIR.
    Returns (path, None) on success, (None, error_str) on failure."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in machine_id)
    if not safe:
        return None, "Invalid machine_id"
    filename  = safe + ".env"
    candidate = os.path.join(MACHINES_DIR, filename)
    real_base = os.path.realpath(MACHINES_DIR) + os.sep
    if not os.path.realpath(candidate).startswith(real_base):
        return None, "Invalid machine_id"
    return candidate, None


def _validate_server_url(url: str):
    """Return error string if the URL is unsafe, None if OK.
    Blocks non-HTTPS and internal/loopback addresses (SSRF guard)."""
    if not url.startswith("https://"):
        return "Server URL must start with https://"
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return "Invalid URL"
    if host.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return "Server URL cannot point to localhost"
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            return "Server URL cannot point to private/internal addresses"
    except ValueError:
        pass  # hostname, not an IP literal — allowed
    return None


def _load_machine(path: str) -> dict:
    try:
        cfg = dotenv_values(path, encoding="utf-8")
    except Exception:
        # Fallback: try system default encoding (for legacy files saved without utf-8)
        try:
            cfg = dotenv_values(path, encoding="cp1252")
        except Exception:
            cfg = {}
    return {
        "file":         os.path.basename(path),
        "machine_id":   cfg.get("MACHINE_ID", ""),
        "fanuc_ip":     cfg.get("FANUC_IP",    "192.168.1.22"),
        "fanuc_port":   cfg.get("FANUC_PORT",  "8193"),
        "server_url":   cfg.get("SERVER_URL",  ""),
        "macro_vars":   cfg.get("MACRO_VARS",  "3901:Parts Done,3902:Parts Required"),
        "pmc_auto_exec_byte":   cfg.get("PMC_AUTO_EXEC_BYTE",   "0"),
        "pmc_auto_exec_bit":    cfg.get("PMC_AUTO_EXEC_BIT",    "7"),
        "pmc_cutting_byte":     cfg.get("PMC_CUTTING_BYTE",     "2"),
        "pmc_cutting_bit":      cfg.get("PMC_CUTTING_BIT",      "6"),
        "pmc_spindle_cw_byte":  cfg.get("PMC_SPINDLE_CW_BYTE",  "70"),
        "pmc_spindle_cw_bit":   cfg.get("PMC_SPINDLE_CW_BIT",   "5"),
        "pmc_spindle_ccw_byte": cfg.get("PMC_SPINDLE_CCW_BYTE", "70"),
        "pmc_spindle_ccw_bit":  cfg.get("PMC_SPINDLE_CCW_BIT",  "4"),
        "pmc_feed_hold_byte":   cfg.get("PMC_FEED_HOLD_BYTE",   "0"),
        "pmc_feed_hold_bit":    cfg.get("PMC_FEED_HOLD_BIT",    "4"),
        "pmc_block_stop_byte":  cfg.get("PMC_BLOCK_STOP_BYTE",  "46"),
        "pmc_block_stop_bit":   cfg.get("PMC_BLOCK_STOP_BIT",   "1"),
        "running": _procs.get(cfg.get("MACHINE_ID", "")) is not None and
                   _procs.get(cfg.get("MACHINE_ID", "")).poll() is None,
    }


def _save_machine(path: str, data: dict):
    # _strip() every value to prevent .env injection via embedded newlines
    lines = f"""# FANUC IoT Edge — Machine Config
# Generated by Local GUI on {datetime.now().strftime('%Y-%m-%d %H:%M')}

MACHINE_ID={_strip(data.get('machine_id', ''))}
MACHINE_TOKEN={_strip(data.get('machine_token', ''))}

FANUC_IP={_strip(data.get('fanuc_ip', '192.168.1.22'))}
FANUC_PORT={_safe_int(data.get('fanuc_port', 8193), 8193, 1, 65535)}
FANUC_TIMEOUT=10

SERVER_URL={_strip(data.get('server_url', ''))}

PMC_AUTO_EXEC_BYTE={_safe_int(data.get('pmc_auto_exec_byte', 0), 0, 0, 9999)}
PMC_AUTO_EXEC_BIT={_safe_int(data.get('pmc_auto_exec_bit', 7), 7, 0, 7)}
PMC_CUTTING_BYTE={_safe_int(data.get('pmc_cutting_byte', 2), 2, 0, 9999)}
PMC_CUTTING_BIT={_safe_int(data.get('pmc_cutting_bit', 6), 6, 0, 7)}
PMC_SPINDLE_CW_BYTE={_safe_int(data.get('pmc_spindle_cw_byte', 70), 70, 0, 9999)}
PMC_SPINDLE_CW_BIT={_safe_int(data.get('pmc_spindle_cw_bit', 5), 5, 0, 7)}
PMC_SPINDLE_CCW_BYTE={_safe_int(data.get('pmc_spindle_ccw_byte', 70), 70, 0, 9999)}
PMC_SPINDLE_CCW_BIT={_safe_int(data.get('pmc_spindle_ccw_bit', 4), 4, 0, 7)}
PMC_FEED_HOLD_BYTE={_safe_int(data.get('pmc_feed_hold_byte', 0), 0, 0, 9999)}
PMC_FEED_HOLD_BIT={_safe_int(data.get('pmc_feed_hold_bit', 4), 4, 0, 7)}
PMC_BLOCK_STOP_BYTE={_safe_int(data.get('pmc_block_stop_byte', 46), 46, 0, 9999)}
PMC_BLOCK_STOP_BIT={_safe_int(data.get('pmc_block_stop_bit', 1), 1, 0, 7)}

MACRO_VARS={_strip(data.get('macro_vars', '3901:Parts Done,3902:Parts Required'))}
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(lines)


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/machines", methods=["GET"])
def api_list_machines():
    machines = []
    for f in _list_machine_files():
        try:
            machines.append(_load_machine(f))
        except Exception as e:
            _log(f"WARNING: could not load {f}: {e}")
    return jsonify(machines)


@app.route("/api/machines", methods=["POST"])
def api_add_machine():
    data     = request.json or {}
    mid      = data.get("machine_id", "").strip()
    if not mid:
        return jsonify({"error": "machine_id is required"}), 400
    # Sanitize filename — only allow alphanumeric, dash, underscore, dot
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in mid)
    filename  = safe_name + ".env"
    path      = os.path.join(MACHINES_DIR, filename)
    if os.path.exists(path):
        return jsonify({"error": f"Machine ID '{mid}' already exists"}), 409
    try:
        _save_machine(path, data)
        _log(f"Machine added: {mid} → {path}")
        return jsonify({"ok": True, "file": filename})  # path omitted (info leak)
    except Exception as e:
        _log(f"ERROR saving machine {mid}: {e}")
        return jsonify({"error": "Failed to save machine config"}), 500


@app.route("/api/debug")
def api_debug():
    """Debug endpoint — only active when FANUC_DEBUG env var is set (H1 guard)."""
    if not os.environ.get("FANUC_DEBUG"):
        return jsonify({"error": "Not found"}), 404
    files = []
    try:
        files = os.listdir(MACHINES_DIR)
    except Exception as e:
        files = [f"ERROR: {e}"]
    return jsonify({
        "base_dir":     BASE_DIR,
        "machines_dir": MACHINES_DIR,
        "machines_dir_exists": os.path.exists(MACHINES_DIR),
        "files_in_dir": files,
        "loaded_machines": len(_list_machine_files()),
    })


@app.route("/api/machines/<machine_id>", methods=["PUT"])
def api_update_machine(machine_id):
    path, err = _safe_machine_path(machine_id)
    if err:
        return jsonify({"error": err}), 400
    if not os.path.exists(path):
        return jsonify({"error": "Machine not found"}), 404
    data = request.json or {}
    _save_machine(path, data)
    _log(f"Machine updated: {machine_id}")
    return jsonify({"ok": True})


@app.route("/api/machines/<machine_id>", methods=["DELETE"])
def api_delete_machine(machine_id):
    path, err = _safe_machine_path(machine_id)
    if err:
        return jsonify({"error": err}), 400
    if not os.path.exists(path):
        return jsonify({"error": "Machine not found"}), 404
    # Stop collector first
    _stop_collector(machine_id)
    os.remove(path)
    _log(f"Machine deleted: {machine_id}")
    return jsonify({"ok": True})


# ── Collector start/stop ──────────────────────────────────────────────────────

def _start_collector(machine_id: str) -> dict:
    if machine_id in _procs and _procs[machine_id].poll() is None:
        return {"error": "Already running"}
    path, err = _safe_machine_path(machine_id)
    if err:
        return {"error": err}
    if not os.path.exists(path):
        return {"error": "Config file not found"}
    collector = os.path.join(BASE_DIR, "collector.py")
    proc = subprocess.Popen(
        [sys.executable, collector, "--config", path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    _procs[machine_id] = proc
    # Stream output to log queue
    def _reader():
        for line in iter(proc.stdout.readline, ""):
            _log(f"[{machine_id}] {line.rstrip()}")
        proc.stdout.close()
    threading.Thread(target=_reader, daemon=True).start()
    _log(f"Started collector for {machine_id} (PID {proc.pid})")
    return {"ok": True, "pid": proc.pid}


def _stop_collector(machine_id: str) -> dict:
    proc = _procs.get(machine_id)
    if proc is None or proc.poll() is not None:
        _procs.pop(machine_id, None)
        return {"error": "Not running"}
    proc.terminate()
    try: proc.wait(timeout=5)
    except subprocess.TimeoutExpired: proc.kill()
    _procs.pop(machine_id, None)
    _log(f"Stopped collector for {machine_id}")
    return {"ok": True}


@app.route("/api/machines/<machine_id>/start", methods=["POST"])
def api_start(machine_id):
    return jsonify(_start_collector(machine_id))


@app.route("/api/machines/<machine_id>/stop", methods=["POST"])
def api_stop(machine_id):
    return jsonify(_stop_collector(machine_id))


# ── Test tools ────────────────────────────────────────────────────────────────

@app.route("/api/test/ping", methods=["POST"])
def api_ping():
    data = request.json or {}
    ip   = data.get("ip", "").strip()
    port = _safe_int(data.get("port", 8193), 8193, 1, 65535)
    if not ip:
        return jsonify({"error": "IP required"}), 400
    try:
        t0  = time.monotonic()
        s   = socket.create_connection((ip, port), timeout=3)
        ms  = round((time.monotonic() - t0) * 1000, 1)
        s.close()
        _log(f"Ping {ip}:{port} → OK ({ms}ms)")
        return jsonify({"ok": True, "ms": ms, "message": f"Reachable in {ms}ms"})
    except Exception as e:
        _log(f"Ping {ip}:{port} → FAILED: {e}")
        return jsonify({"ok": False, "message": "Host unreachable"})


@app.route("/api/test/server", methods=["POST"])
def api_test_server():
    data  = request.json or {}
    url   = data.get("url", "").strip().rstrip("/")
    if not url:
        return jsonify({"error": "Server URL required"}), 400
    # SSRF guard — block non-HTTPS and private/internal addresses (H2)
    err = _validate_server_url(url)
    if err:
        return jsonify({"error": err}), 400
    try:
        t0  = time.monotonic()
        r   = requests.get(f"{url}/health", timeout=5)
        ms  = round((time.monotonic() - t0) * 1000, 1)
        ok  = r.status_code == 200
        msg = f"{'OK' if ok else 'Error'} — HTTP {r.status_code} in {ms}ms"
        _log(f"Server test {url} → {msg}")
        return jsonify({"ok": ok, "ms": ms, "message": msg,
                        "response": r.json() if ok else {}})
    except requests.exceptions.SSLError:
        _log(f"Server test {url} → SSL error")
        return jsonify({"ok": False, "message": "SSL certificate error"})
    except requests.exceptions.ConnectionError:
        _log(f"Server test {url} → connection refused")
        return jsonify({"ok": False, "message": "Could not connect to server"})
    except Exception as e:
        _log(f"Server test {url} → FAILED: {e}")
        return jsonify({"ok": False, "message": "Server test failed"})


@app.route("/api/test/focas", methods=["POST"])
def api_test_focas():
    """Try to connect FOCAS and read basic data."""
    data    = request.json or {}
    ip      = data.get("ip", "").strip()
    port    = _safe_int(data.get("port", 8193),    8193, 1, 65535)
    timeout = _safe_int(data.get("timeout", 5),    5,    1, 30)
    if not ip:
        return jsonify({"error": "IP required"}), 400
    try:
        from focas_reader import FocasReader
        reader = FocasReader(ip=ip, port=port, timeout=timeout)
        reader.connect()
        result = reader.read_all([3901, 3902], {
            "auto_exec_byte": 0,  "auto_exec_bit": 7,
            "cutting_byte":   2,  "cutting_bit":   6,
            "spindle_cw_byte":70, "spindle_cw_bit":5,
            "spindle_ccw_byte":70,"spindle_ccw_bit":4,
            "feed_hold_byte": 0,  "feed_hold_bit": 4,
            "block_stop_byte":46, "block_stop_bit":1,
        })
        reader.disconnect()
        _log(f"FOCAS test {ip}:{port} → OK | O{result.get('prog_num')} Feed:{result.get('feed_rate')}")
        return jsonify({
            "ok":      True,
            "message": f"Connected to CNC at {ip}:{port}",
            "data":    {k: v for k, v in result.items() if k != "m30_fired"},
        })
    except Exception as e:
        _log(f"FOCAS test {ip}:{port} → FAILED: {e}")
        return jsonify({"ok": False, "message": "FOCAS connection failed"})


@app.route("/api/test/pmc", methods=["POST"])
def api_test_pmc():
    """Read a specific PMC bit live from the CNC."""
    data      = request.json or {}
    ip        = data.get("ip", "").strip()
    port      = _safe_int(data.get("port",    8193), 8193, 1,    65535)
    byte_no   = _safe_int(data.get("byte_no", 0),    0,    0,    9999)
    bit_no    = _safe_int(data.get("bit_no",  7),    7,    0,    7)
    pmc_type  = data.get("pmc_type", "F").upper()
    if pmc_type not in ("F", "G", "X", "Y", "R", "D"):
        pmc_type = "F"
    if not ip:
        return jsonify({"error": "IP required"}), 400
    try:
        from focas_reader import FocasReader
        reader = FocasReader(ip=ip, port=port, timeout=5)
        reader.connect()
        value  = reader._read_bit(pmc_type, byte_no, bit_no)
        reader.disconnect()
        _log(f"PMC {pmc_type}{byte_no}.{bit_no} @ {ip} = {int(value)}")
        return jsonify({
            "ok":    True,
            "value": int(value),
            "message": f"{pmc_type}{byte_no}.{bit_no} = {int(value)} ({'ON' if value else 'OFF'})",
        })
    except Exception as e:
        _log(f"PMC read FAILED: {e}")
        return jsonify({"ok": False, "message": "PMC read failed"})


# ── SSE Log stream ─────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    def _stream():
        yield "data: {\"msg\": \"Log stream connected\"}\n\n"
        while True:
            try:
                line = _log_queue.get(timeout=15)
                yield f"data: {json.dumps({'msg': line})}\n\n"
            except queue.Empty:
                yield "data: {\"msg\": \"ping\"}\n\n"
    return Response(_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/status")
def api_status():
    machines = []
    for f in _list_machine_files():
        try:
            m = _load_machine(f)
        except Exception:
            continue
        machines.append({
            "machine_id": m["machine_id"],
            "fanuc_ip":   m["fanuc_ip"],
            "running":    m["running"],
        })
    return jsonify({
        "version":  APP_VERSION,
        "machines": machines,
        "uptime":   int(time.time()),
        "username": session.get("username", ""),
    })


# ── Auth & live-data routes ───────────────────────────────────────────────────

@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First-time account creation — shown when no auth.json exists."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        pw       = request.form.get("password", "")
        pw2      = request.form.get("confirm", "")
        if not username or not pw:
            return _setup_html("Username and password are required.")
        if pw != pw2:
            return _setup_html("Passwords do not match.")
        if len(pw) < 6:
            return _setup_html("Password must be at least 6 characters.")
        _save_auth({"username": username, "password_hash": _hash_password(pw)})
        session["logged_in"] = True
        session["username"]  = username
        session.permanent    = True
        _log(f"First-time setup — account '{username}' created")
        return redirect("/")
    return _setup_html("")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        pw       = request.form.get("password", "")
        auth     = _load_auth()
        if username == auth.get("username") and _verify_password(auth.get("password_hash", ""), pw):
            session["logged_in"] = True
            session["username"]  = username
            session.permanent    = True
            _log(f"Login: '{username}'")
            return redirect("/")
        return _login_html("Invalid username or password.")
    return _login_html("")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/live")
def api_live():
    """Read per-machine status JSON files written by collectors every 5s."""
    machines = []
    try:
        for f in sorted(os.listdir(STATUS_DIR)):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(STATUS_DIR, f), "r", encoding="utf-8") as fh:
                    machines.append(json.load(fh))
            except Exception:
                pass
    except Exception:
        pass
    return jsonify(machines)


# ── Auth HTML helpers ─────────────────────────────────────────────────────────

_AUTH_HEAD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Metal Craft Solutions — IoT Edge</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--sub:#94a3b8;--blue:#3b82f6;--red:#ef4444;--amber:#f59e0b}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:36px;width:380px;max-width:100%}
.logo{display:flex;align-items:center;gap:10px;justify-content:center;margin-bottom:28px}
.brand{display:flex;flex-direction:column;gap:2px}
.brand-t{font-size:15px;font-weight:700;color:#e2e8f0}
.brand-s{font-size:10px;font-weight:600;color:#f59e0b;text-transform:uppercase;letter-spacing:.12em}
h2{font-size:19px;font-weight:600;text-align:center;margin-bottom:4px}
.sub{font-size:12px;color:var(--sub);text-align:center;margin-bottom:24px}
label{display:block;font-size:11px;color:var(--sub);font-weight:500;margin-bottom:4px}
input{width:100%;background:#0f172a;border:1px solid var(--border);color:var(--text);padding:9px 12px;border-radius:7px;font-size:14px;margin-bottom:14px}
input:focus{outline:none;border-color:var(--blue)}
.btn{width:100%;padding:10px;background:var(--blue);color:#fff;border:none;border-radius:7px;font-size:14px;font-weight:600;cursor:pointer;margin-top:4px}
.btn:hover{background:#2563eb}
.err{background:rgba(239,68,68,.12);border:1px solid var(--red);color:var(--red);padding:9px 12px;border-radius:7px;font-size:12px;margin-bottom:16px}
.note{font-size:11px;color:var(--sub);text-align:center;margin-top:16px}
</style></head><body><div class="card">"""

_LOGO_SVG = """<div class="logo">
  <svg width="36" height="36" viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg">
    <polygon points="18,2 27,6.5 32,14 32,22 27,29.5 18,34 9,29.5 4,22 4,14 9,6.5" fill="#1e3a5f" stroke="#3b82f6" stroke-width="1.5"/>
    <circle cx="18" cy="18" r="7.5" fill="#0f172a"/>
    <line x1="14" y1="15" x2="22" y2="15" stroke="#3b82f6" stroke-width="1"/>
    <line x1="14" y1="15" x2="14" y2="21" stroke="#3b82f6" stroke-width="1"/>
    <line x1="22" y1="15" x2="22" y2="21" stroke="#60a5fa" stroke-width="1"/>
    <line x1="14" y1="21" x2="22" y2="21" stroke="#3b82f6" stroke-width="1"/>
    <circle cx="14" cy="15" r="1.8" fill="#3b82f6"/>
    <circle cx="22" cy="15" r="1.8" fill="#f59e0b"/>
    <circle cx="14" cy="21" r="1.8" fill="#60a5fa"/>
    <circle cx="22" cy="21" r="1.8" fill="#3b82f6"/>
    <circle cx="18" cy="18" r="1.2" fill="#f59e0b"/>
  </svg>
  <div class="brand"><div class="brand-t">IoT Edge Setup</div><div class="brand-s">Metal Craft Solutions</div></div>
</div>"""


def _login_html(error: str) -> str:
    err = f'<div class="err">{error}</div>' if error else ""
    return _AUTH_HEAD + _LOGO_SVG + f"""
<h2>Sign In</h2>
<p class="sub">Edge device configuration panel</p>
{err}
<form method="POST">
  <label>Username</label>
  <input name="username" type="text" autocomplete="username" autofocus required>
  <label>Password</label>
  <input name="password" type="password" autocomplete="current-password" required>
  <button class="btn" type="submit">Sign In →</button>
</form>
</div></body></html>"""


def _setup_html(error: str) -> str:
    err = f'<div class="err">{error}</div>' if error else ""
    return _AUTH_HEAD + _LOGO_SVG + f"""
<h2>Create Admin Account</h2>
<p class="sub">First-time setup — set your login credentials</p>
{err}
<form method="POST">
  <label>Username</label>
  <input name="username" type="text" autocomplete="username" autofocus required>
  <label>Password</label>
  <input name="password" type="password" autocomplete="new-password" required>
  <label>Confirm Password</label>
  <input name="confirm" type="password" autocomplete="new-password" required>
  <button class="btn" type="submit">Create Account &amp; Continue →</button>
</form>
<p class="note">These credentials protect access to this device's configuration page.</p>
</div></body></html>"""


# ── Main HTML page ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Metal Craft Solutions — IoT Edge</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 36 36'%3E%3Cpolygon points='18,2 26,6 32,13 32,23 26,30 18,34 10,30 4,23 4,13 10,6' fill='%231e3a5f' stroke='%233b82f6' stroke-width='1.5'/%3E%3Ccircle cx='18' cy='18' r='7' fill='%230f172a'/%3E%3Cline x1='14' y1='15' x2='22' y2='15' stroke='%233b82f6' stroke-width='1'/%3E%3Cline x1='14' y1='15' x2='14' y2='21' stroke='%233b82f6' stroke-width='1'/%3E%3Cline x1='22' y1='15' x2='22' y2='21' stroke='%2360a5fa' stroke-width='1'/%3E%3Cline x1='14' y1='21' x2='22' y2='21' stroke='%233b82f6' stroke-width='1'/%3E%3Ccircle cx='14' cy='15' r='1.8' fill='%233b82f6'/%3E%3Ccircle cx='22' cy='15' r='1.8' fill='%23f59e0b'/%3E%3Ccircle cx='14' cy='21' r='1.8' fill='%2360a5fa'/%3E%3Ccircle cx='22' cy='21' r='1.8' fill='%233b82f6'/%3E%3C/svg%3E">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--sub:#94a3b8;--blue:#3b82f6;--green:#22c55e;--red:#ef4444;--amber:#f59e0b;--purple:#a78bfa}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}
/* Layout */
.header{background:var(--card);border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;gap:14px}
.header-logo{display:flex;align-items:center;gap:10px;text-decoration:none}
.header-brand{display:flex;flex-direction:column;gap:1px}
.header-title{font-size:15px;font-weight:700;color:#e2e8f0;letter-spacing:.01em}
.header-sub{font-size:10px;font-weight:600;color:#f59e0b;text-transform:uppercase;letter-spacing:.12em}
.header-ver{font-size:11px;color:var(--sub);background:rgba(96,165,250,.1);padding:2px 8px;border-radius:10px;margin-left:4px}
.container{max-width:1100px;margin:0 auto;padding:24px}
/* Tabs */
.tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid var(--border);padding-bottom:0}
.tab{padding:8px 18px;border:none;background:none;color:var(--sub);cursor:pointer;font-size:13px;font-weight:500;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab.active{color:var(--blue);border-bottom-color:var(--blue)}
.tab-panel{display:none}.tab-panel.active{display:block}
/* Cards */
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:16px}
.card-title{font-size:13px;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.05em;margin-bottom:16px}
/* Forms */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-grid.cols3{grid-template-columns:1fr 1fr 1fr}
.form-group{display:flex;flex-direction:column;gap:4px}
.form-group label{font-size:11px;color:var(--sub);font-weight:500}
.form-group input,.form-group select{background:#0f172a;border:1px solid var(--border);color:var(--text);padding:7px 10px;border-radius:6px;font-size:13px;width:100%}
.form-group input:focus,.form-group select:focus{outline:none;border-color:var(--blue)}
.form-row{display:flex;gap:8px;align-items:flex-end}
/* Buttons */
.btn{padding:7px 14px;border:none;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.btn-primary{background:var(--blue);color:#fff}
.btn-primary:hover{background:#2563eb}
.btn-success{background:var(--green);color:#fff}
.btn-success:hover{background:#16a34a}
.btn-danger{background:var(--red);color:#fff}
.btn-danger:hover{background:#dc2626}
.btn-ghost{background:rgba(148,163,184,.1);color:var(--sub);border:1px solid var(--border)}
.btn-ghost:hover{background:rgba(148,163,184,.2)}
.btn-sm{padding:4px 10px;font-size:12px}
/* Machine list */
.machine-row{display:flex;align-items:center;gap:12px;padding:12px 16px;background:#0f172a;border:1px solid var(--border);border-radius:8px;margin-bottom:8px}
.machine-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot-run{background:var(--green);animation:pulse 1.5s infinite}
.dot-stop{background:var(--sub)}
.machine-name{font-weight:600;min-width:160px}
.machine-ip{color:var(--sub);font-size:12px;min-width:120px}
.machine-acts{margin-left:auto;display:flex;gap:6px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
/* Result box */
.result-box{background:#0f172a;border:1px solid var(--border);border-radius:8px;padding:12px 16px;font-family:monospace;font-size:12px;margin-top:12px;min-height:48px;white-space:pre-wrap;line-height:1.6}
.result-ok{border-color:var(--green);color:var(--green)}
.result-err{border-color:var(--red);color:var(--red)}
/* Log stream */
#log-box{height:400px;overflow-y:auto;background:#0f172a;border:1px solid var(--border);border-radius:8px;padding:12px;font-family:monospace;font-size:11px;line-height:1.7}
.log-line{color:#94a3b8}
.log-line.ok{color:var(--green)}
.log-line.err{color:var(--red)}
/* Modal */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center}
.modal-bg.show{display:flex}
.modal{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;width:660px;max-width:95vw;max-height:90vh;overflow-y:auto}
.modal-title{font-size:15px;font-weight:600;margin-bottom:20px;color:#60a5fa}
.modal-footer{display:flex;gap:8px;justify-content:flex-end;margin-top:20px;padding-top:16px;border-top:1px solid var(--border)}
/* Section label */
.section-label{font-size:11px;color:var(--sub);font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin:16px 0 8px;padding-bottom:4px;border-bottom:1px solid var(--border)}
/* Status badge */
.badge{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.badge-run{background:rgba(34,197,94,.15);color:var(--green)}
.badge-stop{background:rgba(100,116,139,.15);color:var(--sub)}
/* Live data cards */
.live-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
.live-card.connected{border-color:rgba(34,197,94,.3)}
.live-card.disconnected{border-color:rgba(239,68,68,.3)}
.live-card.buffering{border-color:rgba(245,158,11,.3)}
.live-mid{font-size:15px;font-weight:700;margin-bottom:2px}
.live-ip{font-size:11px;color:var(--sub);margin-bottom:12px}
.live-signals{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.sig{padding:3px 10px;border-radius:12px;font-size:11px;font-weight:600}
.sig-on{background:rgba(34,197,94,.15);color:#22c55e}
.sig-off{background:rgba(100,116,139,.1);color:var(--sub)}
.live-stats{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.stat{background:#0f172a;border-radius:6px;padding:8px 10px}
.stat-label{font-size:10px;color:var(--sub);font-weight:500;text-transform:uppercase;letter-spacing:.04em}
.stat-val{font-size:18px;font-weight:700;color:var(--text);margin-top:2px}
.live-footer{display:flex;justify-content:space-between;margin-top:10px;font-size:10px;color:var(--sub)}
.live-status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px}
/* Header logout */
.hdr-user{font-size:11px;color:var(--sub);display:flex;align-items:center;gap:8px}
</style>
</head>
<body>

<div class="header">
  <!-- Metal Craft Solutions Logo -->
  <div class="header-logo">
    <svg width="38" height="38" viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg">
      <polygon points="18,2 27,6.5 32,14 32,22 27,29.5 18,34 9,29.5 4,22 4,14 9,6.5"
               fill="#1e3a5f" stroke="#3b82f6" stroke-width="1.5"/>
      <circle cx="18" cy="18" r="7.5" fill="#0f172a"/>
      <line x1="14" y1="15" x2="22" y2="15" stroke="#3b82f6" stroke-width="1"/>
      <line x1="14" y1="15" x2="14" y2="21" stroke="#3b82f6" stroke-width="1"/>
      <line x1="22" y1="15" x2="22" y2="21" stroke="#60a5fa" stroke-width="1"/>
      <line x1="14" y1="21" x2="22" y2="21" stroke="#3b82f6" stroke-width="1"/>
      <line x1="18" y1="15" x2="18" y2="21" stroke="#3b82f6" stroke-width="0.6" opacity="0.5"/>
      <circle cx="14" cy="15" r="1.8" fill="#3b82f6"/>
      <circle cx="22" cy="15" r="1.8" fill="#f59e0b"/>
      <circle cx="14" cy="21" r="1.8" fill="#60a5fa"/>
      <circle cx="22" cy="21" r="1.8" fill="#3b82f6"/>
      <circle cx="18" cy="18" r="1.2" fill="#f59e0b"/>
    </svg>
    <div class="header-brand">
      <span class="header-title">IoT Edge Setup</span>
      <span class="header-sub">Metal Craft Solutions</span>
    </div>
  </div>
  <span class="header-ver">v2.0.0</span>
  <span style="margin-left:auto;font-size:11px;color:var(--sub)" id="hdr-status">Loading…</span>
  <span class="hdr-user">
    <span id="hdr-user-name" style="color:#60a5fa"></span>
    <a href="/logout" style="color:var(--sub);text-decoration:none;padding:3px 8px;border:1px solid var(--border);border-radius:5px;font-size:11px">Sign out</a>
  </span>
</div>

<div class="container">
  <div class="tabs">
    <button class="tab active" onclick="switchTab('machines')">🖥 Machines</button>
    <button class="tab" onclick="switchTab('live')">📊 Live Data</button>
    <button class="tab" onclick="switchTab('test')">🔧 Test Tools</button>
    <button class="tab" onclick="switchTab('logs')">📋 Live Logs</button>
  </div>

  <!-- ── MACHINES TAB ──────────────────────────────────────────────────────── -->
  <div class="tab-panel active" id="tab-machines">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <span style="color:var(--sub);font-size:13px" id="machine-count">No machines configured</span>
      <button class="btn btn-primary" onclick="openAddModal()">＋ Add Machine</button>
    </div>
    <div id="machine-list"></div>
  </div>

  <!-- ── LIVE DATA TAB ────────────────────────────────────────────────────── -->
  <div class="tab-panel" id="tab-live">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <span style="color:var(--sub);font-size:13px">Live machine status — updates every 3 seconds</span>
      <button class="btn btn-ghost btn-sm" onclick="loadLiveData()">↻ Refresh</button>
    </div>
    <div id="live-empty" style="display:none;text-align:center;padding:40px;color:var(--sub)">
      No collector data yet — start a collector and wait a few seconds
    </div>
    <div id="live-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px"></div>
  </div>

  <!-- ── TEST TOOLS TAB ───────────────────────────────────────────────────── -->
  <div class="tab-panel" id="tab-test">

    <!-- Ping CNC -->
    <div class="card">
      <div class="card-title">📡 Ping CNC (TCP Port Test)</div>
      <div class="form-row">
        <div class="form-group" style="flex:2">
          <label>CNC IP Address</label>
          <input id="ping-ip" placeholder="192.168.1.22" />
        </div>
        <div class="form-group" style="flex:1">
          <label>Port</label>
          <input id="ping-port" value="8193" />
        </div>
        <button class="btn btn-primary" onclick="doPing()">Ping</button>
      </div>
      <div class="result-box" id="ping-result">Result will appear here…</div>
    </div>

    <!-- Server Test -->
    <div class="card">
      <div class="card-title">🌐 Test Server Connection</div>
      <div class="form-row">
        <div class="form-group" style="flex:1">
          <label>Server URL</label>
          <input id="srv-url" placeholder="https://iot.metalcraftsolutions.xyz" />
        </div>
        <button class="btn btn-primary" onclick="doServerTest()">Test</button>
      </div>
      <div class="result-box" id="srv-result">Result will appear here…</div>
    </div>

    <!-- FOCAS Connection Test -->
    <div class="card">
      <div class="card-title">🔌 FOCAS Full Connection Test</div>
      <div class="form-row">
        <div class="form-group" style="flex:2">
          <label>CNC IP</label>
          <input id="focas-ip" placeholder="192.168.1.22" />
        </div>
        <div class="form-group" style="flex:1">
          <label>Port</label>
          <input id="focas-port" value="8193" />
        </div>
        <button class="btn btn-primary" onclick="doFocasTest()">Connect & Read</button>
      </div>
      <div class="result-box" id="focas-result">Result will appear here…</div>
    </div>

    <!-- PMC Signal Test -->
    <div class="card">
      <div class="card-title">💡 Read PMC Signal (Live)</div>
      <div class="form-grid cols3">
        <div class="form-group">
          <label>CNC IP</label>
          <input id="pmc-ip" placeholder="192.168.1.22" />
        </div>
        <div class="form-group">
          <label>PMC Type</label>
          <select id="pmc-type">
            <option value="F">F (CNC→PMC)</option>
            <option value="G">G (PMC→CNC)</option>
            <option value="X">X (Input)</option>
            <option value="Y">Y (Output)</option>
            <option value="R">R (Internal Relay)</option>
            <option value="D">D (Data Table)</option>
          </select>
        </div>
        <div class="form-group">
          <label>Port</label>
          <input id="pmc-port" value="8193" />
        </div>
        <div class="form-group">
          <label>Byte No.</label>
          <input id="pmc-byte" value="0" />
        </div>
        <div class="form-group">
          <label>Bit No. (0–7)</label>
          <input id="pmc-bit" value="7" />
        </div>
        <div class="form-group" style="justify-content:flex-end">
          <label>&nbsp;</label>
          <button class="btn btn-primary" onclick="doPmcTest()">Read Signal</button>
        </div>
      </div>
      <div class="result-box" id="pmc-result">Result will appear here…</div>
    </div>

  </div>

  <!-- ── LOGS TAB ─────────────────────────────────────────────────────────── -->
  <div class="tab-panel" id="tab-logs">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <span style="color:var(--sub);font-size:13px">Live output from all running collectors</span>
      <button class="btn btn-ghost btn-sm" onclick="clearLogs()">Clear</button>
    </div>
    <div id="log-box"></div>
  </div>
</div>

<!-- ── ADD / EDIT MACHINE MODAL ────────────────────────────────────────────── -->
<div class="modal-bg" id="machine-modal">
  <div class="modal">
    <div class="modal-title" id="modal-title">Add Machine</div>

    <div class="section-label">Machine Identity</div>
    <div class="form-grid">
      <div class="form-group">
        <label>Machine ID *</label>
        <input id="m-id" placeholder="MCH-20260515-0001" />
      </div>
      <div class="form-group">
        <label>Machine Token * (from Admin Panel)</label>
        <input id="m-token" placeholder="paste token here" type="password" />
      </div>
    </div>

    <div class="section-label">CNC Connection</div>
    <div class="form-grid cols3">
      <div class="form-group">
        <label>CNC IP Address</label>
        <input id="m-ip" value="192.168.1.22" />
      </div>
      <div class="form-group">
        <label>CNC Port</label>
        <input id="m-port" value="8193" />
      </div>
      <div class="form-group">
        <label>Server URL</label>
        <input id="m-server" placeholder="https://iot.metalcraftsolutions.xyz" />
      </div>
    </div>

    <div class="section-label">PMC Signals — Auto Execute & Cutting</div>
    <div class="form-grid" style="grid-template-columns:1fr 1fr 1fr 1fr">
      <div class="form-group"><label>Auto Exec Byte (F0.7)</label><input id="m-ae-b" value="0" /></div>
      <div class="form-group"><label>Auto Exec Bit</label><input id="m-ae-bit" value="7" /></div>
      <div class="form-group"><label>Cutting Byte (F2.6)</label><input id="m-cut-b" value="2" /></div>
      <div class="form-group"><label>Cutting Bit</label><input id="m-cut-bit" value="6" /></div>
    </div>

    <div class="section-label">PMC Signals — Spindle</div>
    <div class="form-grid" style="grid-template-columns:1fr 1fr 1fr 1fr">
      <div class="form-group"><label>Spindle CW Byte</label><input id="m-scw-b" value="70" /></div>
      <div class="form-group"><label>Spindle CW Bit</label><input id="m-scw-bit" value="5" /></div>
      <div class="form-group"><label>Spindle CCW Byte</label><input id="m-sccw-b" value="70" /></div>
      <div class="form-group"><label>Spindle CCW Bit</label><input id="m-sccw-bit" value="4" /></div>
    </div>

    <div class="section-label">PMC Signals — Feed Hold & Block Stop</div>
    <div class="form-grid" style="grid-template-columns:1fr 1fr 1fr 1fr">
      <div class="form-group"><label>Feed Hold Byte (F0.4)</label><input id="m-fh-b" value="0" /></div>
      <div class="form-group"><label>Feed Hold Bit</label><input id="m-fh-bit" value="4" /></div>
      <div class="form-group"><label>Block Stop Byte (G46.1)</label><input id="m-bs-b" value="46" /></div>
      <div class="form-group"><label>Block Stop Bit</label><input id="m-bs-bit" value="1" /></div>
    </div>

    <div class="section-label">Macro Variables</div>
    <div class="form-group">
      <label>Macro Variables (comma separated: number:label)</label>
      <input id="m-macros" value="3901:Parts Done,3902:Parts Required" />
    </div>

    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="saveMachine()" id="modal-save-btn">Save Machine</button>
    </div>
  </div>
</div>

<script>
'use strict';
const API = '/api';
let editingId = null;

// ── Tab switching ─────────────────────────────────────────────────────────────
let _liveTimer = null;
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => {
    const names = ['machines','live','test','logs'];
    t.classList.toggle('active', names[i] === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'logs')  startLogStream();
  if (name === 'live') { loadLiveData(); startLiveRefresh(); } else stopLiveRefresh();
}

// ── Live Data ─────────────────────────────────────────────────────────────────
function startLiveRefresh() {
  stopLiveRefresh();
  _liveTimer = setInterval(loadLiveData, 3000);
}
function stopLiveRefresh() {
  if (_liveTimer) { clearInterval(_liveTimer); _liveTimer = null; }
}

async function loadLiveData() {
  const grid  = document.getElementById('live-grid');
  const empty = document.getElementById('live-empty');
  try {
    const res  = await fetch('/api/live');
    const list = await res.json();
    if (!list.length) { grid.innerHTML = ''; empty.style.display = 'block'; return; }
    empty.style.display = 'none';
    grid.innerHTML = list.map(m => {
      const conn  = m.cnc_connected;
      const buf   = m.buffered > 0;
      const cls   = conn ? (buf ? 'buffering' : 'connected') : 'disconnected';
      const dot   = conn ? (buf ? '#f59e0b' : '#22c55e') : '#ef4444';
      const label = conn ? (buf ? `Buffering ${m.buffered}` : 'Connected') : 'Disconnected';
      const sig = (on, name) =>
        `<span class="sig ${on ? 'sig-on' : 'sig-off'}">${name}: ${on ? 'ON' : 'OFF'}</span>`;
      const updated = m.last_updated
        ? new Date(m.last_updated).toLocaleTimeString() : '—';
      const macros  = m.macros ? Object.entries(m.macros).map(([k,v]) =>
        `<div class="stat"><div class="stat-label">Macro #${k}</div><div class="stat-val">${v ?? '—'}</div></div>`
      ).join('') : '';
      return `<div class="live-card ${cls}">
        <div class="live-mid">${m.machine_id || '?'}</div>
        <div class="live-ip">
          <span class="live-status-dot" style="background:${dot}"></span>
          ${m.fanuc_ip || ''}:${m.fanuc_port || 8193} — <b>${label}</b>
        </div>
        <div class="live-signals">
          ${sig(m.auto_exec,  'Auto')}
          ${sig(m.cutting,    'Cutting')}
          ${sig(m.spindle_on, 'Spindle')}
          ${sig(m.feed_hold,  'FeedHold')}
          ${sig(m.alarm,      'Alarm')}
        </div>
        <div class="live-stats">
          <div class="stat">
            <div class="stat-label">Feed Rate</div>
            <div class="stat-val">${m.feed_rate ?? '—'}<span style="font-size:11px;color:var(--sub)"> mm/min</span></div>
          </div>
          <div class="stat">
            <div class="stat-label">Program #</div>
            <div class="stat-val">O${m.prog_num ?? '—'}</div>
          </div>
          <div class="stat">
            <div class="stat-label">Parts (M30)</div>
            <div class="stat-val" style="color:#22c55e">${m.parts_m30 ?? '—'}</div>
          </div>
          <div class="stat">
            <div class="stat-label">Buffered</div>
            <div class="stat-val" style="color:${buf ? '#f59e0b' : 'var(--sub)'}">${m.buffered ?? 0}</div>
          </div>
          ${macros}
        </div>
        ${m.last_error ? `<div style="margin-top:8px;font-size:11px;color:#ef4444">⚠ ${m.last_error}</div>` : ''}
        <div class="live-footer"><span>Updated: ${updated}</span><span>${m.server_online ? '☁ Cloud: Online' : '☁ Cloud: Offline'}</span></div>
      </div>`;
    }).join('');
  } catch(e) {
    grid.innerHTML = `<div style="color:var(--red);font-size:12px">Failed to load live data: ${e.message}</div>`;
  }
}

// ── Load machines ─────────────────────────────────────────────────────────────
async function loadMachines() {
  const res  = await fetch(API + '/machines');
  const list = await res.json();
  const el   = document.getElementById('machine-list');
  const cnt  = document.getElementById('machine-count');
  cnt.textContent = list.length ? `${list.length} machine${list.length > 1 ? 's' : ''} configured` : 'No machines configured';
  if (!list.length) {
    el.innerHTML = `<div style="text-align:center;padding:40px;color:var(--sub)">No machines yet — click <b>Add Machine</b> to get started</div>`;
    return;
  }
  el.innerHTML = list.map(m => `
    <div class="machine-row">
      <div class="machine-dot ${m.running ? 'dot-run' : 'dot-stop'}"></div>
      <div>
        <div class="machine-name">${m.machine_id}</div>
        <div class="machine-ip">${m.fanuc_ip}:${m.fanuc_port}</div>
      </div>
      <span class="badge ${m.running ? 'badge-run' : 'badge-stop'}">${m.running ? '● Running' : '○ Stopped'}</span>
      <div class="machine-acts">
        ${m.running
          ? `<button class="btn btn-danger btn-sm" onclick="stopMachine('${m.machine_id}')">⏹ Stop</button>`
          : `<button class="btn btn-success btn-sm" onclick="startMachine('${m.machine_id}')">▶ Start</button>`}
        <button class="btn btn-ghost btn-sm" onclick="openEditModal('${m.machine_id}')">✏ Edit</button>
        <button class="btn btn-ghost btn-sm" style="color:var(--red)" onclick="deleteMachine('${m.machine_id}')">🗑</button>
      </div>
    </div>`).join('');
}

// ── Start / Stop ──────────────────────────────────────────────────────────────
async function startMachine(id) {
  await fetch(`${API}/machines/${encodeURIComponent(id)}/start`, {method:'POST'});
  setTimeout(loadMachines, 800);
}
async function stopMachine(id) {
  await fetch(`${API}/machines/${encodeURIComponent(id)}/stop`, {method:'POST'});
  setTimeout(loadMachines, 800);
}
async function deleteMachine(id) {
  if (!confirm(`Delete machine "${id}"? This cannot be undone.`)) return;
  await fetch(`${API}/machines/${encodeURIComponent(id)}`, {method:'DELETE'});
  loadMachines();
}

// ── Modal helpers ─────────────────────────────────────────────────────────────
function openAddModal() {
  editingId = null;
  document.getElementById('modal-title').textContent = 'Add Machine';
  document.getElementById('modal-save-btn').textContent = 'Save Machine';
  ['m-id','m-token','m-ip','m-port','m-server',
   'm-ae-b','m-ae-bit','m-cut-b','m-cut-bit',
   'm-scw-b','m-scw-bit','m-sccw-b','m-sccw-bit',
   'm-fh-b','m-fh-bit','m-bs-b','m-bs-bit','m-macros'].forEach(id => {
    const el = document.getElementById(id);
    const defaults = {'m-ip':'192.168.1.22','m-port':'8193',
      'm-ae-b':'0','m-ae-bit':'7','m-cut-b':'2','m-cut-bit':'6',
      'm-scw-b':'70','m-scw-bit':'5','m-sccw-b':'70','m-sccw-bit':'4',
      'm-fh-b':'0','m-fh-bit':'4','m-bs-b':'46','m-bs-bit':'1',
      'm-macros':'3901:Parts Done,3902:Parts Required'};
    el.value = defaults[id] || '';
  });
  document.getElementById('m-id').disabled = false;
  document.getElementById('machine-modal').classList.add('show');
}

async function openEditModal(id) {
  editingId = id;
  document.getElementById('modal-title').textContent = `Edit Machine — ${id}`;
  document.getElementById('modal-save-btn').textContent = 'Update Machine';
  const res = await fetch(API + '/machines');
  const list = await res.json();
  const m = list.find(x => x.machine_id === id);
  if (!m) return;
  document.getElementById('m-id').value      = m.machine_id;
  document.getElementById('m-id').disabled   = true;
  document.getElementById('m-token').value   = '';
  document.getElementById('m-ip').value      = m.fanuc_ip;
  document.getElementById('m-port').value    = m.fanuc_port;
  document.getElementById('m-server').value  = m.server_url;
  document.getElementById('m-ae-b').value    = m.pmc_auto_exec_byte;
  document.getElementById('m-ae-bit').value  = m.pmc_auto_exec_bit;
  document.getElementById('m-cut-b').value   = m.pmc_cutting_byte;
  document.getElementById('m-cut-bit').value = m.pmc_cutting_bit;
  document.getElementById('m-scw-b').value   = m.pmc_spindle_cw_byte;
  document.getElementById('m-scw-bit').value = m.pmc_spindle_cw_bit;
  document.getElementById('m-sccw-b').value  = m.pmc_spindle_ccw_byte;
  document.getElementById('m-sccw-bit').value= m.pmc_spindle_ccw_bit;
  document.getElementById('m-fh-b').value    = m.pmc_feed_hold_byte;
  document.getElementById('m-fh-bit').value  = m.pmc_feed_hold_bit;
  document.getElementById('m-bs-b').value    = m.pmc_block_stop_byte;
  document.getElementById('m-bs-bit').value  = m.pmc_block_stop_bit;
  document.getElementById('m-macros').value  = m.macro_vars;
  document.getElementById('machine-modal').classList.add('show');
}

function closeModal() {
  document.getElementById('machine-modal').classList.remove('show');
}

async function saveMachine() {
  const data = {
    machine_id:          document.getElementById('m-id').value.trim(),
    machine_token:       document.getElementById('m-token').value.trim(),
    fanuc_ip:            document.getElementById('m-ip').value.trim(),
    fanuc_port:          document.getElementById('m-port').value.trim(),
    server_url:          document.getElementById('m-server').value.trim(),
    pmc_auto_exec_byte:  document.getElementById('m-ae-b').value,
    pmc_auto_exec_bit:   document.getElementById('m-ae-bit').value,
    pmc_cutting_byte:    document.getElementById('m-cut-b').value,
    pmc_cutting_bit:     document.getElementById('m-cut-bit').value,
    pmc_spindle_cw_byte: document.getElementById('m-scw-b').value,
    pmc_spindle_cw_bit:  document.getElementById('m-scw-bit').value,
    pmc_spindle_ccw_byte:document.getElementById('m-sccw-b').value,
    pmc_spindle_ccw_bit: document.getElementById('m-sccw-bit').value,
    pmc_feed_hold_byte:  document.getElementById('m-fh-b').value,
    pmc_feed_hold_bit:   document.getElementById('m-fh-bit').value,
    pmc_block_stop_byte: document.getElementById('m-bs-b').value,
    pmc_block_stop_bit:  document.getElementById('m-bs-bit').value,
    macro_vars:          document.getElementById('m-macros').value.trim(),
  };
  if (!data.machine_id) { alert('Machine ID is required'); return; }
  const url    = editingId
    ? `${API}/machines/${encodeURIComponent(editingId)}`
    : `${API}/machines`;
  const method = editingId ? 'PUT' : 'POST';
  const res    = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  const json   = await res.json();
  if (json.error) { alert('Error: ' + json.error); return; }
  closeModal();
  loadMachines();
}

// ── Test tools ────────────────────────────────────────────────────────────────
async function doPing() {
  const el = document.getElementById('ping-result');
  el.className = 'result-box'; el.textContent = 'Testing…';
  const res  = await fetch(`${API}/test/ping`, {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ip: document.getElementById('ping-ip').value,
                          port: document.getElementById('ping-port').value})});
  const d = await res.json();
  el.className = 'result-box ' + (d.ok ? 'result-ok' : 'result-err');
  el.textContent = d.ok ? `✅ ${d.message}` : `❌ ${d.message}`;
}

async function doServerTest() {
  const el = document.getElementById('srv-result');
  el.className = 'result-box'; el.textContent = 'Testing…';
  const res = await fetch(`${API}/test/server`, {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url: document.getElementById('srv-url').value})});
  const d = await res.json();
  el.className = 'result-box ' + (d.ok ? 'result-ok' : 'result-err');
  el.textContent = d.ok
    ? `✅ ${d.message}\n${JSON.stringify(d.response, null, 2)}`
    : `❌ ${d.message}`;
}

async function doFocasTest() {
  const el = document.getElementById('focas-result');
  el.className = 'result-box'; el.textContent = 'Connecting to CNC…';
  const res = await fetch(`${API}/test/focas`, {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ip: document.getElementById('focas-ip').value,
                          port: document.getElementById('focas-port').value})});
  const d = await res.json();
  el.className = 'result-box ' + (d.ok ? 'result-ok' : 'result-err');
  el.textContent = d.ok
    ? `✅ ${d.message}\n\nLive CNC Data:\n${JSON.stringify(d.data, null, 2)}`
    : `❌ ${d.message}`;
}

async function doPmcTest() {
  const el = document.getElementById('pmc-result');
  el.className = 'result-box'; el.textContent = 'Reading PMC signal…';
  const res = await fetch(`${API}/test/pmc`, {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      ip:       document.getElementById('pmc-ip').value,
      port:     document.getElementById('pmc-port').value,
      pmc_type: document.getElementById('pmc-type').value,
      byte_no:  document.getElementById('pmc-byte').value,
      bit_no:   document.getElementById('pmc-bit').value,
    })});
  const d = await res.json();
  el.className = 'result-box ' + (d.ok ? 'result-ok' : 'result-err');
  el.textContent = d.ok
    ? `✅ ${d.message}\n\nValue = ${d.value} (${d.value ? 'ON / TRUE' : 'OFF / FALSE'})`
    : `❌ ${d.message}`;
}

// ── Log stream (SSE) ──────────────────────────────────────────────────────────
let _es = null;
function startLogStream() {
  if (_es) return;
  _es = new EventSource('/api/logs');
  _es.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.msg === 'ping') return;
    const box  = document.getElementById('log-box');
    const line = document.createElement('div');
    line.className = 'log-line' +
      (d.msg.includes('✅') || d.msg.includes('OK') ? ' ok' : '') +
      (d.msg.includes('❌') || d.msg.includes('ERROR') || d.msg.includes('FAILED') ? ' err' : '');
    line.textContent = d.msg;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
    // Keep max 500 lines
    while (box.children.length > 500) box.removeChild(box.firstChild);
  };
}
function clearLogs() { document.getElementById('log-box').innerHTML = ''; }

// ── Header status ─────────────────────────────────────────────────────────────
async function updateHeaderStatus() {
  try {
    const res = await fetch(API + '/status');
    const d   = await res.json();
    const run = d.machines.filter(m => m.running).length;
    document.getElementById('hdr-status').textContent =
      `${d.machines.length} machine(s) · ${run} running`;
  } catch {}
}

// ── Boot ──────────────────────────────────────────────────────────────────────
loadMachines();
updateHeaderStatus();
setInterval(loadMachines, 5000);
setInterval(updateHeaderStatus, 5000);
// Show logged-in username in header
fetch('/api/status').then(r=>r.json()).then(d=>{
  const el = document.getElementById('hdr-user-name');
  if(el && d.username) el.textContent = d.username;
}).catch(()=>{});
</script>
</body>
</html>"""

@app.route("/")
def index():
    return HTML


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _migrate_encodings()   # fix any legacy cp1252 files on first run
    _log(f"FANUC IoT Edge GUI v{APP_VERSION} starting on http://localhost:{GUI_PORT}")
    # Auto-open browser
    def _open_browser():
        time.sleep(1.2)
        import webbrowser
        webbrowser.open(f"http://localhost:{GUI_PORT}")
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=GUI_PORT, debug=False, threaded=True)
