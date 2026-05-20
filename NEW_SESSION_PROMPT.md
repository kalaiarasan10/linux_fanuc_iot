# New Session Knowledge Brief — FANUC IoT Edge Linux
# =====================================================
# Paste this entire file as the first message in a new Claude Code session.
# It contains everything needed to continue without re-explaining anything.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 1 — WHAT THIS PROJECT IS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

We built a complete FANUC CNC machine data collection system for a factory.
It reads live data from FANUC CNC machines and sends to a cloud dashboard.

THREE REPOS:

1. FanucIoTEdge_Setup (Windows Edge App) — WORKING/DEPLOYED
   github.com/kalaiarasan10/FanucIoTEdge_Setup
   - Runs on Windows PC connected to factory network
   - manager.py    → orchestrator, starts GUI + one collector per machine
   - collector.py  → polls CNC every 500ms, sends to cloud every 5s
   - local_gui.py  → Flask web UI at localhost:8765 for setup/config
   - focas_reader.py → reads CNC data via FANUC Focas2 library
   - Auto-starts on boot via Task Scheduler + launch_hidden.vbs (silent)
   - Uses chattertools pip package (bundles Fwlib64.dll for Windows)

2. fanuc-iot (Cloud Dashboard) — WORKING/DEPLOYED on VPS
   github.com/kalaiarasan10/fanuc-iot
   - FastAPI backend + HTML/JS frontend
   - Branding: Metal Craft Solutions (metalcraftsolutions.xyz)
   - Shows live: spindle, feed, alarms, positions, net cycle time, M30 count
   - Runs via: docker compose up -d

3. linux_fanuc_iot (Linux Edge App) — THIS PROJECT, IN PROGRESS
   github.com/kalaiarasan10/linux_fanuc_iot
   - Same as FanucIoTEdge_Setup but for Linux (Raspberry Pi / Ubuntu)
   - Uses libfwlib32.so via raw ctypes (no chattertools on Linux)
   - Files already copied from Windows project
   - Bugs to fix in focas_reader.py Linux branch (see Part 4)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 2 — HOW THE FOCAS2 REVERSE ENGINEERING WAS DONE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FANUC Focas2 is a proprietary C library. There are no Python bindings.
We had to reverse engineer the struct layouts by trial and error + manual.

STEP-BY-STEP METHOD USED:

Step 1 — Read FANUC Focas2 PDF manual
  The manual gives C struct definitions for each function.
  Example from manual for cnc_rdmacro:
    typedef struct odbmacro {
        long   datano;   /* macro variable number */
        short  dummy;    /* unused */
        short  dec;      /* decimal places */
        long   mcr_val;  /* raw value */
        short  type;     /* 9 = undefined */
    } ODBMACRO;

Step 2 — Critical Linux problem: c_long size
  On Windows:  c_long = 4 bytes (correct for FOCAS)
  On Linux x64: c_long = 8 bytes (WRONG — breaks all structs)
  FIX: on Linux, import c_int32 as c_long at the top of every file
    from ctypes import c_int32 as c_long   # Linux FOCAS fix

Step 3 — Add _pack_ = 1 to all structs
  Without _pack_=1, Python ctypes adds padding bytes between fields.
  FOCAS structs are tightly packed (no padding).
  Every struct must have _pack_ = 1

Step 4 — Add _layout_ = 'ms' for Linux structs
  On Linux only, ctypes needs _layout_ = 'ms' (Microsoft layout)
  to match how FANUC compiled their library.
  Windows structs work without this. Linux structs need it.

Step 5 — Verify with sizeof()
  After defining each struct, print sizeof() and compare to manual.
  Example: sizeof(ODBMACRO) should be 14 bytes
    datano(4) + dummy(2) + dec(2) + mcr_val(4) + type(2) = 14

