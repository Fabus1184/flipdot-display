"""ESP32 DevKit + test points: symbols, sub-sheet, and helpers for pin positions."""
import math
import sexpr as S
from sexpr import Num, Sym, find, find_all, head, prop

LEFT = ["5V", "GND", "IO13", "IO12", "IO14", "IO27", "IO26", "IO25", "IO33", "IO32", "IO35", "IO34", "IO39", "IO36", "EN"]
RIGHT = ["3V3", "GND", "IO15", "IO2", "IO4", "IO16", "IO17", "IO5", "IO18", "IO19", "IO21", "RX0/IO3", "TX0/IO1", "IO22", "IO23"]
# net per pin number, as on the PCB (the rest is unused)
NETS = {1: "+5V", 2: "GND", 3: "FP_EN", 4: "FP_DATA", 5: "FP_A0", 6: "FP_A1", 7: "FP_A2", 8: "FP_B0", 9: "FP_B1",
        16: "3.3V", 17: "GND", 18: "SR_ICLK", 19: "SR_OCLK", 20: "SR_DATA", 21: "SR_OE#"}
POWER = {"+5V", "3.3V", "GND"}
PIN_DY = 2.54
TOP_Y = 17.78


def pin_type(name):
    return {"5V": "power_in", "GND": "power_in", "3V3": "power_out"}.get(name, "bidirectional")


def esp_symbol(lib="flipdot", cached=False):
    name = f"{lib}:ESP32_DevKit_30" if cached else "ESP32_DevKit_30"
    pins = ""
    for i, n in enumerate(LEFT):
        pins += f'(pin {pin_type(n)} line (at -17.78 {TOP_Y - i * PIN_DY:.2f} 0) (length 5.08) (name "{n}" (effects (font (size 1.27 1.27)))) (number "{i + 1}" (effects (font (size 1.27 1.27)))))\n'
    for i, n in enumerate(RIGHT):
        pins += f'(pin {pin_type(n)} line (at 17.78 {TOP_Y - i * PIN_DY:.2f} 180) (length 5.08) (name "{n}" (effects (font (size 1.27 1.27)))) (number "{i + 16}" (effects (font (size 1.27 1.27)))))\n'
    txt = f'''(symbol "{name}" (pin_names (offset 1.016)) (exclude_from_sim no) (in_bom no) (on_board yes)
  (property "Reference" "U" (at 0 22.86 0) (effects (font (size 1.27 1.27))))
  (property "Value" "ESP32 DevKit (30 pin)" (at 0 -22.86 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "{lib}:ESP32_DevKit_30pin_2widths" (at 0 -25.4 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Description" "Socket for a 30-pin ESP32 DevKit (DOIT ESP32 DevKit V1 pinout); two row spacings for narrow and wide modules. Plugged in, not assembled." (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (symbol "ESP32_DevKit_30_0_1" (rectangle (start -12.7 20.32) (end 12.7 -20.32) (stroke (width 0.254) (type default)) (fill (type background))))
  (symbol "ESP32_DevKit_30_1_1" {pins})
  (embedded_fonts no))'''
    return S.loads(txt)


def testpoint_symbol(lib="flipdot", cached=False):
    c = S.loads(open("/usr/share/kicad/symbols/Connector.kicad_sym").read())
    tp = [s for s in c if head(s) == "symbol" and s[1] == "TestPoint"][0]
    tp = S.loads(S.dumps(tp))
    if cached:
        tp[1] = f"{lib}:TestPoint"
    prop(tp, "Footprint")[2] = f"{lib}:TestPoint_Pad_D0.6mm"
    return tp


def rot(px, py, angle):
    a = math.radians(angle)
    return px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)


def pin_pos(sch, sym, number):
    """Absolute position of pin `number` of placed symbol `sym`."""
    lib_id = find(sym, "lib_id")[1]
    libdef = [s for s in find(sch, "lib_symbols") if head(s) == "symbol" and s[1] == lib_id][0]
    at = find(sym, "at")
    x, y, a = float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0
    mirror = find(sym, "mirror")
    found = []

    def visit(n):
        for c in n:
            if isinstance(c, list):
                if head(c) == "pin" and find(c, "number") and find(c, "number")[1] == number:
                    found.append(c)
                visit(c)

    visit(libdef)
    p = find(found[0], "at")
    px, py = float(p[1]), float(p[2])
    if mirror and mirror[1] == "y":
        px = -px
    if mirror and mirror[1] == "x":
        py = -py
    rx, ry = rot(px, py, a)
    return round(x + rx, 4), round(y - ry, 4)
