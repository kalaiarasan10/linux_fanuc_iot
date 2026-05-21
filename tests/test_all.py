"""
test_all.py  —  Full CNC data read test (Linux FOCAS2)
=======================================================
  1. Program Number
  2. Part Count  (CNC built-in counter via params 6711/6712)
  3. CNC Parameters
  4. Axis Positions (X/Y/Z)
  5. PMC Signals  (with correct FANUC signal names)
  6. Feed Rate & Spindle Speed
  7. Tool Offsets
  8. Active Alarms

Run:  python3 test_all.py
"""

import os, sys, ctypes
from ctypes import (Structure, c_short, c_ushort, c_uint8, c_char,
                    c_int32 as c_long, byref, POINTER, sizeof)

HERE     = os.path.dirname(os.path.abspath(__file__))
LIB_PATH = os.path.join(HERE, "lib", "libfwlib32.so")
IP       = "192.168.1.22"
PORT     = 8193
TIMEOUT  = 10

lib = ctypes.CDLL(LIB_PATH, mode=0x00001)
PMC_TYPE = {"G":0,"F":1,"Y":2,"X":3,"A":4,"R":5,"T":6,"K":7,"C":8,"D":9}

# ═══════════════════════════════════════════════════════════════════════════════
#  Structs
# ═══════════════════════════════════════════════════════════════════════════════
class IODBPMC(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("type",c_short),("_r",c_short),("datano_s",c_short),
              ("datano_e",c_short),("cdata",c_uint8*256)]

class ODBACT(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("data",c_long)]

class ODBMACRO(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("datano",c_long),("dummy",c_short),("dec",c_short),
              ("mcr_val",c_long),("type",c_short)]

class ODBALMMSG2(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("alm_no",c_long),("type",c_short),("axis",c_short),
              ("msg_len",c_short),("msg",c_char*32)]

class POSELM(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("data",c_long),("dec",c_short),("unit",c_short),
              ("name",c_uint8),("suff",c_uint8*3)]

class ODBPOS(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("abs",POSELM),("mach",POSELM),("rel",POSELM),("dist",POSELM)]

class ODBPARAM(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("datano",c_short),("type",c_short),("ldata",c_long)]

class REALPRM(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("prm_val",c_long),("dec_val",c_short),("_pad",c_short)]

class ODBPARAM_REAL(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("datano",c_short),("type",c_short),("rdata",REALPRM)]

class ODBTOFS(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("datano",c_short),("type",c_short),("data",c_long),("dec",c_short)]

class SPEEDELM(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("data",c_long),("dec",c_short),("unit",c_short),
              ("name",c_uint8),("suff",c_uint8*3)]

class ODBSPEED(Structure):
    _pack_=1;_layout_='ms'
    _fields_=[("actf",SPEEDELM),("acts",SPEEDELM)]

# ═══════════════════════════════════════════════════════════════════════════════
#  Setup
# ═══════════════════════════════════════════════════════════════════════════════
def setup():
    lib.cnc_allclibhndl3.restype  = c_short
    lib.cnc_allclibhndl3.argtypes = [ctypes.c_char_p,c_ushort,c_long,POINTER(c_ushort)]
    lib.cnc_freelibhndl.restype   = c_short
    lib.cnc_freelibhndl.argtypes  = [c_ushort]
    lib.cnc_rdprgnum.restype      = c_short
    lib.cnc_rdprgnum.argtypes     = [c_ushort,POINTER(c_short)]
    lib.cnc_rdmacro.restype       = c_short
    lib.cnc_rdmacro.argtypes      = [c_ushort,c_long,c_short,POINTER(ODBMACRO)]
    lib.pmc_rdpmcrng.restype      = c_short
    lib.pmc_rdpmcrng.argtypes     = [c_ushort,c_short,c_short,c_ushort,c_ushort,c_ushort,POINTER(IODBPMC)]
    lib.cnc_actf.restype          = c_short
    lib.cnc_actf.argtypes         = [c_ushort,POINTER(ODBACT)]
    lib.cnc_rdspeed.restype       = c_short
    lib.cnc_rdspeed.argtypes      = [c_ushort,c_short,POINTER(ODBSPEED)]
    lib.cnc_rdalmmsg2.restype     = c_short
    lib.cnc_rdalmmsg2.argtypes    = [c_ushort,c_short,POINTER(c_short),POINTER(ODBALMMSG2)]
    lib.cnc_rdposition.restype    = c_short
    lib.cnc_rdposition.argtypes   = [c_ushort,c_short,POINTER(c_short),POINTER(ODBPOS)]
    lib.cnc_rdparam.restype       = c_short
    lib.cnc_rdparam.argtypes      = [c_ushort,c_short,c_short,c_short,ctypes.c_void_p]
    lib.cnc_rdtofs.restype        = c_short
    lib.cnc_rdtofs.argtypes       = [c_ushort,c_short,c_short,c_short,POINTER(ODBTOFS)]

# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════
def section(t): print(f"\n{'━'*56}\n  {t}\n{'━'*56}")
def ok(lbl,val): print(f"  ✅ {lbl:<32} {val}")
def fail(lbl,err): print(f"  ❌ {lbl:<32} ERROR: {err}")

def pmc_byte(h, atype, byte_no):
    buf = IODBPMC()
    ret = lib.pmc_rdpmcrng(h,c_short(PMC_TYPE[atype]),c_short(0),
                           c_ushort(byte_no),c_ushort(byte_no),c_ushort(9),byref(buf))
    return buf.cdata[0] if ret==0 else None

def rdparam(h, pno, axis=0, length=8):
    if length<=8:
        buf=ODBPARAM()
        ret=lib.cnc_rdparam(h,c_short(pno),c_short(axis),c_short(length),
                            ctypes.cast(byref(buf),ctypes.c_void_p))
        return ret, buf.ldata if ret==0 else None
    buf=ODBPARAM_REAL()
    ret=lib.cnc_rdparam(h,c_short(pno),c_short(axis),c_short(length),
                        ctypes.cast(byref(buf),ctypes.c_void_p))
    if ret==0:
        dec=max(int(buf.rdata.dec_val),0)
        return 0, round(buf.rdata.prm_val/(10**dec),dec) if dec else buf.rdata.prm_val
    return ret, None

# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    print(f"\n{'═'*56}")
    print(f"  FANUC CNC — Full Data Read (Linux)   {IP}:{PORT}")
    print(f"{'═'*56}")

    setup()
    h = c_ushort(0)
    ret = lib.cnc_allclibhndl3(IP.encode(),c_ushort(PORT),c_long(TIMEOUT),byref(h))
    if ret!=0: print(f"\n❌ Connection FAILED (ret={ret})"); sys.exit(1)
    print(f"\n✅ Connected  (handle={h.value})")

    # ── 1. Program Number ─────────────────────────────────────────────────────
    section("1.  PROGRAM NUMBER")
    try:
        # Linux layout: dummy[4] + current[2] + main[2]  → use 4-short buffer
        buf = (c_short*4)()
        ret = lib.cnc_rdprgnum(h, ctypes.cast(buf, POINTER(c_short)))
        if ret==0:
            ok("Current Program", f"O{buf[2]}")
            ok("Main Program",    f"O{buf[3]}")
        else:
            fail("cnc_rdprgnum", f"ret={ret}")
    except Exception as e:
        fail("cnc_rdprgnum", e)

    # ── 2. Part Count ─────────────────────────────────────────────────────────
    section("2.  PART COUNT  (CNC built-in counter)")
    # Param 6712 = workpieces machined (done)
    # Param 6711 = workpieces required (target)
    ret_d, done = rdparam(h, 6712)
    ret_r, req  = rdparam(h, 6711)
    if ret_d==0:
        ok("Parts Done    (param 6712)", int(done) if done==int(done) else done)
    else:
        fail("Parts Done (6712)", f"ret={ret_d}")
    if ret_r==0:
        ok("Parts Required (param 6711)", int(req) if req==int(req) else req)
    else:
        fail("Parts Required (6711)", f"ret={ret_r}")
    # Also show macro #3901/#3902 (set by NC program)
    for var,lbl in [(3901,"#3901 (macro-done)"),(3902,"#3902 (macro-req)")]:
        buf=ODBMACRO()
        r=lib.cnc_rdmacro(h,c_long(var),c_short(sizeof(ODBMACRO)),byref(buf))
        if r==0 and buf.type!=9:
            dec=max(int(buf.dec),0)
            val=round(buf.mcr_val/(10**dec),dec)
            ok(lbl, int(val) if val==int(val) else val)
        else:
            ok(lbl, f"0  (not set by NC program)")

    # ── 3. CNC Parameters ────────────────────────────────────────────────────
    section("3.  CNC PARAMETERS")
    params = [
        (1020,1,8, "Axis-1 Name  (param 1020)"),
        (1020,2,8, "Axis-2 Name  (param 1020)"),
        (1020,3,8, "Axis-3 Name  (param 1020)"),
        (1420,1,12,"Rapid Rate X (param 1420) mm/min"),
        (1420,2,12,"Rapid Rate Y (param 1420) mm/min"),
        (1420,3,12,"Rapid Rate Z (param 1420) mm/min"),
        (1023,1,8, "Servo Axis-1 (param 1023)"),
        (1023,2,8, "Servo Axis-2 (param 1023)"),
    ]
    for pno,ax,ln,lbl in params:
        ret,val=rdparam(h,pno,ax,ln)
        if ret==0:
            # Axis name: 88=X 89=Y 90=Z (ASCII)
            display = chr(int(val)) if pno==1020 and 65<=int(val)<=90 else val
            ok(lbl, display)
        else:
            fail(lbl,f"ret={ret}")

    # ── 4. Axis Positions ────────────────────────────────────────────────────
    section("4.  MACHINE AXIS POSITIONS")
    AXIS_MAP = {0x58:"X",0x59:"Y",0x5A:"Z",0x41:"A",0x42:"B",0x43:"C",
                1:"X",2:"Y",3:"Z",4:"A",5:"B",6:"C"}
    AXIS_LBL = ["X","Y","Z","A","B","C"]
    try:
        num = c_short(8)
        buf = (ODBPOS*8)()
        ret = lib.cnc_rdposition(h,c_short(-1),byref(num),byref(buf[0]))
        if ret==0:
            for i in range(num.value):
                pos  = buf[i]
                name = AXIS_MAP.get(int(pos.abs.suff[1]),
                       AXIS_MAP.get(int(pos.abs.name),
                       AXIS_LBL[i] if i<len(AXIS_LBL) else f"ax{i+1}"))
                dec  = max(int(pos.abs.dec),0)
                mach = round(pos.mach.data/(10**dec),dec)
                abso = round(pos.abs.data /(10**dec),dec)
                rel  = round(pos.rel.data /(10**dec),dec)
                print(f"  ✅ {name}  machine={mach:>10.3f} mm   abs={abso:>10.3f}   rel={rel:>10.3f}")
        else:
            fail("cnc_rdposition",f"ret={ret}")
    except Exception as e:
        fail("cnc_rdposition",e)

    # ── 5. PMC Signals ───────────────────────────────────────────────────────
    section("5.  PMC SIGNALS")
    print(f"  {'Address':<9} {'Signal (FANUC name)':<28} {'State':<8} {'Byte (b7..b0)'}")
    print(f"  {'───────':<9} {'───────────────────':<28} {'─────':<8} {'─────────────'}")

    signals = [
        # addr           FANUC / machine signal name  (verified via PMC Symbol Viewer)
        ("F", 0,7,  "OP    – Automatic Operation"),   # F0.7=OP
        ("F", 0,6,  "SA    – Servo Ready Compl"),     # F0.6=SA  ← was wrongly STL
        ("F", 0,5,  "STL   – Cycle Start Lamp"),      # F0.5=STL ← was wrongly SPL
        ("F", 0,4,  "SPL   – Feed Hold Lamp"),        # F0.4=SPL ← was wrongly AL
        ("F", 1,0,  "AL    – Alarm Signal"),          # F1.0=AL  ← was wrongly MF
        ("F", 1,1,  "RST   – Resetting Signal"),      # F1.1=RST ✅
        ("F", 1,3,  "DEN   – Distribution End"),      # F1.3=DEN
        ("F", 1,4,  "ENB   – Spindle Enable"),        # F1.4=ENB
        ("F", 1,7,  "MA    – Servo Ready (CNC Sig)"), # F1.7=MA  ← was wrongly EMG
        ("F", 2,6,  "GIS   – G01 Cutting Feed"),      # F2.6=GIS ✅
        ("F", 7,0,  "MF    – M-func Strobe"),         # F7.0=MF  ← was wrongly F1.0
        ("F",62,7,  "PRTSF – Part Count Reached"),    # F62.7=PRTSF (from PMC viewer)
        ("F",64,0,  "TLCH  – Tool Change Signal"),    # F64.0=TLCH
        ("A", 0,7,  "ESP   – Emergency Stop (A0.7)"), # A0.7=ESP (source of ALM-1007)
        ("G",70,5,  "SFR   – Spindle Forward"),       # G70.5=SFR ✅
        ("G",70,6,  "SRV   – Spindle Reverse"),       # G70.6=SRV ✅
        ("G", 8,0,  "IT    – Interlock"),             # G8.0=IT  ✅
        ("G",46,1,  "SBK   – Single Block"),          # G46.1=SBK
        ("G",43,0,  "SPSTP – Spindle Stop"),          # G43.0=SPSTP
    ]

    prev_byte = None
    for atype,byte_no,bit_no,name in signals:
        bv = pmc_byte(h, atype, byte_no)
        if bv is None: continue
        bit  = (bv>>bit_no)&1
        state= "ON  🟢" if bit else "off ⚫"
        addr = f"{atype}{byte_no}.{bit_no}"
        bstr = f"0b{bv:08b}" if (atype,byte_no)!=prev_byte else "   (same) "
        prev_byte = (atype,byte_no)
        print(f"  {addr:<9} {name:<28} {state:<8} {bstr}")

    # ── 6. Feed Rate & Spindle ────────────────────────────────────────────────
    section("6.  FEED RATE & SPINDLE SPEED")
    try:
        buf=ODBACT()
        ret=lib.cnc_actf(h,byref(buf))
        ok("Feed Rate    cnc_actf (mm/min)", buf.data if ret==0 else f"err {ret}")
    except Exception as e:
        fail("Feed Rate",e)
    try:
        buf=ODBSPEED()
        ret=lib.cnc_rdspeed(h,c_short(0),byref(buf))
        if ret==0:
            df=max(int(buf.actf.dec),0); ds=max(int(buf.acts.dec),0)
            ok("Feed Rate    cnc_rdspeed",round(buf.actf.data/(10**df),df) if df else buf.actf.data)
            ok("Spindle RPM  cnc_rdspeed",round(buf.acts.data/(10**ds),ds) if ds else buf.acts.data)
        else:
            fail("cnc_rdspeed",f"ret={ret}")
    except Exception as e:
        fail("cnc_rdspeed",e)

    # ── 7. Tool Offsets ───────────────────────────────────────────────────────
    section("7.  TOOL OFFSETS  (unit: 0.001mm → displayed in mm)")
    # type 3 = H Geometry (tool length), type 0 = H Wear
    # dec=0 means raw value in IS-B units (0.001mm) → divide by 1000
    print(f"  {'Tool':<6} {'H-Geometry (mm)':>16}  {'H-Wear (mm)':>14}  {'D-Geometry':>12}  {'D-Wear':>10}")
    print(f"  {'────':<6} {'───────────────':>16}  {'──────────':>14}  {'──────────':>12}  {'──────':>10}")
    for tno in range(1, 17):
        row = {}
        any_nonzero = False
        for tp in range(0,5):
            buf=ODBTOFS()
            ret=lib.cnc_rdtofs(h,c_short(tno),c_short(tp),c_short(sizeof(ODBTOFS)),byref(buf))
            if ret==0:
                dec=int(buf.dec)
                raw=buf.data
                # IS-B: dec=0 means value is in 0.001mm units → /1000
                val=raw/(10**dec) if dec>0 else raw/1000.0
                row[tp]=round(val,3)
                if raw!=0: any_nonzero=True
        if any_nonzero:
            hg = f"{row.get(3,0.0):>10.3f}" if row.get(3,0.0)!=0 else f"{'0.000':>10}"
            hw = f"{row.get(0,0.0):>10.3f}" if row.get(0,0.0)!=0 else f"{'0.000':>10}"
            dg = f"{row.get(1,0.0):>10.3f}" if row.get(1,0.0)!=0 else f"{'─':>10}"
            dw = f"{row.get(2,0.0):>10.3f}" if row.get(2,0.0)!=0 else f"{'─':>10}"
            print(f"  T{tno:02d}   H-Geom={hg} mm   H-Wear={hw} mm")

    # ── 8. Alarms ─────────────────────────────────────────────────────────────
    section("8.  ACTIVE ALARMS")

    # Known external alarm descriptions
    EXT_ALM = {
        1000:"External Alarm 0  (EAX0 — check PMC ladder)",
        1001:"External Alarm 1  (EAX1)",
        1002:"External Alarm 2  (EAX2)",
        1003:"External Alarm 3  (EAX3)",
        1004:"External Alarm 4  (EAX4)",
        1005:"External Alarm 5  (EAX5)",
        1006:"External Alarm 6  (EAX6)",
        1007:"Emergency is activated",
        1008:"External Alarm 8  (EAX8)",
        1009:"External Alarm 9  (EAX9)",
    }
    ALM_TYPE={0:"BG EDIT",1:"SERVO",2:"THERMAL",4:"PC BOARD",5:"OT",
              8:"OVERTRAVEL",9:"PARITY",13:"ABS ENCDR",15:"EXTERNAL",
              18:"PC2",20:"EDIT ERR",25:"SERVO2"}

    class ALMBUF10(Structure):
        _pack_=1;_layout_='ms'
        _fields_=[("alm",ODBALMMSG2*10)]

    num=c_short(10)
    abuf=ALMBUF10()
    ret=lib.cnc_rdalmmsg2(h,c_short(-1),byref(num),byref(abuf.alm[0]))
    if ret==0 and num.value>0:
        found=False
        for i in range(num.value):
            a=abuf.alm[i]
            if a.alm_no==0: continue
            found=True
            msg=a.msg[:a.msg_len].decode("ascii","replace").strip()
            tname=ALM_TYPE.get(int(a.type),f"type-{a.type}")
            ax=f"  axis={a.axis}" if a.axis>0 else ""
            # Use lookup table if message is empty
            if not msg:
                msg=EXT_ALM.get(int(a.alm_no), f"Alarm #{a.alm_no} — check CNC alarm page")
            ax_str = f"  AXIS-{a.axis}" if a.axis > 0 else ""
            print(f"  🔴 ALM-{a.alm_no}  [{tname}]{ax_str}")
            print(f"     Description : {msg}")
        if not found:
            ok("Alarms", "No active alarms ✅")
    else:
        ok("Alarms", f"No active alarms ✅  (ret={ret})")

    print(f"\n{'═'*56}\n  Done\n{'═'*56}\n")
    lib.cnc_freelibhndl(h)

if __name__=="__main__":
    main()