Step 6 — Print raw bytes to debug field alignment
  When a field returns garbage, dump raw bytes:
    import ctypes
    buf = ODBMACRO()
    raw = bytes(ctypes.string_at(ctypes.addressof(buf), ctypes.sizeof(buf)))
    print(raw.hex())
  Then manually count byte positions to find misalignment.

Step 7 — Function signatures must be set explicitly
  FANUC functions return c_short (error code, 0 = OK).
  You MUST set .restype and .argtypes before calling, otherwise
  ctypes assumes int return and corrupts the stack.
  Example:
    lib.cnc_rdmacro.restype  = c_short
    lib.cnc_rdmacro.argtypes = [c_ushort, c_long, c_short, POINTER(ODBMACRO)]

Step 8 — Load .so with RTLD_LAZY mode
  libfwlib32.so has unresolved symbols that cause RTLD_NOW to fail.
  MUST use mode=0x00001 (RTLD_LAZY):
    lib = ctypes.CDLL("/path/to/libfwlib32.so", mode=0x00001)

Step 9 — Connect to CNC
  lib.cnc_allclibhndl3(ip_bytes, port, timeout, byref(handle))
  Returns 0 on success. Handle is c_ushort passed to all functions.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 3 — ALL VERIFIED STRUCT LAYOUTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

These are all verified working on Linux. Use exactly as shown.
All use: from ctypes import c_int32 as c_long  (Linux fix)

-- PMC read struct (pmc_rdpmcrng) --
class IODBPMC(Structure):
    _pack_ = 1
    _layout_ = 'ms'
    _fields_ = [
        ("type",     c_short),
        ("_r",       c_short),
        ("datano_s", c_short),
        ("datano_e", c_short),
        ("cdata",    c_uint8 * 256),
    ]
Usage: pmc_rdpmcrng(handle, type, 0, byte_no, byte_no, 9, byref(buf))
Bit read: (buf.cdata[0] >> bit_no) & 1

-- Feed rate struct (cnc_actf) --
class ODBACT(Structure):
    _pack_ = 1
    _layout_ = 'ms'
    _fields_ = [("data", c_long)]   # c_long = c_int32 on Linux
