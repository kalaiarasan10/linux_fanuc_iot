"""
read_param.py — Read FANUC CNC Machine Parameters
===================================================
Reads parameter numbers 6710 and 6750 from CNC via Focas2.
Run: python read_param.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import ctypes
from ctypes import (Structure, c_short, c_ushort, c_long, c_int32,
                    c_uint8, c_char, byref, POINTER, sizeof, Union)

# ── Config ────────────────────────────────────────────────────────────────────
CNC_IP      = "192.168.1.22"
CNC_PORT    = 8193
TIMEOUT     = 10
PARAMS      = [1815]   # parameters to read
MAX_AXIS    = 8

# ── Load library ──────────────────────────────────────────────────────────────
import chattertools as ch

# ── IODBPARAM struct ──────────────────────────────────────────────────────────
class _U(Union):
    _pack_ = 1
    _fields_ = [
        ("cdata",  c_uint8),
        ("idata",  c_short),
        ("ldata",  c_long),
        ("cdatas", c_uint8  * MAX_AXIS),
        ("idatas", c_short  * MAX_AXIS),
        ("ldatas", c_long   * MAX_AXIS),
    ]

class IODBPARAM(Structure):
    _pack_ = 1
    _fields_ = [
        ("datano", c_short),
        ("type",   c_short),
        ("u",      _U),
    ]

# ── Connect ───────────────────────────────────────────────────────────────────
print(f"Connecting to {CNC_IP}:{CNC_PORT}...")
try:
    focas  = ch.Focas(ip=CNC_IP, port=CNC_PORT, timeout=TIMEOUT)
    lib    = focas.fwlib
    handle = focas.handle
    print("Connected OK\n")
except Exception as e:
    print(f"Connection failed: {e}")
    sys.exit(1)

# ── Setup cnc_rdparam ─────────────────────────────────────────────────────────
lib.cnc_rdparam.restype  = c_short
lib.cnc_rdparam.argtypes = [
    c_ushort,   # handle
    c_short,    # param number
    c_short,    # axis (0 = non-axis, -1 = all axes)
    c_short,    # data length
    POINTER(IODBPARAM),
]

# ── Read each parameter ───────────────────────────────────────────────────────
AXIS_NAMES = {0: "all", 1: "X", 2: "Y", 3: "Z", 4: "A", 5: "B"}

print(f"{'Param':<10} {'Axis':<6} {'byte':<10} {'short':<12} {'long':<12}")
print("-" * 54)

for param_no in PARAMS:
    found = False
    # First try axis=0 (non-axis parameter)
    for axis in [0, 1, 2, 3, 4, 5]:
        buf = IODBPARAM()
        ret = lib.cnc_rdparam(
            handle,
            c_short(param_no),
            c_short(axis),
            c_short(sizeof(IODBPARAM)),
            byref(buf)
        )
        if ret == 0:
            aname = AXIS_NAMES.get(axis, str(axis))
            print(f"#{param_no:<9} {aname:<6} "
                  f"{buf.u.cdata:<10} "
                  f"{buf.u.idata:<12} "
                  f"{buf.u.ldata}")
            found = True
            # if axis=0 worked it's a non-axis param, no need to loop axes
            if axis == 0:
                break
        else:
            # axis=0 failed → it's an axis param, continue to per-axis reads
            if axis == 0:
                continue
            # axis 1+ failed → no more axes
            else:
                break
    if not found:
        print(f"#{param_no:<9} ERROR — not readable (code={ret})")

print()

# ── Disconnect ────────────────────────────────────────────────────────────────
try:
    lib.cnc_freelibhndl(handle)
except Exception:
    pass
print("Done.")
