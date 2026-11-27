"""Attach 3D models to the project footprints (library and board).

usage: apply_models.py <project dir> fit.json"""
import json, math, sys
import pcbnew
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fit import body, rot  # noqa: E402  (fit.py runs its fitting on import; guarded below)

PRJ = sys.argv[1]
FIT = json.load(open(sys.argv[2]))
STOCK = __import__("os").environ.get("KICAD10_FOOTPRINT_DIR", "/usr/share/kicad/footprints")
D = "${KICAD10_3DMODEL_DIR}/"
io = pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP)

def model(path, dx=0.0, dy=0.0, dz=0.0, rz=0.0, rx=0.0):
    m = pcbnew.FP_3DMODEL()
    m.m_Filename = path
    m.m_Offset = pcbnew.VECTOR3D(dx, -dy, dz)      # model offsets: y up
    m.m_Rotation = pcbnew.VECTOR3D(rx, 0, -rz)   # KiCad: positive z turns clockwise seen from above
    m.m_Scale = pcbnew.VECTOR3D(1, 1, 1)
    return m

def placed(stock, target_x, target_y, deg, z):
    """Model of a stock footprint, rotated by deg, body centre at target (footprint coords)."""
    lib, name = stock.split(":")
    st = io.FootprintLoad(f"{STOCK}/{lib}.pretty", name)
    cx, cy, _, _ = body(st)
    rx, ry = rot((cx, cy), deg)
    return model(st.Models()[0].m_Filename, target_x - rx, target_y - ry, z, deg)

MODELS = {}
for name, f in FIT.items():
    MODELS[name] = [model(f["model"], f["dx"], f["dy"], 0, f["rot"])]
MODELS["IND-TH_L16.0-W8.5-P5.00-D0.7"] = [model("${KIPRJMOD}/flipdot.3dshapes/Toroid_Vertical_D16.0mm_T8.5mm_P5.00mm.wrl")]
top = 8.5 + 2.5 + 1.6
MODELS["ESP32_DevKit_30pin_2widths"] = [
    model("${KIPRJMOD}/flipdot.3dshapes/ESP32_DevKit_30pin_DOIT_V1.wrl"),
    model(D + "Connector_PinSocket_2.54mm.3dshapes/PinSocket_1x15_P2.54mm_Vertical.step", -17.78, -12.7, 0, 90),
    model(D + "Connector_PinSocket_2.54mm.3dshapes/PinSocket_1x15_P2.54mm_Vertical.step", -17.78, 12.7, 0, 90),
    placed("RF_Module:ESP32-WROOM-32", 25.75 - 12.75 + 1.0, 0, 270, top),     # antenna to the "WLAN" side
    placed("Connector_USB:USB_Micro-B_Molex_47346-0001", -25.75 + 2.6, 0, 90, top),
    placed("Package_TO_SOT_SMD:SOT-223", -13.5, 7.0, 0, top),
]

def set_models(fp):
    name = fp.GetFPID().GetLibItemName().wx_str()
    if name not in MODELS:
        return False
    ms = fp.Models()
    while len(ms):
        ms.pop()
    for m in MODELS[name]:
        ms.push_back(m)
    return True

# library
lib = f"{PRJ}/flipdot.pretty"
done = 0
for name in io.FootprintEnumerate(lib):
    fp = io.FootprintLoad(lib, name)
    if set_models(fp):
        io.FootprintSave(lib, fp); done += 1
print("library footprints with models:", done)
# board
pcb = f"{PRJ}/flipdot-driver.kicad_pcb"
b = pcbnew.LoadBoard(pcb)
n = sum(set_models(f) for f in b.GetFootprints())
missing = sorted({f.GetFPID().GetLibItemName().wx_str() for f in b.GetFootprints() if not len(f.Models())})
pcbnew.SaveBoard(pcb, b)
print("board footprints with models:", n, "without:", missing)