NOTE: On Linux use cnc_actf, NOT cnc_rdactf (that's Windows only)

-- Macro variable struct (cnc_rdmacro) --
class ODBMACRO(Structure):
    _pack_ = 1
    _layout_ = 'ms'
    _fields_ = [
        ("datano",  c_long),    # macro variable number
        ("dummy",   c_short),   # unused
        ("dec",     c_short),   # decimal places
        ("mcr_val", c_long),    # raw integer value
        ("type",    c_short),   # 9 = undefined/null
    ]
Read value: buf.mcr_val / (10 ** buf.dec)
Undefined check: if buf.type == 9: skip

-- Macro write struct (cnc_wrmacro) --
class IODBMR(Structure):
    _pack_ = 1
    _layout_ = 'ms'
    _fields_ = [
        ("datano", c_long),
        ("type",   c_short),
        ("data",   c_long),
        ("dec",    c_short),
    ]

-- Alarm message struct (cnc_rdalmmsg2) --
class ODBALMMSG2(Structure):
    _pack_ = 1
    _layout_ = 'ms'
    _fields_ = [
        ("alm_no",  c_long),
        ("type",    c_short),
        ("axis",    c_short),
        ("msg_len", c_short),
        ("msg",     c_char * 32),
    ]
Usage: cnc_rdalmmsg2(handle, -1, byref(num), byref(buf))
num = c_short(1) — max alarms to read

-- Servo load struct (cnc_rdsvmeter) --
class LOADELM(Structure):
    _pack_ = 1
    _layout_ = 'ms'
    _fields_ = [
        ("data",    c_long),
        ("dec",     c_short),
        ("unit",    c_short),
        ("name",    c_uint8),   # axis name ASCII: X=0x58, Y=0x59, Z=0x5A
        ("suff",    c_uint8),
        ("reserve", c_uint8 * 2),
    ]
class ODBSVLOAD(Structure):
    _pack_ = 1
    _layout_ = 'ms'
    _fields_ = [("svload", LOADELM * 8)]   # MAX_AXIS = 8
Load value: round(elm.data / (10 ** max(elm.dec, 0)))
Skip garbage: if abs(load) > 500: continue

-- Speed struct (cnc_rdspeed) --
class SPEEDELM(Structure):
    _pack_ = 1
    _layout_ = 'ms'
    _fields_ = [
        ("data", c_long),
        ("dec",  c_short),
        ("unit", c_short),
        ("name", c_uint8),
        ("suff", c_uint8 * 3),
    ]
class ODBSPEED(Structure):
    _pack_ = 1
    _layout_ = 'ms'
    _fields_ = [
        ("actf", SPEEDELM),   # actual feed
        ("acts", SPEEDELM),   # actual spindle
    ]

-- Program number (cnc_rdprgnum on Linux) --
Linux uses a different signature than Windows:
    lib.cnc_rdprgnum.argtypes = [c_ushort, POINTER(c_short)]
    buf = (c_short * 2)()
    ret = lib.cnc_rdprgnum(handle, byref(buf))
    program_no = int(buf[0])

-- Tool offset (cnc_rdtofs) --
class ODBTOFS(Structure):
    _pack_ = 1
    _layout_ = 'ms'
    _fields_ = [
        ("datano", c_short),
        ("type",   c_short),
        ("data",   c_long),
        ("dec",    c_short),
    ]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 4 — LINUX BUGS STILL TO FIX
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BUG 1: Macro #3901 / #3902 returns 0.0
  Symptom: cnc_rdmacro returns 0 on success but value is always 0.0
  Root cause: ODBMACRO struct in focas_reader.py has wrong layout
  The current focas_reader.py ODBMACRO:
    ("datano", c_long), ("dummy", c_short), ("dec", c_short),
    ("mcr_val", c_long), ("type", c_short)
  This may work on Windows but dec field returns garbage on Linux.
  Fix to try: compare sizeof(ODBMACRO) against manual (should be 14 bytes)
  Also verify the fanuc_connection.py version (from WSL) which is known good.

BUG 2: Program number returns None
  Symptom: _read_prog() returns 0 always on Linux
  Root cause: Windows focas_reader.py uses ODBPRO struct:
    class ODBPRO(Structure):
        _pack_ = 1
        _fields_ = [("dummy",c_long),("data",c_short),("mdata",c_short)]
  But Linux cnc_rdprgnum has DIFFERENT signature:
    argtypes = [c_ushort, POINTER(c_short)]  ← just a short array, no struct
  Fix: on Linux, use (c_short * 2)() buffer, read buf[0] directly.
  The fanuc_connection.py in WSL has the correct Linux implementation.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 5 — FILE LOCATIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

WINDOWS DEV MACHINE:

  Linux project (main work here):
    C:\Users\Dell\python\linux_fanuc_iot\
      src\focas_reader.py   ← FIX THIS (Linux branch has bugs)
      src\collector.py      ← works, minor Linux path fix needed
      src\manager.py        ← works, no GUI auto-open on Linux
      src\local_gui.py      ← works on Linux
      lib\libfwlib32.so     ← the Linux .so file (armv7 for Pi)
      install.sh            ← one-command installer for Pi/Ubuntu
      installer\fanuciotedge.service ← systemd auto-start

  WSL Ubuntu (reference + testing):
    ~/linux_focas/
      fanuc_connection.py   ← REFERENCE: correct Linux struct layouts
      test_connection.py    ← REFERENCE: test all reads
      lib\libfwlib32.so     ← same .so file
    Access from Windows PowerShell: wsl -e bash -c "..."
    Access in WSL: /mnt/c/Users/Dell/python/linux_fanuc_iot/

  Windows project (source of truth for working code):
    C:\Users\Dell\python\FanucIoTEdge_Setup\src\
      focas_reader.py  ← Windows branch is verified working

.so FILES AVAILABLE (inside chattertools pip package):
  C:\Users\Dell\python\new_focas_turbo\venv\Lib\site-packages\chattertools\lib\
    libfwlib32-linux-armv7.so.1.0.5   ← Raspberry Pi (use this for Pi Zero)
    libfwlib32-linux-x64.so.1.0.5     ← Ubuntu/Debian x64
    libfwlib32-linux-x86.so.1.0.5     ← Linux 32-bit

  To copy x64 version:
    Copy to: lib\libfwlib32.so (rename when copying)

GITHUB REPOS:
  github.com/kalaiarasan10/linux_fanuc_iot   ← this project
  github.com/kalaiarasan10/FanucIoTEdge_Setup ← Windows reference
  github.com/kalaiarasan10/fanuc-iot          ← cloud dashboard

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 6 — PMC SIGNAL MAP (verified working)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

These PMC addresses are confirmed working on the Sumitomo FANUC machine:

  F0.7  = Auto executing (program running)
  F2.6  = Cutting active
  G70.5 = Spindle CW
  G70.4 = Spindle CCW
  F0.4  = Feed hold active
  G46.1 = Block stop (M00/M01/single block)

M30 detection method:
  Track F0.7 (auto_exec) — when it goes True→False, M30 fired.
  Increment parts counter on falling edge.
  This is more reliable than watching M30 signal directly.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 7 — COLLECTOR LOGIC (how data flows)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

collector.py polls CNC every 500ms (FAST_POLL_SEC = 0.5):
  - Reads: auto_exec, cutting, spindle_on, feed_hold, block_stop
  - Reads: prog_num, feed_rate, alarm, macro vars (#3901, #3902)
  - Counts ticks for each signal being True

Every 10 ticks (5 seconds) sends summary to cloud:
  - gross_cycle_sec = auto_exec ticks * 0.5
  - cutting_sec     = cutting ticks * 0.5
  - feed_hold_sec   = feed_hold ticks * 0.5
  - net_cycle_sec   = gross - feed_hold - block_stop (true productive time)

On M30 (falling edge of auto_exec):
  Sends INSTANT cycle_complete event to cloud (not waiting for 5s window)

Offline handling:
  If server unreachable → saves to SQLite local buffer
  When back online → automatically flushes buffer to server

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 8 — START HERE (next steps in order)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Read src/focas_reader.py Linux branch
   Compare ODBMACRO struct against fanuc_connection.py in WSL
   Fix the struct layout so macro reads work

2. Fix program number read on Linux
   Replace ODBPRO struct approach with (c_short * 2)() buffer
   Match the fanuc_connection.py get_current_program() method

3. Test on WSL Ubuntu:
   wsl -e bash -c "cd /mnt/c/Users/Dell/python/linux_fanuc_iot && python3 src/focas_reader.py"
   Or run test against real CNC if on factory network

4. Fix collector.py Linux path issue:
   _get_status_dir() uses Windows path check (os.path.splitext .exe)
   On Linux, always use the relative path branch

5. Test full stack on WSL:
   wsl -e bash -c "cd /mnt/c/Users/Dell/python/linux_fanuc_iot && python3 src/manager.py"

6. Deploy to Raspberry Pi:
   git clone https://github.com/kalaiarasan10/linux_fanuc_iot
   sudo bash install.sh

7. Set up systemd auto-start on Pi:
   Service file already written: installer/fanuciotedge.service
   install.sh handles this automatically

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEVELOPER INFO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GitHub:  kalaiarasan10
Email:   kalaiarasansathish.08@gmail.com
Company: Metal Craft Solutions
Python:  3.10 (Windows), 3.14 (WSL Ubuntu), 3.x (Raspberry Pi)
WSL:     Ubuntu on Windows (wsl --list shows Ubuntu as default)
