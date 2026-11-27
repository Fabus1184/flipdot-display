#!/usr/bin/env python3
"""Library tables, project settings, zone fill. usage: finish_project.py project_dir name"""
import json, os, sys
pdir, name = sys.argv[1:3]
open(f"{pdir}/sym-lib-table", "w").write(
    '(sym_lib_table\n\t(version 7)\n\t(lib (name "flipdot")(type "KiCad")(uri "${KIPRJMOD}/flipdot.kicad_sym")(options "")(descr "Project symbols"))\n)\n')
open(f"{pdir}/fp-lib-table", "w").write(
    '(fp_lib_table\n\t(version 7)\n\t(lib (name "flipdot")(type "KiCad")(uri "${KIPRJMOD}/flipdot.pretty")(options "")(descr "Project footprints"))\n)\n')
pro_path = f"{pdir}/{name}.kicad_pro"
p = json.load(open(pro_path))
p["meta"]["filename"] = f"{name}.kicad_pro"
p["sheets"] = []
sev = p["erc"]["rule_severities"]
# EasyEDA symbols carry no pin electrical types ("Unspecified"); these checks only produce noise.
for k in ("pin_to_pin", "pin_not_driven", "power_pin_not_driven"):
    sev[k] = "ignore"
nc = p["net_settings"]["classes"][0]
# the EasyEDA design rule was 4 mil (0.102 mm) clearance; tracks are routed at that spacing
nc.update({"clearance": 0.1, "track_width": 0.254, "via_diameter": 0.6, "via_drill": 0.3})
board = p.setdefault("board", {})
ds = board.setdefault("design_settings", {})
ds["rules"] = {  # JLCPCB standard 2-layer capabilities, with margin
    "min_clearance": 0.1, "min_track_width": 0.127, "min_via_diameter": 0.45, "min_via_annular_width": 0.1,
    "min_through_hole_diameter": 0.3, "min_hole_clearance": 0.25, "min_hole_to_hole": 0.25,
    "min_copper_edge_clearance": 0.3, "min_silk_clearance": 0.0, "min_text_height": 0.6,
    "min_text_thickness": 0.08, "solder_mask_to_copper_clearance": 0.0,
}
json.dump(p, open(pro_path, "w"), indent=2)

import pcbnew  # noqa: E402
b = pcbnew.LoadBoard(f"{pdir}/{name}.kicad_pcb")
# EasyEDA layer names ("Top Layer", "Multi-Layer" for the outline) -> KiCad's
for lid in b.GetEnabledLayers().Seq():
    b.SetLayerName(lid, pcbnew.BOARD.GetStandardLayerName(lid))
zones = b.Zones()
for z in zones:
    if z.GetNetname():
        z.SetThermalReliefGap(pcbnew.FromMM(0.254))
        z.SetThermalReliefSpokeWidth(pcbnew.FromMM(0.3))
        z.SetLocalClearance(pcbnew.FromMM(0.2))
        z.SetMinThickness(pcbnew.FromMM(0.15))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)  # direct connection, as on the EasyEDA board
pcbnew.ZONE_FILLER(b).Fill(zones)
pcbnew.SaveBoard(f"{pdir}/{name}.kicad_pcb", b)
print("zones filled:", len(zones))
