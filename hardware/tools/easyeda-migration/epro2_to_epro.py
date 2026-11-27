#!/usr/bin/env python3
"""Convert an EasyEDA Pro v3 project (.epro2, containing a .epru) into the
older .epro (format "V1") that KiCad's EasyEDA Pro importer reads.

Usage: epro2_to_epro.py project.epro2 outdir [--board "Board SMD" ...]

Writes one .epro per board (each holding only that board's schematic and
PCB, so KiCad does not need to ask which one to import), plus one per
schematic that belongs to no board.

Only what KiCad's importer reads is converted carefully. Design rules,
panelization, pour fill caches and editor preferences are dropped; KiCad
refills zones itself.
"""

import io
import json
import os
import re
import sys
import zipfile
from collections import defaultdict

# --------------------------------------------------------------------------
# reading


def read_epru(data):
    docs = []
    cur = None
    for line in data.splitlines():
        if not line:
            continue
        if line.endswith("|"):
            line = line[:-1]
        hdr_s, _, body_s = line.partition("||")
        hdr = json.loads(hdr_s)
        body = json.loads(body_s) if body_s else None
        if hdr.get("type") == "DOCHEAD":
            cur = {"head": body, "records": []}
            docs.append(cur)
        else:
            cur["records"].append((hdr, body))
    return docs


def meta(doc):
    for h, b in doc["records"]:
        if h["type"] == "META":
            return b or {}
    return {}


def live(doc):
    """Records with a body (a null body marks a deleted object)."""
    return [(h, b) for h, b in doc["records"] if b is not None]


# --------------------------------------------------------------------------
# helpers

H_ALIGN = {"LEFT": 0, "CENTER": 1, "RIGHT": 2}
V_ALIGN = {"TOP": 0, "MIDDLE": 1, "BOTTOM": 2}
ORIGIN = {
    "LEFT_TOP": 1, "LEFT_MIDDLE": 2, "LEFT_BOTTOM": 3,
    "CENTER_TOP": 4, "CENTER_MIDDLE": 5, "CENTER_BOTTOM": 6,
    "RIGHT_TOP": 7, "RIGHT_MIDDLE": 8, "RIGHT_BOTTOM": 9,
}


def b2i(v):
    if isinstance(v, bool):
        return int(v)
    return v


def neg(v):
    return -v if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def flip_xy(points):
    """[x, y, x, y, ...] with y negated (schematic coordinates)."""
    return [neg(v) if i % 2 else v for i, v in enumerate(points)]


def origin(v):
    if isinstance(v, str):
        return ORIGIN.get(v, 3)
    return v if v is not None else 3


class Styles:
    """Collects FONTSTYLE / LINESTYLE records for one schematic file."""

    def __init__(self):
        self.font = {}
        self.line = {}
        self.records = []

    def font_style(self, b):
        align = b.get("align")
        h = v = None
        if isinstance(align, str) and "_" in align:
            hs, vs = align.split("_", 1)
            h, v = H_ALIGN.get(hs), V_ALIGN.get(vs)
        key = (
            b.get("fillColor"), b.get("color"), b.get("fontFamily"), b.get("fontSize"),
            b2i(b.get("italic")), b2i(b.get("fontWeight")), b2i(b.get("underline")),
            b2i(b.get("strikeout")), v, h,
        )
        if key not in self.font:
            sid = f"st{len(self.font) + len(self.line) + 1}"
            self.font[key] = sid
            self.records.append(["FONTSTYLE", sid, *key])
        return self.font[key]

    def line_style(self, b):
        key = (b.get("strokeColor"), b.get("strokeStyle"), b.get("fillColor"), b.get("strokeWidth"))
        if key not in self.line:
            sid = f"st{len(self.font) + len(self.line) + 1}"
            self.line[key] = sid
            self.records.append(["LINESTYLE", sid, *key])
        return self.line[key]


def dump_lines(lines):
    # no trailing newline: KiCad turns an empty last line into a null record
    return "\n".join(json.dumps(l, ensure_ascii=False, separators=(",", ":")) for l in lines)


# --------------------------------------------------------------------------
# schematic-side records (symbols and sheets): y axis is flipped


