"""
collector.py  —  FANUC IoT Edge Device v2
==========================================
Single-machine data collector.
- Polls CNC every 500ms for accurate signal tracking
- Summarises every 5 seconds → sends to server
- M30 detected → sends instant cycle-complete event
- Offline → saves to SQLite buffer → auto-retries

Called by manager.py (one process per machine)
or run standalone: python collector.py --config machines/CNC-1.env
"""

import os
import sys
import time
import json
import argparse
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

from focas_reader import FocasReader
from local_buffer  import save as buf_save, get_unsent, mark_sent, pending_count, cleanup

# ── Status file path (read by GUI live-data tab) ──────────────────────────────
def _get_status_dir() -> str:
    is_exe = os.path.splitext(sys.executable)[1].lower() == ".exe"
    if is_exe:
        d = os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "FanucIoTEdge", "status")
    else:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "status")
    os.makedirs(d, exist_ok=True)
    return d

_STATUS_DIR = _get_status_dir()


def _write_status(machine_id: str, data: dict, buffered: int, server_online: bool, error: str):
    """Write machine status JSON for the GUI live-data tab."""
    try:
        payload = {
            "machine_id":   machine_id,
            "fanuc_ip":     FANUC_IP,
            "fanuc_port":   FANUC_PORT,
            "cnc_connected": data is not None,
            "auto_exec":    data.get("auto_exec",  False) if data else False,
            "cutting":      data.get("cutting",    False) if data else False,
            "spindle_on":   data.get("spindle_on", False) if data else False,
            "feed_hold":    data.get("feed_hold",  False) if data else False,
            "block_stop":   data.get("block_stop", False) if data else False,
            "alarm":        data.get("alarm",      False) if data else False,
            "feed_rate":    data.get("feed_rate",  0)     if data else 0,
            "prog_num":     data.get("prog_num",   0)     if data else 0,
            "parts_m30":    data.get("parts_m30",  0)     if data else 0,
            "macros":       data.get("macros",     {})    if data else {},
            "buffered":     buffered,
            "server_online": server_online,
            "last_error":   error,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in machine_id)
        path = os.path.join(_STATUS_DIR, f"{safe_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass

# ── Parse arguments ───────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--config", default=None, help="Path to machine config .env file")
args, _ = parser.parse_known_args()

# ── Load config ───────────────────────────────────────────────────────────────
if args.config and os.path.exists(args.config):
    load_dotenv(args.config, override=True)
else:
    # Fallback: look for config.env in same dir
    load_dotenv(os.path.join(os.path.dirname(__file__), "config.env"), override=True)

MACHINE_ID    = os.getenv("MACHINE_ID")
MACHINE_TOKEN = os.getenv("MACHINE_TOKEN")
FANUC_IP      = os.getenv("FANUC_IP",      "192.168.1.22")
FANUC_PORT    = int(os.getenv("FANUC_PORT",    "8193"))
FANUC_TIMEOUT = int(os.getenv("FANUC_TIMEOUT", "10"))
SERVER_URL    = os.getenv("SERVER_URL",    "https://iot.metalcraftsolutions.xyz")

# ── Timing ────────────────────────────────────────────────────────────────────
FAST_POLL_SEC       = 0.5   # read CNC every 500ms
FAST_TICKS_PER_SEND = 10    # send every 10 ticks = 5 seconds

# ── PMC signal addresses ──────────────────────────────────────────────────────
SIG_CFG = {
    "auto_exec_byte":   int(os.getenv("PMC_AUTO_EXEC_BYTE",   "0")),
    "auto_exec_bit":    int(os.getenv("PMC_AUTO_EXEC_BIT",    "7")),
    "cutting_byte":     int(os.getenv("PMC_CUTTING_BYTE",     "2")),
    "cutting_bit":      int(os.getenv("PMC_CUTTING_BIT",      "6")),
    "spindle_cw_byte":  int(os.getenv("PMC_SPINDLE_CW_BYTE",  "70")),
    "spindle_cw_bit":   int(os.getenv("PMC_SPINDLE_CW_BIT",   "5")),
    "spindle_ccw_byte": int(os.getenv("PMC_SPINDLE_CCW_BYTE", "70")),
    "spindle_ccw_bit":  int(os.getenv("PMC_SPINDLE_CCW_BIT",  "4")),
    "feed_hold_byte":   int(os.getenv("PMC_FEED_HOLD_BYTE",   "0")),
    "feed_hold_bit":    int(os.getenv("PMC_FEED_HOLD_BIT",    "4")),
    "block_stop_byte":  int(os.getenv("PMC_BLOCK_STOP_BYTE",  "46")),
    "block_stop_bit":   int(os.getenv("PMC_BLOCK_STOP_BIT",   "1")),
}

def parse_macros() -> list:
    raw = os.getenv("MACRO_VARS", "3901:Parts Done,3902:Parts Required")
    result = []
    for item in raw.split(","):
        try:
            result.append(int(item.split(":")[0].strip()))
        except Exception:
            pass
    return result

MACRO_VARS = parse_macros()

HEADERS = {
    "Authorization": f"Bearer {MACHINE_TOKEN}",
    "Content-Type":  "application/json",
}


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def send_to_server(payload: dict) -> bool:
    try:
        r = requests.post(
            f"{SERVER_URL}/api/v1/ingest",
            json=payload, headers=HEADERS, timeout=8,
        )
        return r.status_code == 200
    except Exception:
        return False


def flush_buffer():
    unsent = get_unsent(MACHINE_ID, limit=50)
    if not unsent:
        return
    sent_ids = []
    for row_id, payload in unsent:
        if send_to_server(payload):
            sent_ids.append(row_id)
        else:
            break
    if sent_ids:
        mark_sent(sent_ids)
        print(f"  [BUFFER] Flushed {len(sent_ids)} records")


def poll_pending(reader: FocasReader):
    try:
        r = requests.get(
            f"{SERVER_URL}/api/v1/pending/{MACHINE_ID}",
            headers=HEADERS, timeout=5,
        )
        if r.status_code != 200:
            return
        data = r.json()

        approval = data.get("pending_approval")
        if approval is not None:
            byte_no = data.get("approval_pmc_byte", 2500)
            bit_no  = data.get("approval_pmc_bit",  0)
            reader.write_pmc_bit("R", byte_no, bit_no, int(approval))
            print(f"  [PMC WRITE] R{byte_no}.{bit_no} = {approval}")

        for mw in data.get("pending_macro_writes") or []:
            num = mw.get("num")
            val = mw.get("value")
            if num is not None and val is not None:
                ok = reader.write_macro(int(num), float(val))
                print(f"  [MACRO WRITE] #{num} = {val} → {'OK' if ok else 'FAILED'}")
    except Exception as e:
        print(f"  [PENDING POLL ERROR] {e}")


# ── Build payloads ────────────────────────────────────────────────────────────
def build_summary(ticks, ae_count, cut_count, spin_count, fh_count, bs_count, last):
    gross_sec = round(ae_count   * FAST_POLL_SEC, 2)
    cut_sec   = round(cut_count  * FAST_POLL_SEC, 2)
    spin_sec  = round(spin_count * FAST_POLL_SEC, 2)
    fh_sec    = round(fh_count   * FAST_POLL_SEC, 2)
    bs_sec    = round(bs_count   * FAST_POLL_SEC, 2)
    net_sec   = round(max(gross_sec - fh_sec - bs_sec, 0), 2)
    return {
        "machine_id": MACHINE_ID,
        "ts": datetime.now(timezone.utc).isoformat(),
        "auto_exec":  last["auto_exec"],  "cutting":    last["cutting"],
        "spindle_on": last["spindle_on"], "feed_hold":  last["feed_hold"],
        "block_stop": last["block_stop"], "prog_num":   last["prog_num"],
        "feed_rate":  last["feed_rate"],  "alarm":      last["alarm"],
        "parts_m30":  last["parts_m30"],  "macros":     last["macros"],
        "window_sec":      ticks * FAST_POLL_SEC,
        "gross_cycle_sec": gross_sec, "cutting_sec":   cut_sec,
        "spindle_sec":     spin_sec,  "feed_hold_sec": fh_sec,
        "block_stop_sec":  bs_sec,    "net_cycle_sec": net_sec,
        "cycle_complete":  False,
    }


def build_cycle_complete(last):
    return {
        "machine_id": MACHINE_ID,
        "ts": datetime.now(timezone.utc).isoformat(),
        "auto_exec":  last["auto_exec"],  "cutting":    last["cutting"],
        "spindle_on": last["spindle_on"], "feed_hold":  last["feed_hold"],
        "block_stop": last["block_stop"], "prog_num":   last["prog_num"],
        "feed_rate":  last["feed_rate"],  "alarm":      last["alarm"],
        "parts_m30":  last["parts_m30"],  "macros":     last["macros"],
        "window_sec": 0, "gross_cycle_sec": 0, "cutting_sec": 0,
        "spindle_sec": 0, "feed_hold_sec": 0, "block_stop_sec": 0,
        "net_cycle_sec": 0, "cycle_complete": True,
    }


# ── Shared status dict (read by local_gui.py via manager) ─────────────────────
_STATUS = {
    "machine_id":  MACHINE_ID or "unknown",
    "online":      False,
    "cnc_connected": False,
    "last_data":   {},
    "buffered":    0,
    "last_error":  "",
    "started_at":  datetime.now(timezone.utc).isoformat(),
}

def get_status() -> dict:
    return _STATUS.copy()


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    if not MACHINE_ID or not MACHINE_TOKEN:
        print("ERROR: MACHINE_ID and MACHINE_TOKEN must be set in config file")
        sys.exit(1)

    print(f"======================================")
    print(f"   FANUC IoT Edge  v2")
    print(f"   Machine : {MACHINE_ID}")
    print(f"   CNC     : {FANUC_IP}:{FANUC_PORT}")
    print(f"   Server  : {SERVER_URL}")
    print(f"======================================")

    reader = FocasReader(ip=FANUC_IP, port=FANUC_PORT, timeout=FANUC_TIMEOUT)
    _STATUS["machine_id"] = MACHINE_ID

    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Connecting to CNC {FANUC_IP}...")
            reader.connect()
            print("  ✅ CNC Connected")
            _STATUS["cnc_connected"] = True
            _STATUS["last_error"]    = ""
        except Exception as e:
            _STATUS["cnc_connected"] = False
            _STATUS["last_error"]    = str(e)
            print(f"  ❌ CNC connection failed: {e}  — retry in 30s")
            time.sleep(30)
            continue

        tick = ae_count = cut_count = spin_count = fh_count = bs_count = 0
        last_data = {}

        while True:
            t0 = time.monotonic()
            try:
                data      = reader.read_all(MACRO_VARS, SIG_CFG)
                last_data = data
                _STATUS["last_data"] = data
                _STATUS["online"]    = True

                if data["auto_exec"]:  ae_count   += 1
                if data["cutting"]:    cut_count  += 1
                if data["spindle_on"]: spin_count += 1
                if data["feed_hold"]:  fh_count   += 1
                if data["block_stop"]: bs_count   += 1

                if data["m30_fired"]:
                    cc = build_cycle_complete(data)
                    if send_to_server(cc):
                        print(f"  [M30] Instant event sent (parts: {data['parts_m30']})")
                    else:
                        buf_save(MACHINE_ID, cc)
                        print(f"  [M30] Offline — buffered")

                tick += 1
                if tick % FAST_TICKS_PER_SEND == 0:
                    summary = build_summary(
                        FAST_TICKS_PER_SEND,
                        ae_count, cut_count, spin_count, fh_count, bs_count,
                        last_data,
                    )
                    status  = "AUTO" if last_data.get("auto_exec") else "IDLE"
                    fh_str  = f" FH:{summary['feed_hold_sec']}s" if fh_count > 0 else ""
                    print(
                        f"  [{datetime.now().strftime('%H:%M:%S')}] "
                        f"{status} | Feed:{last_data.get('feed_rate',0)} "
                        f"| O{last_data.get('prog_num',0)} "
                        f"| Parts:{last_data.get('parts_m30',0)} "
                        f"| Net:{summary['net_cycle_sec']}s{fh_str}"
                    )

                    if send_to_server(summary):
                        _STATUS["buffered"] = 0
                        if pending_count(MACHINE_ID) > 0:
                            flush_buffer()
                        _write_status(MACHINE_ID, last_data, 0, True, "")
                    else:
                        buf_save(MACHINE_ID, summary)
                        _STATUS["buffered"] = pending_count(MACHINE_ID)
                        print(f"  [OFFLINE] Buffered ({_STATUS['buffered']} pending)")
                        _write_status(MACHINE_ID, last_data, _STATUS["buffered"], False, "")

                    poll_pending(reader)
                    ae_count = cut_count = spin_count = fh_count = bs_count = 0

                if tick % 7200 == 0:
                    cleanup()

                elapsed = time.monotonic() - t0
                time.sleep(max(FAST_POLL_SEC - elapsed, 0))

            except KeyboardInterrupt:
                print("\nStopped.")
                reader.disconnect()
                sys.exit(0)
            except Exception as e:
                _STATUS["cnc_connected"] = False
                _STATUS["last_error"]    = str(e)
                print(f"  ❌ Read error: {e} — reconnecting...")
                _write_status(MACHINE_ID, None, _STATUS["buffered"], False, str(e)[:120])
                reader.disconnect()
                time.sleep(5)
                break


if __name__ == "__main__":
    main()
