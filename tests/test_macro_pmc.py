"""
test_macro_pmc.py
=================
1. Read macros #500-#510
2. Write macro #100 = 50, then verify
3. Check A0.0 (emergency signal)
4. Check F1.7, F1.1, G8.0 raw byte + bit
"""
import os, sys, ctypes
from ctypes import (Structure, c_short, c_ushort, c_uint8,
                    c_int32 as c_long, byref, POINTER, sizeof)

HERE = os.path.dirname(os.path.abspath(__file__))
lib  = ctypes.CDLL(os.path.join(HERE,"lib","libfwlib32.so"), mode=0x00001)
IP, PORT, TIMEOUT = "192.168.1.22", 8193, 10
PMC_TYPE = {"G":0,"F":1,"Y":2,"X":3,"A":4,"R":5,"T":6,"K":7,"C":8,"D":9}

class ODBMACRO(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("datano",c_long),("dummy",c_short),("dec",c_short),
              ("mcr_val",c_long),("type",c_short)]

class IODBMR(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("datano",c_long),("type",c_short),("data",c_long),("dec",c_short)]

class IODBPMC(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("type",c_short),("_r",c_short),("datano_s",c_short),
              ("datano_e",c_short),("cdata",c_uint8*256)]

def setup():
    lib.cnc_allclibhndl3.restype=c_short
    lib.cnc_allclibhndl3.argtypes=[ctypes.c_char_p,c_ushort,c_long,POINTER(c_ushort)]
    lib.cnc_freelibhndl.restype=c_short
    lib.cnc_freelibhndl.argtypes=[c_ushort]
    lib.cnc_rdmacro.restype=c_short
    lib.cnc_rdmacro.argtypes=[c_ushort,c_long,c_short,POINTER(ODBMACRO)]
    lib.cnc_wrmacro.restype=c_short
    lib.cnc_wrmacro.argtypes=[c_ushort,c_long,c_short,POINTER(IODBMR)]
    lib.pmc_rdpmcrng.restype=c_short
    lib.pmc_rdpmcrng.argtypes=[c_ushort,c_short,c_short,c_ushort,c_ushort,c_ushort,POINTER(IODBPMC)]

def read_macro(h, var_no):
    buf=ODBMACRO()
    ret=lib.cnc_rdmacro(h,c_long(var_no),c_short(sizeof(ODBMACRO)),byref(buf))
    if ret!=0: return None, f"ret={ret}"
    if buf.type==9: return None, "undefined"
    dec=max(int(buf.dec),0)
    return round(buf.mcr_val/(10**dec),dec) if dec else float(buf.mcr_val), "ok"

def write_macro_struct(h, var_no, value):
    """Method A: IODBMR struct (4 args)"""
    lib.cnc_wrmacro.restype  = c_short
    lib.cnc_wrmacro.argtypes = [c_ushort,c_long,c_short,POINTER(IODBMR)]
    dec = 4 if value != int(value) else 0
    raw = int(round(value * (10**dec)))
    buf=IODBMR(); buf.datano=var_no; buf.type=0; buf.data=raw; buf.dec=dec
    return lib.cnc_wrmacro(h,c_long(var_no),c_short(sizeof(IODBMR)),byref(buf))

def write_macro_5arg(h, var_no, value):
    """Method B: 5 direct args (handle, var_no, length, dec, value)"""
    lib.cnc_wrmacro.restype  = c_short
    lib.cnc_wrmacro.argtypes = [c_ushort,c_long,c_short,c_short,c_long]
    # sizeof(ODBMACRO) = 14 — same as read struct
    dec = 4 if value != int(value) else 0
    raw = int(round(value * (10**dec)))
    return lib.cnc_wrmacro(h,c_long(var_no),c_short(14),c_short(dec),c_long(raw))

def pmc_byte(h, atype, byte_no):
    buf=IODBPMC()
    ret=lib.pmc_rdpmcrng(h,c_short(PMC_TYPE[atype]),c_short(0),
                         c_ushort(byte_no),c_ushort(byte_no),c_ushort(9),byref(buf))
    return buf.cdata[0] if ret==0 else None

