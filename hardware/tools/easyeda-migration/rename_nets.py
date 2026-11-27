#!/usr/bin/env python3
"""Rename PCB nets to the schematic's net names; report any connectivity disagreement.
usage: rename_nets.py board.kicad_pcb netlist.net"""
import sys, collections, os
sys.path.insert(0, os.path.dirname(__file__))
import sexpr as S
import pcbnew
pcb, net = sys.argv[1:3]
t = S.loads(open(net, encoding="utf-8").read())
sch_net = {}
for n in S.find(t, "nets"):
    if S.head(n) != "net":
        continue
    name = S.find(n, "name")[1]
    for node in S.find_all(n, "node"):
        sch_net[(S.find(node, "ref")[1], S.find(node, "pin")[1])] = name
b = pcbnew.LoadBoard(pcb)
votes = collections.defaultdict(collections.Counter)
for f in b.GetFootprints():
    for p in f.Pads():
        if p.GetNetname():
            votes[p.GetNetname()][sch_net.get((f.GetReference(), p.GetNumber()), "<none>")] += 1
mapping, problems = {}, []
for old, c in votes.items():
    if len(c) != 1:
        problems.append((old, dict(c)))
    mapping[old] = c.most_common(1)[0][0]
inv = collections.defaultdict(list)
for o, n in mapping.items():
    inv[n].append(o)
# Nets the later schematic fix merges on purpose (see hardware/README: PCB8 erratum).
ALLOWED = {"GND": {"GND", "$1N38170", "$1N37900"}}
merged = {n: o for n, o in inv.items() if len(o) > 1 and set(o) != ALLOWED.get(n)}
print("pcb nets:", len(mapping), "renamed:", sum(o != n for o, n in mapping.items()))
print("pcb nets split across schematic nets:", problems)
print("schematic nets covering several pcb nets:", merged)
if problems or merged or "<none>" in inv:
    sys.exit("connectivity differs, not renaming")
nets = b.GetNetInfo().NetsByName()
targets = {}
for ni in list(nets.values()):
    old = ni.GetNetname()
    if old in mapping and mapping[old] != old and mapping[old] not in nets:
        ni.SetNetname(mapping[old])
# merged nets: move their items onto the surviving net
by_name = b.GetNetInfo().NetsByName()
for old, new in mapping.items():
    if old != new and old in by_name and new in by_name and by_name[old].GetNetCode() != by_name[new].GetNetCode():
        code = by_name[new].GetNetCode()
        n_items = 0
        for f in b.GetFootprints():
            for pd in f.Pads():
                if pd.GetNetname() == old:
                    pd.SetNetCode(code); n_items += 1
        for t in b.GetTracks():
            if t.GetNetname() == old:
                t.SetNetCode(code); n_items += 1
        for z in b.Zones():
            if z.GetNetname() == old:
                z.SetNetCode(code); n_items += 1
        print(f"merged {old} into {new}: {n_items} items")
# unused pins: KiCad gives them their own "unconnected-(...)" nets
added = 0
for f in b.GetFootprints():
    for pd in f.Pads():
        want = sch_net.get((f.GetReference(), pd.GetNumber()))
        if want and not pd.GetNetname() and not want.startswith("unconnected-"):
            print(f"pad {f.GetReference()}.{pd.GetNumber()} had no net on the PCB, schematic says {want}")
        if want and not pd.GetNetname():
            ni = b.FindNet(want)
            if ni is None:
                ni = pcbnew.NETINFO_ITEM(b, want)
                b.Add(ni)
            pd.SetNet(ni)
            added += 1
print("unconnected-pin nets:", added)
b.BuildConnectivity()
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
pcbnew.SaveBoard(pcb, b)
print("saved")
