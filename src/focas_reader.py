"""
focas_reader.py
================
Reads all data from FANUC CNC via FOCAS2.
Works on Windows (chattertools) and Linux (libfwlib32.so).

Knowledge base: docs/KNOWLEDGE_BASE.md
"""

import os
import sys
import time
from ctypes import (Structure, c_short, c_ushort, c_uint8,
                    byref, POINTER, sizeof)

# ── Platform: Windows uses chattertools, Linux uses raw .so ───────────────────
if sys.platform == "win32":
    import chattertools as ch
    _WINDOWS = True
else:
    import ctypes
    from ctypes import c_int32 as c_long   # Linux x64: c_long=8 bytes (wrong for FOCAS)
    _WINDOWS = False

from ctypes import c_long   # Windows: c_long = 4 bytes (correct)

# ── PMC type codes ────────────────────────────────────────────────────────────
PMC_TYPE = {"G":0,"F":1,"Y":2,"X":3,"A":4,"R":5,"T":6,"K":7,"C":8,"D":9}

# ── Structs ───────────────────────────────────────────────────────────────────
class IODBPMC(Structure):
    _pack_ = 1
    if not _WINDOWS: _layout_ = 'ms'
    _fields_ = [("type",c_short),("_r",c_short),
                ("datano_s",c_short),("datano_e",c_short),
                ("cdata",c_uint8*256)]

class ODBACT(Structure):
    _pack_ = 1
    if not _WINDOWS: _layout_ = 'ms'
    _fields_ = ([("dummy",c_short*2),("data",c_long)] if _WINDOWS
                else [("data",c_long)])

class ODBMACRO(Structure):
    _pack_ = 1
    if not _WINDOWS: _layout_ = 'ms'
    _fields_ = [("datano",c_long),("dummy",c_short),
                ("dec",c_short),("mcr_val",c_long),("type",c_short)]

class ODBPRO(Structure):        # Windows confirmed: dummy(long)+data(short)+mdata(short)
    _pack_ = 1
    _fields_ = [("dummy",c_long),("data",c_short),("mdata",c_short)]

class ODBALMMSG2(Structure):
    _pack_ = 1
    if not _WINDOWS: _layout_ = 'ms'
    _fields_ = [("alm_no",c_long),("type",c_short),("axis",c_short),
                ("msg_len",c_short),("msg",c_uint8*32)]