def sch_attr(i, b, st):
    x, y = b.get("x"), b.get("y")
    return ["ATTR", i, b.get("parentId", ""), b.get("key"), b.get("value"),
            b.get("keyVisible"), b.get("valueVisible"), x, neg(y),
            b.get("rotation"), st.font_style(b), 0]


def sch_primitive(t, i, b, st, blobs):
    if t == "RECT":
        return ["RECT", i, b["dotX1"], neg(b["dotY1"]), b["dotX2"], neg(b["dotY2"]),
                b.get("radiusX", 0), b.get("radiusY", 0), b.get("rotation", 0), st.line_style(b), 0]
    if t == "CIRCLE":
        return ["CIRCLE", i, b["centerX"], neg(b["centerY"]), b["radius"], st.line_style(b), 0]
    if t == "ELLIPSE":
        return ["ELLIPSE", i, b["centerX"], neg(b["centerY"]), abs(b["radiusX"]), abs(b["radiusY"]),
                b.get("rotation", 0), st.line_style(b), 0]
    if t == "ARC":
        return ["ARC", i, b["startX"], neg(b["startY"]), b["referX"], neg(b["referY"]),
                b["endX"], neg(b["endY"]), st.line_style(b), 0]
    if t == "BEZIER":
        pts = []
        for p in b.get("points", []):
            pts += [p["x"], -p["y"]]
        return ["BEZIER", i, pts, st.line_style(b), 0]
    if t == "POLY":
        pts = []
        for p in b["points"]:
            pts += [p["x"], -p["y"]]
        return ["POLY", i, pts, b2i(b.get("closed", False)) == 1, st.line_style(b), 0]
    if t == "TEXT":
        return ["TEXT", i, b["x"], neg(b["y"]), b.get("rotation", 0), b.get("value", ""), st.font_style(b), 0]
    if t == "OBJ":
        content = b.get("content", "")
        if content.startswith("blob:"):
            content = blobs.get(content[5:], content)
        return ["OBJ", i, b.get("fileName", ""), b["startX"], neg(b["startY"]), b["width"], b["height"],
                b.get("rotation", 0), b2i(b.get("isMirror", False)), content]
    return None


PIN_SHAPE = {"NONE": 0, "INVERTED": 2}


def convert_symbol(doc, blobs):
    m = meta(doc)
    st = Styles()
    parts = []  # [(title, bbox, [records])]
    by_part = {}
    for h, b in live(doc):
        t, i = h["type"], h["id"]
        if t == "PART":
            entry = (b["title"], {"BBOX": b.get("BBOX")}, [])
            parts.append(entry)
            by_part[b["title"]] = entry[2]
            continue
        if t in ("META", "CANVAS", "ELE_PLACEHOLDER", "TABLE", "GROUP"):
            continue
        pid = b.get("partId")
        if pid not in by_part:
            # a primitive before any PART: attach to the first one
            if not parts:
                continue
            pid = parts[0][0]
        out = by_part[pid]
        if t == "ATTR":
            out.append(sch_attr(i, b, st))
        elif t == "PIN":
            out.append(["PIN", i, b2i(b.get("display", True)), 1, b["x"], neg(b["y"]), b["length"],
                        b["rotation"], b.get("color"), PIN_SHAPE.get(b.get("pinShape"), 0), 0, 1])
        else:
            r = sch_primitive(t, i, b, st, blobs)
            if r:
                out.append(r)
            else:
                print(f"  symbol {m.get('title')}: skipping {t}", file=sys.stderr)
    lines = [["DOCTYPE", "SYMBOL", "1.1"],
             ["HEAD", {"symbolType": m.get("docType", 2), "originX": 0, "originY": 0, "version": "0.13.0"}]]
    lines += st.records
    for title, bbox, recs in parts:
        lines.append(["PART", title, bbox])
        lines += recs
    return dump_lines(lines)


