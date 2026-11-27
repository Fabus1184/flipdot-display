#!/usr/bin/env bash
# Checks a board project and writes fabrication and documentation outputs to
# hardware/kicad/<project>/output/ (git-ignored).
#
# usage: hardware/export.sh [project]      default: flipdot-driver
#
# Gates: ERC must be clean, DRC must report no unconnected items and no
# schematic/PCB parity issues. Other DRC findings (silkscreen, the USB-C
# footprint's edge clearance) are known and listed in drc.rpt.
set -euo pipefail

PROJECT="${1:-flipdot-driver}"
cd "$(dirname "$0")/kicad/$PROJECT"
OUT=output
rm -rf "$OUT"
mkdir -p "$OUT/gerbers"

HERE="$(pwd)"

echo "== ERC"
kicad-cli sch erc --exit-code-violations -o "$HERE/$OUT/erc.rpt" "$HERE/$PROJECT.kicad_sch" | grep -v Debug: || true
grep -q "ERC messages: 0" "$OUT/erc.rpt" || { echo "ERC failed, see $OUT/erc.rpt"; exit 1; }

echo "== DRC"
kicad-cli pcb drc --schematic-parity -o "$HERE/$OUT/drc.rpt" "$HERE/$PROJECT.kicad_pcb" 2>&1 \
    | grep '^Found' | tee "$OUT/drc-summary.txt"
grep -q "Found 0 unconnected items" "$OUT/drc-summary.txt" || { echo "DRC: unconnected items, see $OUT/drc.rpt"; exit 1; }
grep -q "Found 0 schematic parity issues" "$OUT/drc-summary.txt" || { echo "DRC: parity issues, see $OUT/drc.rpt"; exit 1; }

echo "== schematic PDF"
kicad-cli sch export pdf -o "$OUT/$PROJECT-schematic.pdf" "$PROJECT.kicad_sch"

echo "== BOM"
kicad-cli sch export bom -o "$OUT/$PROJECT-bom.csv" \
    --fields 'Reference,Value,Footprint,${QUANTITY},Manufacturer,Manufacturer Part,Supplier Part' \
    --labels 'Designator,Value,Footprint,Qty,Manufacturer,MPN,LCSC' \
    --group-by 'Value,Footprint,Supplier Part' --ref-range-delimiter '' \
    "$PROJECT.kicad_sch"

echo "== Gerbers and drill files"
kicad-cli pcb export gerbers -o "$OUT/gerbers/" --subtract-soldermask \
    -l F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts \
    "$PROJECT.kicad_pcb"
kicad-cli pcb export drill -o "$OUT/gerbers/" --excellon-separate-th --generate-map --map-format gerberx2 \
    "$PROJECT.kicad_pcb"
(cd "$OUT/gerbers" && python3 -m zipfile -c "../$PROJECT-gerbers.zip" ./*)

echo "== placement"
kicad-cli pcb export pos -o "$OUT/$PROJECT-pos.csv" --format csv --units mm --side both --exclude-dnp \
    "$PROJECT.kicad_pcb"

echo "== done: $(pwd)/$OUT"
ls -1 "$OUT"
