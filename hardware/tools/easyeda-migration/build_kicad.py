#!/usr/bin/env python3
"""Turn the schematics/PCBs imported by KiCad into clean KiCad projects.

* SMD driver board: EasyEDA's batch-reused `hss`/`lss` blocks become real
  hierarchical sheets (16 each) with the per-channel references EasyEDA
  stored in its INSTANCE documents.
* One symbol library and one footprint library per project.
* PCB footprints linked to their schematic symbols (by reference).
* EasyEDA drawing frames/logos removed, proper paper sizes and title blocks.

Usage: build_kicad.py <epro2> <imported dir> <pcb dir> <out dir> [--rev12 | --panel]
"""

import copy
import json
import os
import re
import shutil
import sys
import uuid as uuidlib
import zipfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
import sexpr as S  # noqa: E402
from sexpr import Num, Sym, find, find_all, head, prop  # noqa: E402
from epro2_to_epro import read_epru, meta, live  # noqa: E402

DATE = "2026-11-25"
UNIT = 0.254  # mm per EasyEDA schematic unit (10 mil)


def new_uuid():
    return str(uuidlib.uuid4())


def num(v):
    return Num(S.fmt(v))


def load(path):
    return S.loads(open(path, encoding="utf-8").read())


def save(path, tree):
    with open(path, "w", encoding="utf-8") as f:
        f.write(S.dumps(tree) + "\n")


# --------------------------------------------------------------------------
# generic schematic helpers


def rename_libs(tree, lib):
    """`<anything>-easyedapro:Name` -> `<lib>:Name` everywhere."""

    def fix(s):
        return re.sub(r"^[^:]*-easyedapro:", lib + ":", s)

    def visit(node):
        for i, c in enumerate(node):
            if isinstance(c, list):
                visit(c)
            elif isinstance(c, str) and not isinstance(c, (Sym, Num)) and "-easyedapro:" in c:
                node[i] = fix(c)

    visit(tree)


def drop_drawing_frames(sch):
    """EasyEDA's A4 frame symbol (with its logo) has no place in KiCad."""
    keep = []
    for c in sch:
        if head(c) == "symbol" and find(c, "lib_id") and "Drawing-Symbol" in find(c, "lib_id")[1]:
            continue
        keep.append(c)
    sch[:] = keep
    libs = find(sch, "lib_symbols")
    if libs:
        libs[:] = [c for c in libs if not (head(c) == "symbol" and "Drawing-Symbol" in c[1])]


def items_bbox(sch):
    xs, ys = [], []

    def visit(node):
        for c in node:
            if isinstance(c, list):
                if head(c) in ("lib_symbols", "title_block", "paper", "property"):
                    continue
                if head(c) in ("at", "xy", "start", "end", "center", "mid") and len(c) >= 3:
                    try:
                        xs.append(float(c[1]))
                        ys.append(float(c[2]))
                    except ValueError:
                        pass
                visit(c)

    for c in sch:
        if isinstance(c, list) and head(c) not in ("lib_symbols", "title_block", "paper", "sheet_instances"):
            visit([c])
    return min(xs), min(ys), max(xs), max(ys)


def translate(sch, dx, dy):
    """Moves everything on the sheet (not the library cache) by dx, dy (mm),
    snapped to the 1.27 mm grid so wires stay on grid."""

    def visit(node, inside_symbol_lib=False):
        for c in node:
            if not isinstance(c, list):
                continue
            h = head(c)
            if h in ("lib_symbols", "title_block", "paper", "sheet_instances"):
                continue
            if h in ("at", "xy", "start", "end", "center", "mid") and len(c) >= 3:
                c[1] = num(float(c[1]) + dx)
                c[2] = num(float(c[2]) + dy)
            visit(c)

    visit(sch)


def snap_grid(sch, grid=1.27, tol=0.08):
    """EasyEDA drew on a 1.27 mm grid; KiCad's importer leaves some points a
    few hundredths off (e.g. 214.60 instead of 214.63), which breaks
    connections. Put them back."""

    def snap(v):
        f = float(v)
        g = round(f / grid) * grid
        return num(g) if abs(g - f) <= tol else v

    def visit(node):
        for c in node:
            if not isinstance(c, list):
                continue
            h = head(c)
            if h in ("lib_symbols", "title_block", "paper", "sheet_instances", "property", "size"):
                continue
            if h in ("at", "xy", "start", "end", "center", "mid") and len(c) >= 3:
                c[1], c[2] = snap(c[1]), snap(c[2])
            visit(c)

    visit(sch)


def set_page(sch, paper, title, rev, comment=None):
    i = next(i for i, c in enumerate(sch) if head(c) == "paper")
    sch[i] = [Sym("paper"), paper]
    tb = [Sym("title_block"), [Sym("title"), title], [Sym("date"), DATE], [Sym("rev"), rev],
          [Sym("company"), "flipdot-display"]]
    if comment:
        tb.append([Sym("comment"), Num("1"), comment])
    sch[:] = [c for c in sch if head(c) != "title_block"]
    sch.insert(i + 1, tb)


def fit_on_page(sch, w, h, margin=25):
    x0, y0, x1, y1 = items_bbox(sch)
    dx = round(((w - (x1 - x0)) / 2 - x0) / 1.27) * 1.27
    dy = round(((h - 40 - (y1 - y0)) / 2 + 10 - y0) / 1.27) * 1.27
    translate(sch, dx, dy)
    return dx, dy