def convert_sheet(doc, blobs):
    st = Styles()
    out = []
    wire_lines = defaultdict(list)
    recs = live(doc)
    for h, b in recs:
        if h["type"] == "LINE" and b.get("lineGroup"):
            wire_lines[b["lineGroup"]].append(b)
    for h, b in recs:
        t, i = h["type"], h["id"]
        if t in ("META", "CANVAS", "ELE_PLACEHOLDER", "GROUP", "LINE", "BUS"):
            continue
        if t == "COMPONENT":
            # "pid…" is V3's placeholder for "the only part"; V1 left it empty
            part = b.get("partId", "")
            part = "" if part.startswith("pid") else part
            out.append(["COMPONENT", i, part, b["x"], neg(b["y"]), b.get("rotation", 0),
                        b2i(b.get("isMirror", False)), b.get("attrs") or {}, 0])
        elif t == "WIRE":
            segs = [[l["startX"], -l["startY"], l["endX"], -l["endY"]] for l in wire_lines.get(i, [])]
            style = st.line_style(wire_lines[i][0]) if wire_lines.get(i) else st.line_style({})
            out.append(["WIRE", i, segs, style, 0])
        elif t == "ATTR":
            out.append(sch_attr(i, b, st))
        else:
            r = sch_primitive(t, i, b, st, blobs)
            if r:
                out.append(r)
            else:
                print(f"  sheet: skipping {t}", file=sys.stderr)
    lines = [["DOCTYPE", "SCH", "1.1"], ["HEAD", {"originX": 0, "originY": 0, "version": "2"}]]
    return dump_lines(lines + st.records + out)


# --------------------------------------------------------------------------
# PCB-side records (footprints and boards): same axis as the old format


def pcb_attr(i, b):
    return ["ATTR", i, b.get("groupId", 0), b.get("parentId", ""), b["layerId"], b.get("x"), b.get("y"),
            b.get("key"), b.get("value"), b2i(b.get("keyVisible")), b2i(b.get("valueVisible")),
            b.get("fontFamily"), b.get("fontSize"), b.get("strokeWidth"), b2i(b.get("bold")),
            b2i(b.get("italic")), origin(b.get("origin")), b.get("angle", 0), b2i(b.get("reverse")),
            b.get("expansion", 0), b2i(b.get("mirror")), b2i(b.get("locked"))]


def pad_shape(dp):
    if dp is None:
        return None
    t = dp["padType"]
    if t == "RECT":
        return ["RECT", dp["width"], dp["height"], dp.get("radius", 0)]
    if t in ("ELLIPSE", "OVAL"):
        return [t, dp["width"], dp["height"]]
    if t in ("POLYGON", "POLY"):
        return ["POLY", dp.get("path", [])]
    return [t, dp.get("width"), dp.get("height")]


