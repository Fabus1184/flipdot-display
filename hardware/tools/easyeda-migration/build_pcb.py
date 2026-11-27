#!/usr/bin/env python3
"""Finish the imported PCB inside its KiCad project.

usage: build_pcb.py imported.kicad_pcb project_dir project_name netlist.net [--smd]

* footprint library ids -> "flipdot:", footprints exported to flipdot.pretty
* (SMD board) the EasyEDA free pads become the U5 DevKit socket and TP1..TP9
* every footprint linked to its schematic symbol (path, sheet, value, LCSC)
* title block, JLCPCB-friendly design rules, zones filled
"""

import os
import re
import sys

import pcbnew

sys.path.insert(0, os.path.dirname(__file__))
import sexpr as S  # noqa: E402

LIB = "flipdot"
MM = pcbnew.FromMM


def netlist_components(path):
    t = S.loads(open(path, encoding="utf-8").read())
    comps = {}
    for c in S.find(t, "components"):
        if S.head(c) != "comp":
            continue
        ref = S.find(c, "ref")[1]
        fields = {}
        fl = S.find(c, "fields")
        if fl:
            for f in S.find_all(fl, "field"):
                name = S.find(f, "name")[1]
                fields[name] = f[2] if len(f) > 2 and not isinstance(f[2], list) else ""
        sp = S.find(c, "sheetpath")
        comps[ref] = {
            "value": S.find(c, "value")[1] if S.find(c, "value") else "",
            "footprint": S.find(c, "footprint")[1] if S.find(c, "footprint") else "",
            "names": S.find(sp, "names")[1],
            "tstamps": S.find(sp, "tstamps")[1],
            "tstamp": S.find(c, "tstamps")[1] if S.find(c, "tstamps") else S.find(c, "tstamp")[1],
            "fields": fields,
            "exclude_from_bom": any(S.find(pr, "name") and S.find(pr, "name")[1] == "exclude_from_bom"
                                    for pr in S.find_all(c, "property")),
        }
    return comps


def free_pad_footprints(board):
    return [f for f in board.GetFootprints() if not f.GetReference()]