def set_project(tree, project):
    def visit(node):
        for c in node:
            if isinstance(c, list):
                if head(c) == "project" and len(c) > 1:
                    c[1] = project
                visit(c)

    visit(tree)


def symbols(sch):
    return [c for c in sch if head(c) == "symbol" and find(c, "lib_id")]


def ref_of(sym):
    p = prop(sym, "Reference")
    return p[2] if p else None


# --------------------------------------------------------------------------
# EasyEDA side: block instances and their per-channel references


def easyeda_blocks(epro2):
    z = zipfile.ZipFile(epro2)
    docs = read_epru(z.read([n for n in z.namelist() if n.endswith(".epru")][0]).decode())
    by = {d["head"]["uuid"]: d for d in docs}
    sch_pages = {}
    for d in docs:
        if d["head"]["docType"] == "SCH_PAGE":
            sch_pages[meta(d)["schematic"]] = d["head"]["uuid"]
    sch_title = {d["head"]["uuid"]: meta(d)["title"] for d in docs if d["head"]["docType"] == "SCH"}
    page_of = {sch_title[s]: p for s, p in sch_pages.items()}

    def comp_attrs(page_uuid):
        attrs = defaultdict(dict)
        comps = {}
        for h, b in live(by[page_uuid]):
            if h["type"] == "ATTR" and b.get("parentId"):
                attrs[b["parentId"]][b["key"]] = b["value"]
            if h["type"] == "COMPONENT":
                comps[h["id"]] = b
        return comps, attrs

    top = page_of["neu"]
    comps, attrs = comp_attrs(top)
    block_syms = {}
    for d in docs:
        if d["head"]["docType"] == "SYMBOL" and meta(d).get("docType") == 17:
            block_syms[d["head"]["uuid"]] = meta(d)["title"]
    blocks = []
    for cid, b in comps.items():
        s = attrs[cid].get("Symbol")
        if s in block_syms and block_syms[s] in ("hss", "lss"):
            blocks.append({"id": cid, "kind": block_syms[s], "x": b["x"], "y": b["y"],
                           "designator": attrs[cid].get("Designator")})
    # default designators inside each block page
    inner_default = {}
    for kind in ("hss", "lss"):
        _, a = comp_attrs(page_of[kind])
        inner_default[kind] = {cid: v.get("Designator") for cid, v in a.items() if v.get("Designator")}
    # per-instance designators
    for blk in blocks:
        key_prefix = f"_${top}~{blk['id']}_$"
        inst = [d for d in docs if d["head"]["docType"] == "INSTANCE" and key_prefix in d["head"]["uuid"]]
        assert len(inst) == 1, blk
        mapping = {}
        for h, b in live(inst[0]):
            dflt = inner_default[blk["kind"]].get(h["id"])
            if dflt and b.get("Designator"):
                mapping[dflt] = b["Designator"]
        blk["refs"] = mapping
    buses = []
    recs = live(by[top])
    for h, b in recs:
        if h["type"] == "BUS":
            segs = [(l["startX"], l["startY"], l["endX"], l["endY"]) for hh, l in recs
                    if hh["type"] == "LINE" and l.get("lineGroup") == h["id"]]
            entries = [(e["pointX"], e["pointY"]) for e in b.get("busEntry", {}).values()]
            buses.append((segs, entries))
    return blocks, buses


BLOCK_GEOMETRY = {
    # sheet rectangle and pins in EasyEDA symbol units (y down), from the block symbols
    "hss": {"rect": (-20, -140, 90, -110),
            "pins": [("VMEGA", "input", -20, -130), ("IN_PD", "input", -20, -120), ("OUT", "output", 90, -125)]},
    "lss": {"rect": (-10, -130, 80, -100),
            "pins": [("IN", "input", -10, -120), ("GND", "passive", -10, -110), ("OUT", "output", 80, -120)]},
}


def wire_ends(sch):
    """Connection points: wire ends and label anchors."""
    pts = set()
    for w in find_all(sch, "wire"):
        for xy in find_all(find(w, "pts"), "xy"):
            pts.add((round(float(xy[1]), 2), round(float(xy[2]), 2)))
    for lab in sch:
        if head(lab) in ("label", "global_label", "hierarchical_label"):
            at = find(lab, "at")
            pts.add((round(float(at[1]), 2), round(float(at[2]), 2)))
    return pts