def section(t): print(f"\n{'━'*50}\n  {t}\n{'━'*50}")

def main():
    setup()
    h=c_ushort(0)
    ret=lib.cnc_allclibhndl3(IP.encode(),c_ushort(PORT),c_long(TIMEOUT),byref(h))
    if ret!=0: print(f"Connect fail {ret}"); sys.exit(1)
    print(f"✅ Connected  handle={h.value}")

    # ── 1. Read macros #500 – #510 ────────────────────────────────────────────
    section("1.  MACRO READ  #500 – #510")
    print(f"  {'Var':>6}  {'Value':>12}  Status")
    print(f"  {'───':>6}  {'─────':>12}  ──────")
    for v in range(500, 511):
        val, status = read_macro(h, v)
        if status == "ok":
            print(f"  #{v:>5}  {str(val):>12}  ✅")
        else:
            print(f"  #{v:>5}  {'─':>12}  {status}")

    # ── 2. Write macro #100 = 50, then verify ─────────────────────────────────
    section("2.  MACRO WRITE  #501 = 50")
    print(f"  NOTE: #100-#149 = LOCAL vars — FOCAS2 cannot write externally")
    print(f"        Common vars #500-#999 are writeable.\n")
    ERR = {1:"EW_FUNC",2:"EW_LENGTH",3:"EW_NUMBER",4:"EW_ATTRIB",
           5:"EW_DATA (alarm active / data error)",6:"EW_NOOPT",
           7:"EW_PROT (write protected)"}

    for method, fn in [("A – IODBMR struct", write_macro_struct),
                       ("B – 5-arg direct",  write_macro_5arg)]:
        before, _ = read_macro(h, 501)
        ret = fn(h, 501, 50)
        if ret == 0:
            after, _ = read_macro(h, 501)
            ok_str = "✅ CONFIRMED" if after == 50.0 else f"⚠️  got {after}"
            print(f"  Method {method}: ret=0  #501={after}  {ok_str}")
            break
        else:
            print(f"  Method {method}: ret={ret}  {ERR.get(ret,'')}")

    print(f"\n  ⚠️  If both fail with ret=5 while ALM-1007 is active →")
    print(f"     FANUC blocks macro writes during Emergency Stop.")
    print(f"     Clear the alarm first, then retry.")

    # ── 3. Check A0.0 — Emergency Stop Signal ────────────────────────────────
    section("3.  A0.0 — Emergency Signal (ALM-1007 source)")
    for byte_no in range(0, 4):
        bv = pmc_byte(h, "A", byte_no)
        if bv is not None:
            bits = ''.join(str((bv>>i)&1) for i in range(7,-1,-1))
            on_bits = [f"A{byte_no}.{i}" for i in range(8) if (bv>>i)&1]
            if bv:
                print(f"  A{byte_no} = 0x{bv:02X} = {bits}  ON: {on_bits}")
            else:
                print(f"  A{byte_no} = 0x00  (all off)")

    # ── 4. Check F1.7, F1.1, G8.0 raw bits ──────────────────────────────────
    section("4.  PMC SIGNAL VERIFY  (F1, G8 bytes)")
    checks = [
        ("F",1,"F1 full byte"),
        ("G",8,"G8 full byte"),
        ("G",43,"G43 full byte"),
    ]
    for atype,byte_no,label in checks:
        bv=pmc_byte(h,atype,byte_no)
        if bv is not None:
            bits=''.join(str((bv>>i)&1) for i in range(7,-1,-1))
            on_bits=[f"{atype}{byte_no}.{i}" for i in range(8) if (bv>>i)&1]
            print(f"  {label}: 0x{bv:02X} = {bits}  ON={on_bits}")

    lib.cnc_freelibhndl(h)
    print("\nDone.")

if __name__=="__main__":
    main()
