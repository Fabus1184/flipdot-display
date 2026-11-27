"""Fit KiCad stock footprints onto the project footprints -> 3D model transforms.

usage: fit.py <project dir> fit.json"""
import json, math, sys
import numpy as np
import pcbnew

STOCK = __import__("os").environ.get("KICAD10_FOOTPRINT_DIR", "/usr/share/kicad/footprints")
MAP = {
    "C0402": "Capacitor_SMD:C_0402_1005Metric",
    "C1206": "Capacitor_SMD:C_1206_3216Metric",
    "CAP-TH_BD8.0-P3.50-D0.6-FD": "Capacitor_THT:CP_Radial_D8.0mm_P3.50mm",
    "HDR-TH_2P-P2.54-V-M": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    "IDC-TH_50P-P2.54_BOOMELE2X25P": "Connector_IDC:IDC-Header_2x25_P2.54mm_Vertical",

    "LED0805-R-RD": "LED_SMD:LED_0805_2012Metric",
    "R0402": "Resistor_SMD:R_0402_1005Metric",
    "R0603": "Resistor_SMD:R_0603_1608Metric",
    "RES-ADJ-TH_3P-L10.0-W10.0-P2.50-L": "Potentiometer_THT:Potentiometer_Bourns_3296W_Vertical",
    "SMB_L4.6-W3.6-LS5.3-BI": "Diode_SMD:D_SMB",
    "SOD-123_L2.7-W1.6-LS3.7-RD": "Diode_SMD:D_SOD-123",
    "SOIC-16_L9.9-W3.9-P1.27-LS6.0-BL": "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
    "SOP-18_L11.4-W7.6-P1.27-LS10.6-BL": "Package_SO:SOIC-18W_7.5x11.6mm_P1.27mm",
    "SOT-23-3_L2.9-W1.3-P1.90-LS2.4-BR": "Package_TO_SOT_SMD:SOT-23",
    "SOT-23_L2.9-W1.3-P1.90-LS2.4-BR": "Package_TO_SOT_SMD:SOT-23",
    "TO-252-3_L6.5-W5.9-P4.60-LS10.0-BL": "Package_TO_SOT_SMD:TO-252-2",
    "TO-263-5_L10.2-W9.9-P1.70-LS14.4-TL": "Package_TO_SOT_SMD:TO-263-5_TabPin3",
    "USB-C-SMD_TYPE-C-6PIN-2MD-073": "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal",
}
BY_BODY = set()
THT_ONLY = {"USB-C-SMD_TYPE-C-6PIN-2MD-073"}     # align on the shell holes

io = pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP)

def pads(fp, tht_only=False):
    out = []
    for p in fp.Pads():
        if tht_only and p.GetDrillSize().x == 0:
            continue
        q = p.GetFPRelativePosition()
        out.append((p.GetNumber(), pcbnew.ToMM(q.x), pcbnew.ToMM(q.y)))
    return out

def body(fp):
    """Fab/courtyard bounding box centre and size (mm, footprint coords)."""
    xs, ys = [], []
    for g in fp.GraphicalItems():
        if g.GetLayer() in (pcbnew.F_Fab, pcbnew.F_CrtYd, pcbnew.F_SilkS):
            bb = g.GetBoundingBox()
            xs += [pcbnew.ToMM(bb.GetLeft()) - pcbnew.ToMM(fp.GetPosition().x),
                   pcbnew.ToMM(bb.GetRight()) - pcbnew.ToMM(fp.GetPosition().x)]
            ys += [pcbnew.ToMM(bb.GetTop()) - pcbnew.ToMM(fp.GetPosition().y),
                   pcbnew.ToMM(bb.GetBottom()) - pcbnew.ToMM(fp.GetPosition().y)]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(xs) - min(xs), max(ys) - min(ys)

def rot(p, deg):  # visual CCW rotation in y-down footprint coordinates
    a = math.radians(deg); x, y = p
    return (x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a))

def main(PRJ, out):
    res = {}
    for name, stock in MAP.items():
        ours = io.FootprintLoad(f"{PRJ}/flipdot.pretty", name)
        lib, fpn = stock.split(":")
        st = io.FootprintLoad(f"{STOCK}/{lib}.pretty", fpn)
        model = st.Models()[0].m_Filename if len(st.Models()) else None
        best = None
        for deg in (0, 90, 180, 270):
            if name in BY_BODY:
                ox, oy, ow, oh = body(ours); sx, sy, sw, sh = body(st)
                rw, rh = (sw, sh) if deg in (0, 180) else (sh, sw)
                cx, cy = rot((sx, sy), deg)
                err = abs(rw - ow) + abs(rh - oh)
                cand = (err, deg, ox - cx, oy - cy, "body")
            else:
                P = pads(ours, name in THT_ONLY); Q = pads(st, name in THT_ONLY)
                qn = {}
                for n, x, y in Q:
                    qn.setdefault(n, []).append(rot((x, y), deg))
                common = [(x, y, qn[n][0]) for n, x, y in P if n in qn and len(qn[n]) == 1]
                if len(common) >= 2 and len(common) >= 0.6 * len(P):
                    d = np.mean([(x - q[0], y - q[1]) for x, y, q in common], axis=0)
                    err = float(np.sqrt(np.mean([(x - q[0] - d[0]) ** 2 + (y - q[1] - d[1]) ** 2 for x, y, q in common])))
                    cand = (err, deg, d[0], d[1], "numbers")
                else:
                    pq = np.array([rot((x, y), deg) for _, x, y in Q]); pp = np.array([(x, y) for _, x, y in P])
                    d = pp.mean(0) - pq.mean(0)
                    dist = np.sqrt(((pp[:, None, :] - (pq[None] + d)) ** 2).sum(-1)).min(1)
                    err = float(dist.mean())
                    cand = (err, deg, d[0], d[1], "geometry")
            if best is None or cand[0] < best[0] - 1e-6:
                best = cand
        err, deg, dx, dy, how = best
        res[name] = {"model": model, "rot": deg, "dx": round(dx, 4), "dy": round(dy, 4), "err": round(err, 3), "how": how}
        print(f"{name:40s} rot {deg:3d}  off {dx:7.3f} {dy:7.3f}  err {err:.3f} ({how})")
    json.dump(res, open(out, "w"), indent=1)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