def sheet_node(name, file, x, y, w, h, pins, root, project, page):
    node = [Sym("sheet"), [Sym("at"), num(x), num(y)], [Sym("size"), num(w), num(h)],
            [Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes")], [Sym("on_board"), Sym("yes")],
            [Sym("dnp"), Sym("no")], [Sym("fields_autoplaced"), Sym("no")],
            [Sym("stroke"), [Sym("width"), num(0.1524)], [Sym("type"), Sym("solid")]],
            [Sym("fill"), [Sym("color"), Num("255"), Num("255"), Num("194"), Num("1.0000")]],
            [Sym("uuid"), new_uuid()],
            [Sym("property"), "Sheetname", name, [Sym("at"), num(x + w / 2), num(y + h / 2), Num("0")],
             [Sym("effects"), [Sym("font"), [Sym("size"), num(1.27), num(1.27)]]]],
            [Sym("property"), "Sheetfile", file, [Sym("at"), num(x), num(y + h + 0.6), Num("0")],
             [Sym("effects"), [Sym("font"), [Sym("size"), num(1.27), num(1.27)]],
              [Sym("justify"), Sym("left"), Sym("top")], [Sym("hide"), Sym("yes")]]]]
    for pname, ptype, px, py in pins:
        left = abs(px - x) < 0.01
        node.append([Sym("pin"), pname, Sym(ptype), [Sym("at"), num(px), num(py), Num("180" if left else "0")],
                     [Sym("uuid"), new_uuid()],
                     [Sym("effects"), [Sym("font"), [Sym("size"), num(1.0), num(1.0)]],
                      [Sym("justify"), Sym("left" if left else "right")]]])
    node.append([Sym("instances"), [Sym("project"), project, [Sym("path"), f"/{root}", [Sym("page"), str(page)]]]])
    return node


def anchor(node):
    at = find(node, "at")
    if at:
        return float(at[1]), float(at[2])
    pts = find(node, "pts")
    if pts:
        xy = find_all(pts, "xy")
        return [(float(p[1]), float(p[2])) for p in xy]
    st = find(node, "start")
    return (float(st[1]), float(st[2])) if st else None


def remove_examples(sch, refs):
    syms = [c for c in symbols(sch) if ref_of(c) in refs]
    assert len(syms) == len(refs), [ref_of(c) for c in syms]
    pos = [anchor(c) for c in syms]
    rects = []
    for c in sch:
        if head(c) == "rectangle":
            (x0, y0), (x1, y1) = [(float(find(c, n)[1]), float(find(c, n)[2])) for n in ("start", "end")]
            x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
            if any(x0 < px < x1 and y0 < py < y1 for px, py in pos):
                rects.append((c, (x1 - x0) * (y1 - y0), (x0, y0, x1, y1)))
    # the smallest box around each example
    boxes = {}
    for c, area, box in sorted(rects, key=lambda r: r[1]):
        for px, py in pos:
            if box[0] < px < box[2] and box[1] < py < box[3] and (px, py) not in boxes:
                boxes[(px, py)] = (c, box)
    doomed = {id(c) for c, _ in boxes.values()}

    def inside(pt):
        return any(b[0] - 0.01 <= pt[0] <= b[2] + 0.01 and b[1] - 0.01 <= pt[1] <= b[3] + 0.01
                   for _, b in boxes.values())

    for c in sch:
        h = head(c)
        if h in ("symbol", "wire", "label", "global_label", "junction", "no_connect", "text", "polyline"):
            a = anchor(c)
            if a is None:
                continue
            pts = a if isinstance(a, list) else [a]
            if all(inside(p) for p in pts):
                doomed.add(id(c))
    sch[:] = [c for c in sch if id(c) not in doomed]


def to_hier_labels(sch, names):
    """Global labels that are block ports become hierarchical labels."""
    for i, c in enumerate(sch):
        if head(c) == "global_label" and c[1] in names:
            shape = find(c, "shape")
            node = [Sym("hierarchical_label"), c[1], shape, find(c, "at")]
            if find(c, "effects"):
                node.append(find(c, "effects"))
            node.append(find(c, "uuid"))
            sch[i] = node


def build_smd(epro2, imp, pcb_in, out, project="flipdot-driver", lib="flipdot"):
    os.makedirs(out, exist_ok=True)
    top = load(f"{imp}/Board-SMD/Board-SMD.kicad_sch")
    subs = {k: load(f"{imp}/{k}/{k}.kicad_sch") for k in ("hss", "lss")}
    root = find(top, "uuid")[1]

    # The importer's page offset, from a symbol whose EasyEDA position is known (U1 at 835,-475).
    u1 = next(s for s in symbols(top) if ref_of(s) == "U1")
    ux, uy = float(find(u1, "at")[1]), float(find(u1, "at")[2])
    X0, Y0 = ux - 835 * UNIT, uy - (-475) * UNIT

    blocks, buses = easyeda_blocks(epro2)
    snap_grid(top)
    for c in top:  # EasyEDA bus syntax NAME[0:7] -> KiCad NAME[0..7]
        if head(c) in ("label", "global_label") and re.search(r"\[\d+:\d+\]$", c[1]):
            c[1] = re.sub(r"\[(\d+):(\d+)\]$", r"[\1..\2]", c[1])
    ends = wire_ends(top)

    def k(x, y):
        return X0 + x * UNIT, Y0 + y * UNIT

    # Buses: KiCad's importer drops them. Connectivity already runs through the
    # BUSX0..7 labels; this restores the drawing so the bus labels attach.
    for segs, entries in buses:
        bx = segs[0][0]
        ys = [v for sgm in segs for v in (sgm[1], sgm[3])]
        for x1, y1, x2, y2 in segs:
            (a, b_), (c, d) = k(x1, y1), k(x2, y2)
            top.append([Sym("bus"), [Sym("pts"), [Sym("xy"), num(a), num(b_)], [Sym("xy"), num(c), num(d)]],
                        [Sym("stroke"), [Sym("width"), Num("0")], [Sym("type"), Sym("default")]],
                        [Sym("uuid"), new_uuid()]])
        for ex, ey in entries:
            ty = ey - 10 if min(ys) <= ey - 10 <= max(ys) else ey + 10
            (wx, wy) = min(ends, key=lambda e: (e[0] - k(ex, ey)[0]) ** 2 + (e[1] - k(ex, ey)[1]) ** 2)
            bxk, byk = k(bx, ty)
            top.append([Sym("bus_entry"), [Sym("at"), num(wx), num(wy)],
                        [Sym("size"), num(bxk - wx), num(byk - wy)],
                        [Sym("stroke"), [Sym("width"), Num("0")], [Sym("type"), Sym("default")]],
                        [Sym("uuid"), new_uuid()]])

    # One-channel example drawings inside the switch boxes: redundant now that
    # the real channel circuits are sub-sheets, and never on the PCB.
    remove_examples(top, {"D19", "Q35", "Q36", "R97", "R99", "R100"})
    sheets = []
    for blk in blocks:
        g = BLOCK_GEOMETRY[blk["kind"]]
        rx0, ry0, rx1, ry1 = g["rect"]
        x = X0 + (blk["x"] + rx0) * UNIT
        y = Y0 + (blk["y"] + ry0) * UNIT
        w, h = (rx1 - rx0) * UNIT, (ry1 - ry0) * UNIT
        pins = []
        for pname, ptype, px, py in g["pins"]:
            pxk, pyk = X0 + (blk["x"] + px) * UNIT, Y0 + (blk["y"] + py) * UNIT
            # the importer rounds some coordinates; take the wire end it produced
            near = min(ends, key=lambda e: (e[0] - pxk) ** 2 + (e[1] - pyk) ** 2)
            assert abs(near[0] - pxk) < 0.1 and abs(near[1] - pyk) < 0.1, (blk["designator"], pname, pxk, pyk)
            pins.append((pname, ptype, near[0], near[1]))
        left = min(p[2] for p in pins)
        right = max(p[2] for p in pins)
        sheets.append((blk, left, y, right - left, h, pins))

    drop_drawing_frames(top)
    for s in subs.values():
        drop_drawing_frames(s)
    for sub in subs.values():
        snap_grid(sub)
    to_hier_labels(subs["hss"], {"VMEGA", "IN_PD", "OUT"})
    to_hier_labels(subs["lss"], {"IN", "GND", "OUT"})

    # temporary names; renamed by channel once the netlist is known
    sheet_nodes = []
    for n, (blk, x, y, w, h, pins) in enumerate(sorted(sheets, key=lambda s: (s[0]["kind"], s[1], s[2]))):
        node = sheet_node(blk["designator"], f"{blk['kind']}.kicad_sch", x, y, w, h, pins, root, project, n + 2)
        blk["sheet_uuid"] = find(node, "uuid")[1]
        blk["node"] = node
        sheet_nodes.append(node)
    insert_at = max(i for i, c in enumerate(top) if head(c) in ("symbol", "wire", "label", "global_label", "junction")) + 1
    top[insert_at:insert_at] = sheet_nodes

    # per-instance references inside the block sheets
    for kind, sch in subs.items():
        for sym in symbols(sch):
            default = ref_of(sym)
            unit = find(find(find(sym, "instances"), "project"), "path")
            unit_n = find(unit, "unit")[1] if unit and find(unit, "unit") else Num("1")
            paths = []
            for blk in (b for b in blocks if b["kind"] == kind):
                # INSTANCE documents only store designators that differ from the block's own
                ref = blk["refs"].get(default, default)
                paths.append([Sym("path"), f"/{root}/{blk['sheet_uuid']}", [Sym("reference"), ref],
                              [Sym("unit"), unit_n]])
            inst = find(sym, "instances")
            inst[:] = [Sym("instances"), [Sym("project"), project, *paths]]

    # sheet_instances of the root: only "/"
    for c in top:
        if head(c) == "sheet_instances":
            c[:] = [Sym("sheet_instances"), [Sym("path"), "/", [Sym("page"), "1"]]]

    for tree in [top, *subs.values()]:
        rename_libs(tree, lib)
        set_project(tree, project)

    print("footprints taken from the PCB:", sync_footprints([top, *subs.values()], f"{pcb_in}/Board-SMD.kicad_pcb", lib))
    tighten_boxes(top)
    name_channel_sheets(top)

    # nets that leave the top sheet for the ESP32/test point sheet
    import esp_sheet as E
    u12 = next(s for s in symbols(top) if ref_of(s) == "U12")
    for pin, net in (("3", "SW"), ("5", "FB")):
        x, y = E.pin_pos(top, u12, pin)
        top.append(global_label(net, x, y, 90, "passive"))

    tps = test_points(f"{pcb_in}/Board-SMD.kicad_pcb")
    libs = find(top, "lib_symbols")
    power_defs = [d for d in libs if head(d) == "symbol" and d[1] in (f"{lib}:Power-5V", f"{lib}:Ground-GND")]
    esp_uuid = new_uuid()
    esp = esp_sheet(top, top, root, esp_uuid, project, lib, tps, power_defs)
    save(f"{out}/esp32.kicad_sch", esp)
    esp_node = sheet_node("ESP32 + test points", "esp32.kicad_sch", 0, 0, 38.1, 12.7, [], root, project, 34)
    find(esp_node, "uuid")[1] = esp_uuid
    sheet_nodes.append(esp_node)
    top.insert(insert_at, esp_node)

    print("no-connect flags:", " ".join(add_no_connects(top, f"{pcb_in}/Board-SMD.kicad_pcb")))

    w3, h3 = 420, 297
    dx, dy = fit_on_page(top, w3, h3)
    # ESP32 sheet symbol into the free space above the title block
    x0, y0, x1, y1 = items_bbox(top)
    ex, ey = 309.88, 213.36
    find(esp_node, "at")[1:3] = [num(ex), num(ey)]
    prop(esp_node, "Sheetname")[3][1:3] = [num(ex + 19.05), num(ey + 6.35)]
    prop(esp_node, "Sheetfile")[3][1:3] = [num(ex), num(ey + 13.3)]
    set_page(top, "A3", "Flipdot driver board", "PCB8 (SMD)",
             "28x16 flipdot panel driver, ESP32 DevKit, row drivers + boost converter")
    set_page(subs["hss"], "A5", "High-side row switch (one channel)", "PCB8 (SMD)",
             "Placed 16x on the top sheet")
    set_page(subs["lss"], "A5", "Low-side row switch (one channel)", "PCB8 (SMD)",
             "Placed 16x on the top sheet")
    fit_on_page(subs["hss"], 210, 148)
    fit_on_page(subs["lss"], 210, 148)

    # library: everything the importer produced, minus EasyEDA's frame, plus our two
    lib_tree = load(f"{imp}/Board-SMD/Board-SMD-easyedapro.kicad_sym")
    lib_tree[:] = [c for c in lib_tree if not (head(c) == "symbol" and "Drawing-Symbol" in c[1])]
    for c in lib_tree:
        if head(c) == "symbol" and prop(c, "Footprint") and prop(c, "Footprint")[2].endswith(":"):
            prop(c, "Footprint")[2] = ""
    rename_libs(lib_tree, lib)
    pwr = load("/usr/share/kicad/symbols/power.kicad_sym")
    lib_tree += [E.esp_symbol(lib), E.testpoint_symbol(lib),
                 [d for d in pwr if head(d) == "symbol" and d[1] == "PWR_FLAG"][0]]
    save(f"{out}/{lib}.kicad_sym", lib_tree)
    for tree in (top, *subs.values()):
        for c in list(find(tree, "lib_symbols")) + symbols(tree):
            if head(c) == "symbol" and prop(c, "Footprint") and prop(c, "Footprint")[2].endswith(":"):
                prop(c, "Footprint")[2] = ""

    save(f"{out}/{project}.kicad_sch", top)
    for k, s in subs.items():
        save(f"{out}/{k}.kicad_sch", s)
    return blocks, tps


# --------------------------------------------------------------------------
# second pass helpers


def pcb_free_pads(pcb_path):
    """Free pads of the EasyEDA PCB (no reference): DevKit socket and test pads."""
    t = load(pcb_path)
    pads = []
    for fp in find_all(t, "footprint"):
        if prop(fp, "Reference")[2]:
            continue
        at = find(fp, "at")
        pad = find(fp, "pad")
        net = find(pad, "net")
        drill = find(pad, "drill")
        pads.append({"x": float(at[1]), "y": float(at[2]), "net": net[1] if net else "",
                     "tht": drill is not None, "name": fp[1]})
    return pads


def test_points(pcb_path):
    """Test pads ordered left to right, top to bottom -> TP1..TPn."""
    tps = sorted([p for p in pcb_free_pads(pcb_path) if not p["tht"]], key=lambda p: (round(p["y"] / 10), p["x"]))
    names = {"$1N90716": "SW", "$1N91375": "FB"}
    for i, p in enumerate(tps):
        p["ref"] = f"TP{i + 1}"
        p["netname"] = names.get(p["net"], p["net"])
    return tps


def tighten_boxes(sch, titles=("high side switches", "low side switches")):
    """Pull the top edge of the switch boxes down to their content (the example
    drawings that used to sit there are gone)."""
    for c in sch:
        if head(c) != "rectangle":
            continue
        st, en = find(c, "start"), find(c, "end")
        x0, y0, x1, y1 = float(st[1]), float(st[2]), float(en[1]), float(en[2])
        x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        texts = [t for t in sch if head(t) == "text" and t[1].strip().lower() in titles
                 and x0 - 1 < float(find(t, "at")[1]) < x1 and y0 - 1 < float(find(t, "at")[2]) < y0 + 8]
        if not texts:
            continue
        inner_y = []
        for t in sch:
            if head(t) in ("symbol", "sheet", "global_label", "label", "wire", "bus") and t is not c:
                a = anchor(t)
                for p in (a if isinstance(a, list) else [a]):
                    if p and x0 < p[0] < x1 and y0 < p[1] < y1:
                        inner_y.append(p[1])
                if head(t) == "sheet":
                    yy = float(find(t, "at")[2])
                    if x0 < float(find(t, "at")[1]) < x1 and y0 < yy < y1:
                        inner_y.append(yy)
        new_top = round((min(inner_y) - 10.16) / 1.27) * 1.27
        shift = new_top - y0
        if shift <= 0:
            continue
        if float(st[2]) < float(en[2]):
            st[2] = num(new_top)
        else:
            en[2] = num(new_top)
        for t in texts:
            find(t, "at")[2] = num(float(find(t, "at")[2]) + shift)


def name_channel_sheets(sch):
    """CBBn -> 'HS 0' / 'LS 0' by the row label on the sheet's OUT pin."""
    labels = [(c[1], float(find(c, "at")[1]), float(find(c, "at")[2])) for c in sch if head(c) == "global_label"]
    for sh in find_all(sch, "sheet"):
        out = [p for p in find_all(sh, "pin") if p[1] == "OUT"][0]
        ox, oy = float(find(out, "at")[1]), float(find(out, "at")[2])
        cand = sorted((abs(lx - ox), name) for name, lx, ly in labels if abs(ly - oy) < 0.05 and lx >= ox - 0.05)
        m = re.match(r"ROW(\d+)([AB])", cand[0][1])
        name = f"{'HS' if m.group(2) == 'B' else 'LS'} row {m.group(1)}"
        prop(sh, "Sheetname")[2] = name


def global_label(name, x, y, angle, shape="bidirectional"):
    just = "right" if angle in (180, 90) else "left"
    return [Sym("global_label"), name, [Sym("shape"), Sym(shape)], [Sym("at"), num(x), num(y), Num(str(angle))],
            [Sym("fields_autoplaced"), Sym("yes")],
            [Sym("effects"), [Sym("font"), [Sym("size"), num(1.27), num(1.27)]], [Sym("justify"), Sym(just)]],
            [Sym("uuid"), new_uuid()]]


def place_symbol(lib_id, ref, value, x, y, angle, root_path, project, footprint="", extra_props=(), pins=("1",),
                 hide_ref=False, hide_value=False, in_bom=True):
    node = [Sym("symbol"), [Sym("lib_id"), lib_id], [Sym("at"), num(x), num(y), Num(str(angle))], [Sym("unit"), Num("1")],
            [Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes" if in_bom else "no")],
            [Sym("on_board"), Sym("yes")], [Sym("dnp"), Sym("no")], [Sym("uuid"), new_uuid()]]

    def p(name, val, dx, dy, hide=False):
        eff = [Sym("effects"), [Sym("font"), [Sym("size"), num(1.27), num(1.27)]]]
        if hide:
            eff.append([Sym("hide"), Sym("yes")])
        return [Sym("property"), name, val, [Sym("at"), num(x + dx), num(y + dy), Num("0")], eff]

    node += [p("Reference", ref, 0, -6.35 if not ref.startswith("U") else -24.13, hide_ref),
             p("Value", value, 0, -4.4 if not ref.startswith("U") else 24.13, hide_value),
             p("Footprint", footprint, 0, 0, True), p("Datasheet", "", 0, 0, True), p("Description", "", 0, 0, True)]
    for k, v in extra_props:
        node.append(p(k, v, 0, 0, True))
    for pn in pins:
        node.append([Sym("pin"), pn, [Sym("uuid"), new_uuid()]])
    node.append([Sym("instances"), [Sym("project"), project, [Sym("path"), root_path, [Sym("reference"), ref],
                                                              [Sym("unit"), Num("1")]]]])
    return node


def sync_footprints(trees, pcb_path, lib="flipdot"):
    """The PCB is what was built: where a symbol names a different footprint,
    take the PCB's."""
    pcb = load(pcb_path)
    fps = {prop(fp, "Reference")[2]: fp[1].split(":")[-1] for fp in find_all(pcb, "footprint")}
    changed = []
    for tree in trees:
        for sym in symbols(tree):
            # the instance references are the real ones; a sub-sheet symbol's own
            # Reference ("R2") can collide with an unrelated top-level part
            inst = find(sym, "instances")
            refs = [find(p, "reference")[1] for pr in find_all(inst, "project") for p in find_all(pr, "path")
                    if find(p, "reference")] if inst else []
            refs = refs or [ref_of(sym)]
            fp_prop = prop(sym, "Footprint")
            for r in refs:
                if r in fps and fp_prop and fp_prop[2].split(":")[-1] != fps[r]:
                    changed.append(f"{r}: {fp_prop[2].split(':')[-1]} -> {fps[r]}")
                    fp_prop[2] = f"{lib}:{fps[r]}"
                    break
    return changed


def add_no_connects(sch, pcb_path):
    """No-connect flags on pins that touch nothing and whose pad has no net on the PCB."""
    import esp_sheet as E
    import collections
    pcb = load(pcb_path)
    nonet = set()
    for fp in find_all(pcb, "footprint"):
        r = prop(fp, "Reference")[2]
        for pad in find_all(fp, "pad"):
            if not find(pad, "net"):
                nonet.add((r, pad[1]))
    pins = []
    for sym in symbols(sch):
        lib_id = find(sym, "lib_id")[1]
        libdef = [d for d in find(sch, "lib_symbols") if head(d) == "symbol" and d[1] == lib_id][0]
        for n in set(re.findall(r'\(number "([^"]+)"', S.dumps(libdef))):
            pins.append((ref_of(sym), n, E.pin_pos(sch, sym, n)))
    count = collections.Counter((round(p[0], 2), round(p[1], 2)) for _, _, p in pins)
    ends = wire_ends(sch)
    added = []
    for ref, n, p in pins:
        key = (round(p[0], 2), round(p[1], 2))
        if (ref, n) in nonet and key not in ends and count[key] == 1:
            sch.append(no_connect(*p))
            added.append(f"{ref}.{n}")
    return added


def no_connect(x, y):
    return [Sym("no_connect"), [Sym("at"), num(x), num(y)], [Sym("uuid"), new_uuid()]]


def esp_sheet(template, top, root, sheet_uuid, project, lib, tps, power_defs, nets=None, power_nets=None, rev="PCB8 (SMD)",
              ref="U5"):
    """A4 sheet with the ESP32 DevKit socket and the test points."""
    import esp_sheet as E
    sch = [Sym("kicad_sch")] + [copy.deepcopy(c) for c in template if head(c) in ("version", "generator", "generator_version")]
    sch += [[Sym("uuid"), new_uuid()], [Sym("paper"), "A4"]]
    libs = [Sym("lib_symbols"), E.esp_symbol(lib, cached=True), E.testpoint_symbol(lib, cached=True)]
    libs += [copy.deepcopy(d) for d in power_defs]
    pwr = S.loads(open("/usr/share/kicad/symbols/power.kicad_sym").read())
    flag = S.loads(S.dumps([d for d in pwr if head(d) == "symbol" and d[1] == "PWR_FLAG"][0]))
    flag[1] = f"{lib}:PWR_FLAG"
    libs.append(flag)
    for d in libs:
        if head(d) == "symbol" and prop(d, "Footprint") and prop(d, "Footprint")[2].endswith(":"):
            prop(d, "Footprint")[2] = ""
    sch.append(libs)
    path = f"/{root}/{sheet_uuid}"
    ux, uy = 101.6, 101.6
    esp = place_symbol(f"{lib}:ESP32_DevKit_30", ref, "ESP32 DevKit (30 pin)", ux, uy, 0, path, project,
                       f"{lib}:ESP32_DevKit_30pin_2widths", pins=[str(i) for i in range(1, 31)], in_bom=False)
    sch.append(esp)
    pwr_n = [0]

    def power(net, x, y, angle):
        pwr_n[0] += 1
        lid = f"{lib}:Ground-GND" if net == "GND" else f"{lib}:Power-5V"
        return place_symbol(lid, f"#PWR{900 + pwr_n[0]}", net, x, y, angle, path, project, hide_ref=True)

    for i in range(1, 31):
        left = i <= 15
        py = uy - (E.TOP_Y - ((i - 1) % 15) * E.PIN_DY)
        px = ux - 17.78 if left else ux + 17.78
        net = (nets or E.NETS).get(i)
        if not net:
            sch.append(no_connect(px, py))
        elif net in (power_nets if power_nets is not None else E.POWER):
            d = -1 if left else 1

            def wire(a, b, c, e):
                return [Sym("wire"), [Sym("pts"), [Sym("xy"), num(a), num(b)], [Sym("xy"), num(c), num(e)]],
                        [Sym("stroke"), [Sym("width"), Num("0")], [Sym("type"), Sym("default")]],
                        [Sym("uuid"), new_uuid()]]

            if net == "GND":
                # sideways ground symbol a little further out
                sx = px + d * 5.08
                sch.append(wire(px, py, sx, py))
                sch.append(power(net, sx, py, 270 if left else 90))
            else:
                # supply: out 2.54, then up to a power flag
                sx = px + d * 2.54
                sch.append(wire(px, py, sx, py))
                sch.append(wire(sx, py, sx, py - 5.08))
                sch.append(power(net, sx, py - 5.08, 0))
        else:
            sch.append(global_label(net, px, py, 180 if left else 0, "output"))
    # test points in a row
    flagged = set()
    for n, tp in enumerate(tps):
        x, y = 165.1 + (n % 5) * 10.16, 76.2 + (n // 5) * 25.4
        sch.append(place_symbol(f"{lib}:TestPoint", tp["ref"], tp["netname"], x, y, 0, path, project,
                                f"{lib}:TestPoint_Pad_D0.6mm", hide_value=False, in_bom=False))
        stub = [Sym("wire"), [Sym("pts"), [Sym("xy"), num(x), num(y)], [Sym("xy"), num(x), num(y + 5.08)]],
                [Sym("stroke"), [Sym("width"), Num("0")], [Sym("type"), Sym("default")]], [Sym("uuid"), new_uuid()]]
        sch.append(stub)
        if tp["netname"] in ("+5V", "GND", "+HV"):
            sch.append(power(tp["netname"], x, y + 5.08, 0 if tp["netname"] == "GND" else 180))
            if tp["netname"] not in flagged:
                # these nets are driven by parts with untyped pins (USB socket, boost IC)
                flagged.add(tp["netname"])
                pwr_n[0] += 1
                sch.append(place_symbol(f"{lib}:PWR_FLAG", f"#FLG{pwr_n[0]:02d}", "PWR_FLAG", x, y + 2.54, 270,
                                        path, project, hide_ref=True))
                sch.append([Sym("junction"), [Sym("at"), num(x), num(y + 2.54)], [Sym("diameter"), Num("0")],
                            [Sym("color"), Num("0"), Num("0"), Num("0"), Num("0")], [Sym("uuid"), new_uuid()]])
        else:
            sch.append(global_label(tp["netname"], x, y + 5.08, 270, "passive"))
    if tps:
        sch.append([Sym("text"), "Test pads on the PCB (bare copper, no part)", [Sym("exclude_from_sim"), Sym("no")],
                    [Sym("at"), num(165.1), num(66.04), Num("0")],
                    [Sym("effects"), [Sym("font"), [Sym("size"), num(1.524), num(1.524)]], [Sym("justify"), Sym("left")]],
                    [Sym("uuid"), new_uuid()]])
    sch.append([Sym("text"), "ESP32 DevKit socket: two header pairs per side for narrow and wide modules.\n"
                "The module is plugged in, not assembled (not in the BOM).", [Sym("exclude_from_sim"), Sym("no")],
                [Sym("at"), num(76.2), num(134.62), Num("0")],
                [Sym("effects"), [Sym("font"), [Sym("size"), num(1.524), num(1.524)]], [Sym("justify"), Sym("left")]],
                [Sym("uuid"), new_uuid()]])
    sch.append([Sym("embedded_fonts"), Sym("no")])
    set_page(sch, "A4", "ESP32 DevKit socket" + (" and test points" if tps else ""), rev)
    return sch


def build_flat(imp_name, pcb_path, out, project, title, rev, paper, size, comment=None, lib="flipdot",
               esp_nets=None, esp_at=(0, 0)):
    """A schematic without hierarchy (Rev 1.2 board, panel drawing)."""
    os.makedirs(out, exist_ok=True)
    sch = load(f"{IMP}/{imp_name}/{imp_name}.kicad_sch")
    snap_grid(sch)
    drop_drawing_frames(sch)
    rename_libs(sch, lib)
    set_project(sch, project)
    for c in sch:
        if head(c) == "sheet_instances":
            c[:] = [Sym("sheet_instances"), [Sym("path"), "/", [Sym("page"), "1"]]]
    if pcb_path:
        print("no-connect flags:", " ".join(add_no_connects(sch, pcb_path)))
        print("footprints taken from the PCB:", sync_footprints([sch], pcb_path, lib))
    for c in list(find(sch, "lib_symbols")) + symbols(sch):
        if head(c) == "symbol" and prop(c, "Footprint") and prop(c, "Footprint")[2].endswith(":"):
            prop(c, "Footprint")[2] = ""
    fit_on_page(sch, *size)
    set_page(sch, paper, title, rev, comment)
    if esp_nets:
        root = find(sch, "uuid")[1]
        power_defs = [d for d in find(sch, "lib_symbols") if head(d) == "symbol" and d[1] in (f"{lib}:Ground-GND",)]
        esp_uuid = new_uuid()
        esp = esp_sheet(sch, sch, root, esp_uuid, project, lib, [], power_defs, esp_nets, {"GND"}, rev, "U6")
        save(f"{out}/esp32.kicad_sch", esp)
        node = sheet_node("ESP32 DevKit", "esp32.kicad_sch", esp_at[0], esp_at[1], 38.1, 12.7, [], root, project, 2)
        find(node, "uuid")[1] = esp_uuid
        prop(node, "Sheetname")[3][1:3] = [num(esp_at[0] + 19.05), num(esp_at[1] + 6.35)]
        sch.insert(len(sch) - 1, node)
    save(f"{out}/{project}.kicad_sch", sch)
    import glob
    lib_tree = load(glob.glob(f"{IMP}/{imp_name}/*-easyedapro.kicad_sym")[0])
    lib_tree[:] = [c for c in lib_tree if not (head(c) == "symbol" and "Drawing-Symbol" in c[1])]
    for c in lib_tree:
        if head(c) == "symbol" and prop(c, "Footprint") and prop(c, "Footprint")[2].endswith(":"):
            prop(c, "Footprint")[2] = ""
    rename_libs(lib_tree, lib)
    if esp_nets:
        import esp_sheet as E
        lib_tree += [E.esp_symbol(lib)]
    save(f"{out}/{lib}.kicad_sym", lib_tree)


IMP = None

if __name__ == "__main__":
    epro2, imp, pcb_in, out = sys.argv[1:5]
    IMP = imp
    if "--rev12" in sys.argv:
        build_flat("Board-Rev-1.2", f"{pcb_in}/Board-Rev-1.2.kicad_pcb", out, "flipdot-driver-rev1.2",
                   "Flipdot driver board, Rev 1.2 (through-hole)", "Rev 1.2", "A3", (420, 297),
                   "Superseded by the SMD board (PCB8)",
                   esp_nets={1: "5V", 2: "GND", 3: "FP_EN", 4: "FP_DATA", 5: "FP_A0", 6: "FP_A1", 7: "FP_A2",
                             8: "FP_B0", 9: "FP_B1", 16: "3.3V", 17: "GND", 18: "SR_ICLK", 19: "SR_OCLK",
                             20: "DATA", 21: "SR_OE#"},
                   esp_at=(30.48, 254.0))
        sys.exit(0)
    if "--panel" in sys.argv:
        build_flat("generelle-ged-ns-zeichnung", None, out, "panel-wiring",
                   "Flipdot panel: coil and diode matrix (reference drawing)", "-", "A3", (420, 297),
                   "Documentation only: how the 28x16 panel is wired internally")
        sys.exit(0)
    blocks, tps = build_smd(epro2, imp, pcb_in, out)
    print("sheets", len(blocks), "test points", len(tps))
