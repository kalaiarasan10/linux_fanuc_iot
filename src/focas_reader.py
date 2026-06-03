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
from ctypes import (Structure, Union, c_short, c_ushort, c_uint8,
                    byref, POINTER, sizeof)

# ── Platform: Windows uses chattertools, Linux uses raw .so ───────────────────
if sys.platform == "win32":
    import chattertools as ch
    from ctypes import c_long           # Windows: c_long = 4 bytes (correct)
    _WINDOWS = True
else:
    import ctypes
    from ctypes import c_int32 as c_long   # Linux x64: c_long=8 bytes, FOCAS needs 4
    _WINDOWS = False

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

# ── Parameter read struct (cnc_rdparam) ──────────────────────────────────────
MAX_AXIS = 8

class _PARAMDATA(Union):
    _pack_ = 1
    _fields_ = [
        ("cdata",  c_uint8),
        ("idata",  c_short),
        ("ldata",  c_long),
        ("cdatas", c_uint8 * MAX_AXIS),
        ("idatas", c_short * MAX_AXIS),
        ("ldatas", c_long  * MAX_AXIS),
    ]

class IODBPARAM(Structure):
    _pack_ = 1
    _fields_ = [
        ("datano", c_short),
        ("type",   c_short),
        ("u",      _PARAMDATA),
    ]

class ODBALMMSG2(Structure):
    _pack_ = 1
    if not _WINDOWS: _layout_ = 'ms'
    _fields_ = [("alm_no",c_long),("type",c_short),("axis",c_short),
                ("msg_len",c_short),("msg",c_uint8*32)]

class ODBTOFS(Structure):           # tool offset
    _pack_ = 1
    if not _WINDOWS: _layout_ = 'ms'
    _fields_ = [("datano",c_short),("type",c_short),("data",c_long),("dec",c_short)]

class ODBPARAM_LONG(Structure):     # single long param (e.g. 6711, 6712)
    _pack_ = 1
    if not _WINDOWS: _layout_ = 'ms'
    _fields_ = [("datano",c_short),("type",c_short),("ldata",c_long)]