def pcb_record(t, hid, b):
    i = hid
    if t == "LAYER":
        n = json.loads(hid)[1]
        flag = (1 if b.get("use") else 0) | (2 if b.get("show") else 0)
        return ["LAYER", n, b["layerType"], b["layerName"], flag, b.get("activeColor"),
                b.get("activateTransparency"), b.get("inactiveColor"), b.get("inactiveTransparency")]
    if t == "NET":
        return ["NET", json.loads(hid)[1], None, None, 1, None, 0, None]
    if t == "PAD_NET":
        _, comp, pad, padid = json.loads(hid)
        return ["PAD_NET", comp, pad, b.get("padNet", ""), padid]
    if t == "COMPONENT":
        return ["COMPONENT", i, b.get("groupId", 0), b["layerId"], b["x"], b["y"], b.get("angle", 0),
                b.get("attrs") or {}, b2i(b.get("locked"))]
    if t == "ATTR":
        return pcb_attr(i, b)
    if t == "PAD":
        hole = b.get("hole")
        hole = [hole["holeType"], hole["width"], hole["height"]] if hole else None
        return ["PAD", i, b.get("groupId", 0), b.get("netName", ""), b["layerId"], b.get("num", ""),
                b["centerX"], b["centerY"], b.get("padAngle", 0), hole, pad_shape(b.get("defaultPad")),
                b.get("specialPad") or [], b.get("padOffsetX", 0), b.get("padOffsetY", 0),
                b.get("relativeAngle", 0), b2i(b.get("plated", True)), 0 if b.get("padType") == "NORMAL" else 1,
                b.get("topSolderExpansion"), b.get("bottomSolderExpansion"), b.get("topPasteExpansion"),
                b.get("bottomPasteExpansion"), b2i(b.get("locked")), b.get("connectMode"), b.get("spokeSpace"),
                b.get("spokeWidth"), b.get("spokeAngle"), b.get("unusedInnerLayers") or []]
    if t == "VIA":
        return ["VIA", i, b.get("groupId", 0), b.get("netName", ""), b.get("ruleName", ""),
                b["centerX"], b["centerY"], b["holeDiameter"], b["viaDiameter"], 0,
                b.get("topSolderExpansion"), b.get("bottomSolderExpansion"), b2i(b.get("locked"))]
    if t == "LINE":
        return ["LINE", i, b.get("groupId", 0), b.get("netName", ""), b["layerId"], b["startX"], b["startY"],
                b["endX"], b["endY"], b["width"], b2i(b.get("locked"))]
    if t == "ARC":
        return ["ARC", i, b.get("groupId", 0), b.get("netName", ""), b["layerId"], b["startX"], b["startY"],
                b["endX"], b["endY"], b["arcAngle"], b["width"], b2i(b.get("locked"))]
    if t == "POLY":
        return ["POLY", i, b.get("groupId", 0), b.get("netName", ""), b["layerId"], b["width"], b["path"],
                b2i(b.get("locked"))]
    if t == "FILL":
        return ["FILL", i, b.get("groupId", 0), b.get("netName", ""), b["layerId"], b["width"],
                0 if b.get("fillStyle") == "SOLID" else 1, b["path"], b2i(b.get("locked"))]
    if t == "REGION":
        return ["REGION", i, b.get("groupId", 0), b["layerId"], b.get("width", 0), b.get("regionType") or [],
                b["path"], b2i(b.get("locked"))]
    if t == "POUR":
        return ["POUR", i, b.get("groupId", 0), b.get("netName", ""), b["layerId"], b["width"],
                b.get("name", ""), b.get("order", 0), b["path"], [], b2i(b.get("locked"))]
    if t == "STRING":
        return ["STRING", i, b.get("groupId", 0), b["layerId"], b["x"], b["y"], b["text"], b.get("fontFamily"),
                b["fontSize"], b["strokeWidth"], b2i(b.get("bold")), b2i(b.get("italic")),
                origin(b.get("origin")), b.get("angle", 0), b2i(b.get("reverse")), b.get("expansion", 0),
                b2i(b.get("mirror")), b2i(b.get("locked"))]
    if t == "IMAGE":
        return ["IMAGE", i, b.get("groupId", 0), b["layerId"], b["startX"], b["startY"], b["width"], b["height"],
                b.get("angle", 0), b2i(b.get("mirror")), b["path"], b2i(b.get("locked"))]
    return None


PCB_SKIP = {"META", "CANVAS", "ELE_PLACEHOLDER", "GROUP", "RULE", "RULE_TEMPLATE", "RULE_SELECTOR",
            "PRIMITIVE", "SILK_OPTS", "PREFERENCE", "LAYER_PHYS", "PANELIZE", "ACTIVE_LAYER",
            "POURED", "DIMENSION", "CONNECT", "STRING_SETTINGS", "LENGTH"}


def convert_pcb_doc(doc, doctype):
    lines = [["DOCTYPE", doctype, "1.8"]]
    if doctype == "PCB":  # V1 footprint files have no HEAD line
        lines.append(["HEAD", {"editorVersion": "2.2.43.4", "importFlag": 0}])
    skipped = defaultdict(int)
    for h, b in live(doc):
        t = h["type"]
        if t in PCB_SKIP:
            continue
        r = pcb_record(t, h["id"], b)
        if r is None:
            skipped[t] += 1
        else:
            lines.append(r)
    if skipped:
        print(f"  {doctype} {meta(doc).get('title')}: skipped {dict(skipped)}", file=sys.stderr)
    return dump_lines(lines)


# --------------------------------------------------------------------------
# project