def make_devkit(board, pads, ref="U5"):
    """60 THT free pads (4 lines of 15, rows or columns) -> one footprint U5 with
    pins 1..15 on the 5 V side and 16..30 on the 3.3 V side."""
    lines = {}
    by_y = {}
    for f in pads:
        by_y.setdefault(round(pcbnew.ToMM(f.GetPosition().y), 1), []).append(f)
    vertical = len(by_y) != 4
    for f in pads:
        k = round(pcbnew.ToMM(f.GetPosition().x if vertical else f.GetPosition().y), 1)
        lines.setdefault(k, []).append(f)
    keys = sorted(lines)
    assert len(keys) == 4 and all(len(lines[k]) == 15 for k in keys), {k: len(v) for k, v in lines.items()}

    def along(f):
        return f.GetPosition().y if vertical else f.GetPosition().x

    def side(k):
        first = min(lines[k], key=along)
        return 1 if "5V" in first.Pads()[0].GetNetname() and "3" not in first.Pads()[0].GetNetname() else 16
    fp = pcbnew.FOOTPRINT(board)
    fp.SetFPID(pcbnew.LIB_ID(LIB, "ESP32_DevKit_30pin_2widths"))
    xs = [f.GetPosition().x for f in pads]
    yv = [f.GetPosition().y for f in pads]
    fp.SetPosition(pcbnew.VECTOR2I((min(xs) + max(xs)) // 2, (min(yv) + max(yv)) // 2))
    fp.SetReference(ref)
    fp.SetValue("ESP32 DevKit (30 pin)")
    fp.Reference().SetVisible(False)
    fp.Value().SetVisible(False)
    fp.Value().SetLayer(pcbnew.F_Fab)
    for k in keys:
        first = side(k)
        for n, f in enumerate(sorted(lines[k], key=along)):
            old = f.Pads()[0]
            pad = pcbnew.PAD(old)
            pad.SetParent(fp)
            pad.SetNumber(str(first + n))
            fp.Add(pad)
    # courtyard and fab outline around all pads
    bb = fp.GetBoundingBox(False)
    for layer, grow in ((pcbnew.F_CrtYd, MM(0.5)), (pcbnew.F_Fab, MM(0.2))):
        r = pcbnew.PCB_SHAPE(fp, pcbnew.SHAPE_T_RECT)
        r.SetLayer(layer)
        r.SetStart(pcbnew.VECTOR2I(bb.GetLeft() - grow, bb.GetTop() - grow))
        r.SetEnd(pcbnew.VECTOR2I(bb.GetRight() + grow, bb.GetBottom() + grow))
        r.SetWidth(MM(0.05) if layer == pcbnew.F_CrtYd else MM(0.1))
        fp.Add(r)
    board.Add(fp)
    for f in pads:
        board.Remove(f)
    return fp


def make_test_points(board, pads):
    order = sorted(pads, key=lambda f: (round(pcbnew.ToMM(f.GetPosition().y) / 10), pcbnew.ToMM(f.GetPosition().x)))
    for i, f in enumerate(order):
        f.SetReference(f"TP{i + 1}")
        f.SetValue("TestPoint")
        f.SetFPID(pcbnew.LIB_ID(LIB, "TestPoint_Pad_D0.6mm"))
        f.Reference().SetVisible(False)
        f.Pads()[0].SetNumber("1")
        f.SetBoardOnly(False)
        f.SetExcludedFromBOM(True)
        f.SetExcludedFromPosFiles(True)
    return order


def mounting_holes(board):
    """EasyEDA multi-layer fills (KiCad: cutouts) that ended up as small net-less
    zones -> board-only NPTH mounting-hole footprints MH1..MHn."""
    holes = []
    for z in list(board.Zones()):
        bb = z.GetBoundingBox()
        if z.GetNetname() or z.GetIsRuleArea() or max(bb.GetWidth(), bb.GetHeight()) > pcbnew.FromMM(6):
            continue
        holes.append((bb.GetCenter(), bb.GetWidth()))
        board.Remove(z)
    for i, (c, d) in enumerate(sorted(holes, key=lambda h: (h[0].y, h[0].x))):
        dmm = round(pcbnew.ToMM(d), 1)
        fp = pcbnew.FOOTPRINT(board)
        fp.SetFPID(pcbnew.LIB_ID(LIB, f"MountingHole_{dmm}mm"))
        fp.SetPosition(c)
        fp.SetReference(f"MH{i + 1}")
        fp.SetValue("MountingHole")
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        fp.SetBoardOnly(True)
        fp.SetExcludedFromBOM(True)
        fp.SetExcludedFromPosFiles(True)
        pad = pcbnew.PAD(fp)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(d, d))
        pad.SetDrillSize(pcbnew.VECTOR2I(d, d))
        pad.SetLayerSet(pcbnew.PAD.UnplatedHoleMask())
        pad.SetPosition(c)
        fp.Add(pad)
        cy = pcbnew.PCB_SHAPE(fp, pcbnew.SHAPE_T_CIRCLE)
        cy.SetLayer(pcbnew.F_CrtYd)
        cy.SetCenter(c)
        cy.SetEnd(pcbnew.VECTOR2I(c.x + d // 2 + pcbnew.FromMM(0.25), c.y))
        cy.SetWidth(pcbnew.FromMM(0.05))
        fp.Add(cy)
        board.Add(fp)
    return len(holes)


def export_library(board, pretty):
    io = pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP)
    os.makedirs(pretty, exist_ok=True)
    done = set()
    for f in board.GetFootprints():
        name = f.GetFPID().GetLibItemName().wx_str()
        if name in done:
            continue
        done.add(name)
        c = pcbnew.FOOTPRINT(f)
        if c.IsFlipped():
            c.Flip(c.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
        c.SetOrientationDegrees(0)
        c.SetPosition(pcbnew.VECTOR2I(0, 0))
        c.SetReference("REF**")
        c.SetPath(pcbnew.KIID_PATH())
        for p in c.Pads():
            p.SetNetCode(0)
        io.FootprintSave(pretty, c)
    return sorted(done)


def main():
    src, pdir, name, net = sys.argv[1:5]
    smd = "--smd" in sys.argv
    rev = "PCB8 (SMD)" if smd else "Rev 1.2 (THT)"
    title = "Flipdot driver board" if smd else "Flipdot driver board, Rev 1.2 (through-hole)"

    board = pcbnew.PCB_IO_MGR.Load(pcbnew.PCB_IO_MGR.EASYEDAPRO if src.endswith(".epro") else pcbnew.PCB_IO_MGR.KICAD_SEXP, src)

    for f in board.GetFootprints():
        lid = f.GetFPID()
        if lid.GetLibNickname().wx_str().endswith("-easyedapro"):
            f.SetFPID(pcbnew.LIB_ID(LIB, lid.GetLibItemName().wx_str()))

    free = free_pad_footprints(board)
    if free:
        tht = [f for f in free if f.Pads()[0].GetDrillSize().x > 0]
        smdp = [f for f in free if f.Pads()[0].GetDrillSize().x == 0]
        used = {f.GetReference() for f in board.GetFootprints()}
        make_devkit(board, tht, "U5" if "U5" not in used else "U6")
        if smdp:
            make_test_points(board, smdp)

    print("mounting holes:", mounting_holes(board))
    comps = netlist_components(net)
    linked, missing, fp_mismatch = 0, [], []
    for f in board.GetFootprints():
        ref = f.GetReference()
        c = comps.get(ref)
        if f.IsBoardOnly():
            continue
        if not c:
            missing.append(ref or f.GetFPID().GetLibItemName().wx_str())
            continue
        f.SetPath(pcbnew.KIID_PATH(c["tstamps"] + c["tstamp"]))
        f.SetSheetname(c["names"])
        f.SetSheetfile(name + ".kicad_sch" if c["names"] == "/" else "")
        if c["value"]:
            f.SetValue(c["value"])
        # same fields as the symbol (hidden, on the fab layer), as "Update PCB" would do
        for k, v in c["fields"].items():
            if k == "Footprint" or not v:
                continue
            f.SetField(k, v)
            fld = f.GetField(k)
            fld.SetVisible(False)
            fld.SetLayer(pcbnew.F_Fab)
        if c.get("exclude_from_bom"):
            f.SetExcludedFromBOM(True)
        want = c["footprint"].split(":")[-1]
        have = f.GetFPID().GetLibItemName().wx_str()
        if want and want != have:
            fp_mismatch.append((ref, want, have))
        linked += 1

    tb = board.GetTitleBlock()
    tb.SetTitle(title)
    tb.SetRevision(rev)
    tb.SetDate("2026-11-25")
    tb.SetCompany("flipdot-display")
    tb.SetComment(0, "Converted from the EasyEDA Pro project 'flipdot gedöns display'")

    out = os.path.join(pdir, name + ".kicad_pcb")
    pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP, out, board)
    print(f"linked {linked}, footprints without symbol: {missing}")
    if fp_mismatch:
        print("footprint field differs from PCB:", fp_mismatch[:10], len(fp_mismatch))

    names = export_library(board, os.path.join(pdir, LIB + ".pretty"))
    print("library footprints:", len(names))


if __name__ == "__main__":
    main()
