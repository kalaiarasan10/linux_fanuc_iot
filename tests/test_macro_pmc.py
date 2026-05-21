"""
test_macro_pmc.py
=================
1. Read macros #500-#510
2. Write macro #100 = 50, then verify
3. Check A0.0 (emergency signal)
4. Check F1.7, F1.1, G8.0 raw byte + bit
"""
import os, sys, ctypes, time
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
    """Method B: 5-arg direct (handle, var_no, length=14, dec, value)"""
    lib.cnc_wrmacro.restype  = c_short
    lib.cnc_wrmacro.argtypes = [c_ushort,c_long,c_short,c_short,c_long]
    dec = 4 if value != int(value) else 0
    raw = int(round(value * (10**dec)))
    return lib.cnc_wrmacro(h,c_long(var_no),c_short(14),c_short(dec),c_long(raw))

def write_macro_5arg_len10(h, var_no, value):
    """Method C: 5-arg direct (handle, var_no, length=10, dec, value)"""
    lib.cnc_wrmacro.restype  = c_short
    lib.cnc_wrmacro.argtypes = [c_ushort,c_long,c_short,c_short,c_long]
    dec = 4 if value != int(value) else 0
    raw = int(round(value * (10**dec)))
    return lib.cnc_wrmacro(h,c_long(var_no),c_short(10),c_short(dec),c_long(raw))

def write_macro_5arg_len12(h, var_no, value):
    """Method D: 5-arg direct (handle, var_no, length=12, dec, value)"""
    lib.cnc_wrmacro.restype  = c_short
    lib.cnc_wrmacro.argtypes = [c_ushort,c_long,c_short,c_short,c_long]
    dec = 4 if value != int(value) else 0
    raw = int(round(value * (10**dec)))
    return lib.cnc_wrmacro(h,c_long(var_no),c_short(12),c_short(dec),c_long(raw))

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

    # Check param 6001 for macro write protection (bit4 = NMC)
    class ODBPARAM(Structure):
        _pack_=1;_layout_='ms'
        _fields_=[("datano",c_short),("type",c_short),("ldata",c_long)]
    lib.cnc_rdparam.restype  = c_short
    lib.cnc_rdparam.argtypes = [c_ushort,c_short,c_short,c_short,ctypes.c_void_p]
    pb=ODBPARAM()
    rp=lib.cnc_rdparam(h,c_short(6001),c_short(0),c_short(8),
                       ctypes.cast(byref(pb),ctypes.c_void_p))
    if rp==0:
        nmc = (pb.ldata>>4)&1
        print(f"  Param 6001 = 0x{pb.ldata:04X}  bit4(NMC/macro-protect)={nmc}")
        if nmc:
            print(f"  ⚠️  NMC=1 → Macro write protection ON! Cannot write via FOCAS2.")
        else:
            print(f"  ✅ NMC=0 → Macro write protection OFF — writes should work.")
    else:
        print(f"  param 6001 read failed ret={rp}")

    confirmed = False
    methods = [
        ("A – IODBMR struct (len=12)", write_macro_struct),
        ("B – 5-arg len=14",           write_macro_5arg),
        ("C – 5-arg len=10",           write_macro_5arg_len10),
        ("D – 5-arg len=12",           write_macro_5arg_len12),
    ]
    for method, fn in methods:
        ret = fn(h, 501, 50)
        time.sleep(0.3)   # give CNC time to commit
        after, _ = read_macro(h, 501)
        if ret == 0:
            if after == 50.0:
                print(f"  Method {method}: ret=0  #501={after}  ✅ CONFIRMED")
                confirmed = True
                break
            else:
                print(f"  Method {method}: ret=0  #501={after}  ⚠️  not persisted")
        else:
            print(f"  Method {method}: ret={ret}  {ERR.get(ret,'unknown')}")

    if not confirmed:
        print(f"\n  ── Machine State ─────────────────────────────────────────")
        for atype,bno,lbl in [("F",0,"F0 (SA=b6,STL=b5,SPL=b4,OP=b7)"),
                               ("F",1,"F1 (MA=b7,ENB=b4,DEN=b3,RST=b1,AL=b0)"),
                               ("F",3,"F3 (MAUTO=b5,MMDI=b3,MJOG=b2,DNC=b4)"),
                               ("A",0,"A0 (EMG)")]:
            bv = pmc_byte(h, atype, bno)
            if bv is not None:
                bits = ''.join(str((bv>>i)&1) for i in range(7,-1,-1))
                print(f"  {lbl}: 0x{bv:02X} = {bits}")
        print(f"\n  ── Action required ───────────────────────────────────────")
        print(f"  1. Check param 6001 bit4 (NMC) on the CNC panel")
        print(f"  2. Press RESET on the machine panel, then retry")
        print(f"  3. Macro write only works when machine is NOT in alarm/reset")

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