def convert(epro2_path, outdir):
    z = zipfile.ZipFile(epro2_path)
    epru_name = [n for n in z.namelist() if n.endswith(".epru")][0]
    docs = read_epru(z.read(epru_name).decode("utf-8"))
    title = json.loads(z.read("project2.json")).get("title", "project")

    by_type = defaultdict(list)
    for d in docs:
        by_type[d["head"]["docType"]].append(d)

    blobs = {}
    for d in by_type["BLOB"]:
        for h, b in live(d):
            if h["type"] == "BLOB":
                blobs[h["id"]] = b.get("content", "")

    symbols, footprints, devices = {}, {}, {}
    files = {}
    for d in by_type["SYMBOL"]:
        u, m = d["head"]["uuid"], meta(d)
        symbols[u] = {"source": m.get("source", ""), "desc": m.get("description", ""),
                      "tags": {"parent_tag": [], "child_tag": []},
                      "custom_tags": json.dumps(m.get("tags", []), ensure_ascii=False),
                      "title": m.get("title", u), "version": d["head"].get("version", ""),
                      "type": m.get("docType", 2)}
        files[f"SYMBOL/{u}.esym"] = convert_symbol(d, blobs)
    for d in by_type["FOOTPRINT"]:
        u, m = d["head"]["uuid"], meta(d)
        footprints[u] = {"title": m.get("title", u), "source": m.get("source", ""),
                         "version": d["head"].get("version", ""), "type": 4,
                         "desc": m.get("description", ""), "tags": {"parent_tag": [], "child_tag": []},
                         "custom_tags": json.dumps(m.get("tags", []), ensure_ascii=False)}
        files[f"FOOTPRINT/{u}.efoo"] = convert_pcb_doc(d, "FOOTPRINT")
    for d in by_type["DEVICE"]:
        u, m = d["head"]["uuid"], meta(d)
        devices[u] = {"title": m.get("title", u), "attributes": m.get("attributes", {}),
                      "description": m.get("description", ""),
                      "custom_tags": json.dumps(m.get("tags", []), ensure_ascii=False),
                      "images": m.get("images", []), "source": m.get("source", ""),
                      "version": d["head"].get("version", "")}

    boards = {d["head"]["uuid"]: meta(d).get("title") for d in by_type["BOARD"]}
    schematics = {}
    sch_board = {}
    for d in by_type["SCH"]:
        m = meta(d)
        schematics[d["head"]["uuid"]] = {"name": m.get("title"), "sheets": []}
        sch_board[d["head"]["uuid"]] = m.get("board", "")
    for d in sorted(by_type["SCH_PAGE"], key=lambda d: meta(d).get("zIndex") or 0):
        m = meta(d)
        sch = schematics[m["schematic"]]
        sid = len(sch["sheets"]) + 1
        sch["sheets"].append({"name": m.get("title", f"P{sid}"), "id": sid, "uuid": d["head"]["uuid"]})
        files[f"SHEET/{m['schematic']}/{sid}.esch"] = convert_sheet(d, blobs)
    pcbs, pcb_board = {}, {}
    for d in by_type["PCB"]:
        m = meta(d)
        pcbs[d["head"]["uuid"]] = m.get("title")
        pcb_board[d["head"]["uuid"]] = m.get("board", "")
        files[f"PCB/{d['head']['uuid']}.epcb"] = convert_pcb_doc(d, "PCB")


    # One project file per board, plus one per board-less schematic.
    jobs = []
    for bu, btitle in boards.items():
        sch = [s for s, b in sch_board.items() if b == bu]
        pcb = [p for p, b in pcb_board.items() if b == bu]
        jobs.append((btitle, sch, pcb))
    for s, b in sch_board.items():
        if not b:
            jobs.append((schematics[s]["name"], [s], []))

    os.makedirs(outdir, exist_ok=True)
    written = []
    for name, sch, pcb in jobs:
        project = {
            "schematics": {s: schematics[s] for s in sch},
            "pcbs": {p: pcbs[p] for p in pcb},
            "panels": {},
            "symbols": symbols,
            "footprints": footprints,
            "devices": devices,
            "boards": {name: {"schematic": sch[0] if sch else "", "pcb": pcb[0] if pcb else ""}} if pcb else {},
            "config": {"title": title, "cbbProject": False, "defaultSheet": "", "editorVersion": "2.2.43.4"},
        }
        fname = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") + ".epro"
        path = os.path.join(outdir, fname)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
            out.writestr("project.json", json.dumps(project, ensure_ascii=False))
            for n, data in files.items():
                kind, rest = n.split("/", 1)
                if kind == "SHEET" and rest.split("/")[0] not in sch:
                    continue
                if kind == "PCB" and rest.split(".")[0] not in pcb:
                    continue
                out.writestr(n, data)
        written.append((name, path, sch, pcb))
    return written, docs


if __name__ == "__main__":
    for name, path, sch, pcb in convert(sys.argv[1], sys.argv[2])[0]:
        print(f"{name}: {path} ({len(sch)} schematic, {len(pcb)} pcb)")