class FocasReader:
    """Reads all CNC data. Call connect() first, then read_all()."""

    def __init__(self, ip: str, port: int = 8193, timeout: int = 10):
        self.ip      = ip
        self.port    = port
        self.timeout = timeout
        self.focas   = None
        self.lib     = None
        self.handle  = None
        self._fn_pmc     = None
        self._fn_feed    = None
        self._fn_macro   = None
        self._fn_prog    = None
        self._fn_alarm   = None
        self._fn_pmc_wr  = None
        self._prev_ae    = False   # for F0.7 falling edge M30 count
        self._parts_m30  = 0

    # ── Connect ───────────────────────────────────────────────────────────────
    def connect(self):
        if _WINDOWS:
            self.focas  = ch.Focas(ip=self.ip, port=self.port, timeout=self.timeout)
            self.lib    = self.focas.fwlib
            self.handle = self.focas.handle
        else:
            lib_path = os.path.join(os.path.dirname(__file__), "lib", "libfwlib32.so")
            import ctypes
            self.lib    = ctypes.CDLL(lib_path, mode=0x00001)
            h = c_ushort(0)
            self.lib.cnc_allclibhndl3(
                self.ip.encode(), c_ushort(self.port),
                c_long(self.timeout), byref(h)
            )
            self.handle = h
        self._setup_fns()

    def disconnect(self):
        try:
            if self.lib and self.handle:
                self.lib.cnc_freelibhndl(self.handle)
        except Exception:
            pass

    # ── Setup function signatures ─────────────────────────────────────────────
    def _setup_fns(self):
        lib = self.lib

        # PMC read
        fn = lib.pmc_rdpmcrng
        fn.restype  = c_short
        fn.argtypes = [c_ushort,c_short,c_short,c_ushort,c_ushort,c_ushort,POINTER(IODBPMC)]
        self._fn_pmc = fn

        # PMC write (for approval signal)
        fn2 = lib.pmc_wrpmcrng
        fn2.restype  = c_short
        fn2.argtypes = [c_ushort,c_short,c_short,c_ushort,c_ushort,c_ushort,POINTER(IODBPMC)]
        self._fn_pmc_wr = fn2

        # Feed rate — try newer name first, fall back to older
        for _feed_name in (("cnc_rdactf", "cnc_actf") if _WINDOWS else ("cnc_actf",)):
            try:
                fn3 = getattr(lib, _feed_name)
                fn3.restype  = c_short
                fn3.argtypes = [c_ushort, POINTER(ODBACT)]
                self._fn_feed    = fn3
                self._feed_fname = _feed_name
                break
            except AttributeError:
                self._fn_feed    = None
                self._feed_fname = None

        # Macro read
        fn4 = lib.cnc_rdmacro
        fn4.restype  = c_short
        fn4.argtypes = [c_ushort, c_long, c_short, POINTER(ODBMACRO)]
        self._fn_macro = fn4

        # Macro write
        fn7 = lib.cnc_wrmacro
        fn7.restype  = c_short
        fn7.argtypes = [c_ushort, c_long, c_short, c_short, c_long]
        self._fn_macro_wr = fn7

        # Program number (Windows confirmed working)
        fn5 = lib.cnc_rdprgnum
        fn5.restype  = c_short
        fn5.argtypes = [c_ushort, POINTER(ODBPRO)]
        self._fn_prog = fn5

        # Alarm
        fn6 = lib.cnc_rdalmmsg2
        fn6.restype  = c_short
        fn6.argtypes = [c_ushort,c_short,POINTER(c_short),POINTER(ODBALMMSG2)]
        self._fn_alarm = fn6

    # ── Read single PMC bit ───────────────────────────────────────────────────
    def _read_bit(self, pmc_type: str, byte_no: int, bit_no: int) -> bool:
        buf = IODBPMC()
        ret = self._fn_pmc(
            self.handle,
            c_short(PMC_TYPE[pmc_type]), c_short(0),
            c_ushort(byte_no), c_ushort(byte_no),
            c_ushort(9), byref(buf)
        )
        return bool((buf.cdata[0] >> bit_no) & 1) if ret == 0 else False

    # ── Write PMC byte (approval signal) ─────────────────────────────────────
    def write_pmc_bit(self, pmc_type: str, byte_no: int, bit_no: int, value: int):
        """Write a single bit to PMC. value = 0 or 1."""
        # Read current byte first
        buf = IODBPMC()
        self._fn_pmc(
            self.handle,
            c_short(PMC_TYPE[pmc_type]), c_short(0),
            c_ushort(byte_no), c_ushort(byte_no),
            c_ushort(9), byref(buf)
        )
        current = buf.cdata[0]
        if value:
            new_val = current | (1 << bit_no)    # set bit
        else:
            new_val = current & ~(1 << bit_no)   # clear bit
        buf.cdata[0] = new_val
        self._fn_pmc_wr(
            self.handle,
            c_short(PMC_TYPE[pmc_type]), c_short(0),
            c_ushort(byte_no), c_ushort(byte_no),
            c_ushort(9), byref(buf)
        )

    # ── Read macro variable ───────────────────────────────────────────────────
    def _read_macro(self, var_no: int) -> float:
        if _WINDOWS:
            try:
                val = self.focas.cnc_rdmacro(var_no)
                if val is not None:
                    return float(val)
            except Exception:
                pass
        buf = ODBMACRO()
        ret = self._fn_macro(
            self.handle, c_long(var_no),
            c_short(sizeof(ODBMACRO)), byref(buf)
        )
        if ret == 0 and buf.type != 9:
            dec = max(int(buf.dec), 0)
            return buf.mcr_val / (10 ** dec)
        return 0.0

    # ── Read feed rate ────────────────────────────────────────────────────────
    def _read_feed(self) -> int:
        if _WINDOWS:
            # Try chattertools wrapper (cnc_rdactf then cnc_actf)
            for fname in ("cnc_rdactf", "cnc_actf"):
                try:
                    result = getattr(self.focas, fname)()
                    if result is not None:
                        return abs(int(result.data if hasattr(result, "data") else result))
                    break
                except Exception:
                    continue
        if self._fn_feed:
            buf = ODBACT()
            ret = self._fn_feed(self.handle, byref(buf))
            return abs(int(buf.data)) if ret == 0 else 0
        return 0

    # ── Read program number ───────────────────────────────────────────────────
    def _read_prog(self) -> int:
        buf = ODBPRO()
        ret = self._fn_prog(self.handle, byref(buf))
        if ret == 0:
            for n in (int(buf.data), int(buf.mdata)):
                if 1 <= n <= 99999999:
                    return n
        return 0

    # ── Read alarm ────────────────────────────────────────────────────────────
    # FANUC alarm type codes → human readable
    _ALM_TYPE = {
        0: "BG EDIT",  1: "SERVO",    2: "THERMAL",  4: "PC BOARD",
        5: "OT",       8: "OVERTRAVEL", 9: "PARITY",10: "PARITY",
        13:"ABS ENCDR",15:"EXTERNAL", 18:"PC2",      19:"HMCC",
        20:"EDIT ERR", 22:"OT2",      24:"OT3",      25:"SERVO2",
    }

    def _read_alarm(self):
        num = c_short(1)
        buf = ODBALMMSG2()
        ret = self._fn_alarm(self.handle, c_short(-1), byref(num), byref(buf))
        if ret == 0 and num.value > 0 and buf.alm_no != 0:
            msg = bytes(buf.msg[:buf.msg_len]).decode("ascii", "replace").strip()
            if not msg:
                # FOCAS returned no text — build description from type + axis
                tname = self._ALM_TYPE.get(int(buf.type), f"TYPE-{buf.type}")
                msg   = tname + (f" AXIS-{buf.axis}" if buf.axis > 0 else "")
            return f"ALM-{buf.alm_no}: {msg}"
        return None

    # ── Write macro variable ─────────────────────────────────────────────────
    def write_macro(self, var_no: int, value: float) -> bool:
        """
        Write a value to a FANUC CNC macro variable.
        Returns True on success.
        """
        # Determine decimal places needed
        if value == int(value):
            dec_places = 0
            int_val    = int(value)
        else:
            dec_places = 4
            int_val    = round(value * 10000)

        if _WINDOWS:
            try:
                self.focas.cnc_wrmacro(var_no, int(sizeof(ODBMACRO)), dec_places, int_val)
                return True
            except Exception:
                pass  # fall through to ctypes call

        ret = self._fn_macro_wr(
            self.handle,
            c_long(var_no),
            c_short(int(sizeof(ODBMACRO))),
            c_short(dec_places),
            c_long(int_val),
        )
        return ret == 0

    # ── Read ALL data — main method called by collector ───────────────────────
    def read_all(self, macro_vars: list, cfg: dict) -> dict:
        """
        macro_vars: list of int var numbers e.g. [3901, 3902]
        cfg: signal address config from .env
        Returns dict ready to POST to server.
        """
        ae   = self._read_bit("F", cfg["auto_exec_byte"],    cfg["auto_exec_bit"])
        cut  = self._read_bit("F", cfg["cutting_byte"],      cfg["cutting_bit"])
        scw  = self._read_bit("G", cfg["spindle_cw_byte"],   cfg["spindle_cw_bit"])
        scc  = self._read_bit("G", cfg["spindle_ccw_byte"],  cfg["spindle_ccw_bit"])

        # Feed hold: F0.4 — operator pressed Feed Hold button
        fh   = self._read_bit("F", cfg["feed_hold_byte"],    cfg["feed_hold_bit"])

        # Block stop: G46.1 — M00 / M01 / Single Block active
        bs   = self._read_bit("G", cfg["block_stop_byte"],   cfg["block_stop_bit"])

        # M30 count: F0.7 falling edge (auto_exec goes False → part complete)
        m30_fired = False
        if self._prev_ae and not ae:
            self._parts_m30 += 1
            m30_fired = True
        self._prev_ae = ae

        macros = {}
        for var in macro_vars:
            macros[str(var)] = self._read_macro(var)

        return {
            "auto_exec":  ae,
            "cutting":    cut,
            "spindle_on": scw or scc,
            "feed_hold":  fh,      # NEW: feed hold active
            "block_stop": bs,      # NEW: M00/M01/single block active
            "m30_fired":  m30_fired,  # NEW: True only on the tick M30 was detected
            "prog_num":   self._read_prog(),
            "feed_rate":  self._read_feed(),
            "alarm":      self._read_alarm(),
            "parts_m30":  self._parts_m30,
            "macros":     macros,
        }