# External alarm text lookup (ALM-1000 to 1015 come from PMC EAX signals)
_EXT_ALM_TEXT = {
    1000:"External Alarm 0 — check PMC ladder (EAX0)",
    1001:"External Alarm 1 (EAX1)",
    1002:"External Alarm 2 (EAX2)",
    1003:"External Alarm 3 (EAX3)",
    1004:"External Alarm 4 (EAX4)",
    1005:"External Alarm 5 (EAX5)",
    1006:"External Alarm 6 (EAX6)",
    1007:"Emergency is activated",
    1008:"External Alarm 8 (EAX8)",
    1009:"External Alarm 9 (EAX9)",
}


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
            ret = self.lib.cnc_allclibhndl3(
                self.ip.encode(), c_ushort(self.port),
                c_long(self.timeout), byref(h)
            )
            if ret != 0:
                raise ConnectionError(f"FANUC CNC not reachable at {self.ip}:{self.port} (error code: {ret})")
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

        # Program number — different ABI on Windows vs Linux
        fn5 = lib.cnc_rdprgnum
        fn5.restype  = c_short
        fn5.argtypes = ([c_ushort, POINTER(ODBPRO)] if _WINDOWS
                        else [c_ushort, POINTER(c_short)])
        self._fn_prog = fn5

        # Alarm
        fn6 = lib.cnc_rdalmmsg2
        fn6.restype  = c_short
        fn6.argtypes = [c_ushort,c_short,POINTER(c_short),POINTER(ODBALMMSG2)]
        self._fn_alarm = fn6

        # Parameter read (generic — uses c_void_p for buffer)
        fn8 = lib.cnc_rdparam
        fn8.restype  = c_short
        if _WINDOWS:
            fn8.argtypes = [c_ushort, c_short, c_short, c_short, POINTER(IODBPARAM)]
        else:
            import ctypes as _ct
            fn8.argtypes = [c_ushort, c_short, c_short, c_short, _ct.c_void_p]
        self._fn_param = fn8

        # Tool offset
        fn9 = lib.cnc_rdtofs
        fn9.restype  = c_short
        fn9.argtypes = [c_ushort, c_short, c_short, c_short, POINTER(ODBTOFS)]
        self._fn_tofs = fn9

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
        if _WINDOWS:
            buf = ODBPRO()
            ret = self._fn_prog(self.handle, byref(buf))
            if ret == 0:
                for n in (int(buf.data), int(buf.mdata)):
                    if 1 <= n <= 99999999:
                        return n
        else:
            # Linux: cnc_rdprgnum writes ODBPRO layout (dummy[4]+data[2]+mdata[2])
            # but argtypes expect POINTER(c_short) — use 4-short buffer and read [2]
            buf = (c_short * 4)()
            import ctypes as _ct
            ret = self._fn_prog(self.handle, _ct.cast(buf, POINTER(c_short)))
            if ret == 0:
                # buf[2] = data (current prog), buf[3] = mdata (main prog)
                for n in (int(buf[2]), int(buf[3])):
                    if 1 <= n <= 99999999:
                        return n
        return 0

    # ── Read CNC built-in part counter (params 6712 / 6711) ──────────────────
    def _read_partcount_cnc(self) -> dict:
        """
        Reads FANUC built-in workpiece counter (incremented automatically at M30).
          6712 = parts done (actual count)
          6711 = parts required (target set by operator)
        Returns {"done": int, "required": int} or empty dict on failure.
        """
        result = {}
        for pno, key in [(6712, "done"), (6711, "required")]:
            try:
                buf = ODBPARAM_LONG()
                if _WINDOWS:
                    ret = self._fn_param(self.handle, c_short(pno), c_short(0),
                                         c_short(sizeof(ODBPARAM_LONG)), byref(buf))
                else:
                    import ctypes as _ct
                    ret = self._fn_param(self.handle, c_short(pno), c_short(0),
                                         c_short(sizeof(ODBPARAM_LONG)),
                                         _ct.cast(byref(buf), _ct.c_void_p))
                if ret == 0:
                    result[key] = int(buf.ldata)
            except Exception:
                pass
        return result

    # ── Read tool offsets (H geometry + H wear for tools 1-N) ────────────────
    def read_tool_offsets(self, max_tools: int = 16) -> list:
        """
        Returns list of dicts: [{"tool": 1, "h_geom_mm": 216.265, "h_wear_mm": 0.0}, ...]
        Only tools with non-zero offsets are returned.
        Unit: raw value is IS-B (0.001mm) → displayed in mm (divide by 1000).
        type=3 = H geometry  |  type=0 = H wear
        """
        offsets = []
        for tno in range(1, max_tools + 1):
            h_geom = h_wear = None
            for tp, key in [(3, "h_geom"), (0, "h_wear")]:
                buf = ODBTOFS()
                ret = self._fn_tofs(self.handle, c_short(tno), c_short(tp),
                                    c_short(sizeof(ODBTOFS)), byref(buf))
                if ret == 0:
                    dec = int(buf.dec)
                    raw = buf.data
                    # IS-B: dec=0 → raw in 0.001mm → divide by 1000
                    val = raw / (10 ** dec) if dec > 0 else raw / 1000.0
                    if key == "h_geom":
                        h_geom = round(val, 3)
                    else:
                        h_wear = round(val, 3)
            if h_geom is not None or h_wear is not None:
                if (h_geom or 0) != 0 or (h_wear or 0) != 0:
                    offsets.append({
                        "tool":      tno,
                        "h_geom_mm": h_geom or 0.0,
                        "h_wear_mm": h_wear or 0.0,
                    })
        return offsets

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
                # Use lookup for external alarms, fall back to type+axis
                msg = _EXT_ALM_TEXT.get(
                    int(buf.alm_no),
                    self._ALM_TYPE.get(int(buf.type), f"TYPE-{buf.type}")
                    + (f" AXIS-{buf.axis}" if buf.axis > 0 else "")
                )
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

    # ── Read machine parameter ────────────────────────────────────────────────
    def read_param(self, param_no: int, axis: int = 0):
        """
        Read a FANUC machine parameter.
        axis=0  → non-axis parameter (single value)
        axis=1  → X axis
        axis=2  → Y axis
        axis=3  → Z axis
        axis=-1 → all axes (returns dict {X, Y, Z})

        Returns int value, or dict if axis=-1, or None on error.

        Examples:
            reader.read_param(6757)          → 32  (non-axis param)
            reader.read_param(1815, axis=1)  → 48  (X axis)
            reader.read_param(1815, axis=-1) → {"X":48, "Y":48, "Z":48}
        """
        AXIS_NAMES = {1: "X", 2: "Y", 3: "Z", 4: "A", 5: "B"}

        if axis == -1:
            # Read all axes
            result = {}
            for ax in range(1, MAX_AXIS + 1):
                buf = IODBPARAM()
                ret = self._fn_param(
                    self.handle, c_short(param_no),
                    c_short(ax), c_short(sizeof(IODBPARAM)), byref(buf)
                )
                if ret == 0:
                    name = AXIS_NAMES.get(ax, f"A{ax}")
                    result[name] = int(buf.u.cdata)
                else:
                    break
            return result if result else None

        buf = IODBPARAM()
        ret = self._fn_param(
            self.handle, c_short(param_no),
            c_short(axis), c_short(sizeof(IODBPARAM)), byref(buf)
        )
        if ret == 0:
            return int(buf.u.cdata)
        return None

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

        # CNC built-in part counter (param 6712=done, 6711=required)
        cnc_parts = self._read_partcount_cnc()

        return {
            "auto_exec":      ae,
            "cutting":        cut,
            "spindle_on":     scw or scc,
            "feed_hold":      fh,
            "block_stop":     bs,
            "m30_fired":      m30_fired,
            "prog_num":       self._read_prog(),
            "feed_rate":      self._read_feed(),
            "alarm":          self._read_alarm(),
            "parts_m30":      self._parts_m30,
            "parts_done":     cnc_parts.get("done"),       # CNC counter (param 6712)
            "parts_required": cnc_parts.get("required"),   # CNC target  (param 6711)
            "macros":         macros,
        }
