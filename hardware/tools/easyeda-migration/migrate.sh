#!/usr/bin/env bash
# One-time migration EasyEDA Pro (.epro2) -> KiCad 10, as done on 2026-11-25.
#
# usage: migrate.sh <project.epro2> <work dir> <output dir>
#   output dir receives flipdot-driver/, flipdot-driver-rev1.2/ and panel-wiring/
#
# needs: KiCad 10 (kicad-cli, eeschema, pcbnew Python module), Xvfb, xdotool
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
EPRO2="$(realpath "$1")"; WORK="$(realpath -m "$2")"; OUT="$(realpath -m "$3")"
mkdir -p "$WORK" "$OUT"
cd "$WORK"

echo "== 1. .epro2 -> one V1 .epro per board"
python3 "$HERE/epro2_to_epro.py" "$EPRO2" epro
mkdir -p imp pcb
for f in epro/*.epro; do
  n=$(basename "$f" .epro)
  mkdir -p "imp/$n"; cp "$f" "imp/$n/"
  printf '(sym_lib_table\n\t(version 7)\n)\n' > "imp/$n/sym-lib-table"
  printf '(fp_lib_table\n\t(version 7)\n)\n' > "imp/$n/fp-lib-table"
done

echo "== 2. schematics through KiCad's importer"
"$HERE/import_schematics.sh" "$WORK"

echo "== 3. PCBs through KiCad's importer"
for n in Board-SMD Board-Rev-1.2; do python3 "$HERE/import_pcb.py" "imp/$n/$n.epro" "pcb/$n.kicad_pcb"; done

board() {  # board <project> <build flag> <imported name> <pcb or ->
  local P=$1 FLAG=$2 N=$3 PCB=$4
  rm -rf "proj/$P"
  python3 "$HERE/build_kicad.py" "$EPRO2" imp pcb "proj/$P" $FLAG
  cp "imp/$N/$N.kicad_pro" "proj/$P/$P.kicad_pro"
  kicad-cli sch export netlist "$WORK/proj/$P/$P.kicad_sch" -o "$WORK/$P.net" >/dev/null
  if [ "$PCB" != "-" ]; then
    python3 "$HERE/build_pcb.py" "pcb/$PCB" "proj/$P" "$P" "$P.net"
    python3 "$HERE/finish_project.py" "proj/$P" "$P"
    python3 "$HERE/rename_nets.py" "proj/$P/$P.kicad_pcb" "$P.net"
  else
    python3 - "proj/$P/$P.kicad_pro" "$P" <<'PY'
import json, sys
p = json.load(open(sys.argv[1])); p["meta"]["filename"] = sys.argv[2] + ".kicad_pro"; p["sheets"] = []
json.dump(p, open(sys.argv[1], "w"), indent=2)
PY
    printf '(sym_lib_table\n\t(version 7)\n\t(lib (name "flipdot")(type "KiCad")(uri "${KIPRJMOD}/flipdot.kicad_sym")(options "")(descr "Project symbols"))\n)\n' > "proj/$P/sym-lib-table"
  fi
}

echo "== 4. KiCad projects"
board flipdot-driver "" Board-SMD Board-SMD.kicad_pcb
board flipdot-driver-rev1.2 --rev12 Board-Rev-1.2 Board-Rev-1.2.kicad_pcb
board panel-wiring --panel generelle-ged-ns-zeichnung -

echo "== 5. copy and let KiCad normalize the files"
for P in flipdot-driver flipdot-driver-rev1.2 panel-wiring; do
  rm -rf "${OUT:?}/$P"; mkdir -p "$OUT/$P"
  (cd "proj/$P" && cp ./*.kicad_pro ./*.kicad_sch sym-lib-table flipdot.kicad_sym "$OUT/$P/" \
     && { [ ! -f fp-lib-table ] || cp fp-lib-table "$OUT/$P/"; } \
     && { ! ls ./*.kicad_pcb >/dev/null 2>&1 || cp ./*.kicad_pcb "$OUT/$P/"; } \
     && { [ ! -d flipdot.pretty ] || cp -r flipdot.pretty "$OUT/$P/"; })
  for f in "$OUT/$P"/*.kicad_sch; do kicad-cli sch upgrade --force "$f" >/dev/null; done
  for f in "$OUT/$P"/*.kicad_pcb; do [ ! -f "$f" ] || kicad-cli pcb upgrade --force "$f" >/dev/null; done
  kicad-cli sym upgrade --force "$OUT/$P/flipdot.kicad_sym" >/dev/null
  [ ! -d "$OUT/$P/flipdot.pretty" ] || kicad-cli fp upgrade --force "$OUT/$P/flipdot.pretty" >/dev/null
  rm -f "$OUT/$P"/*.kicad_prl "$OUT/$P"/*.lck
done
echo "done: $OUT"
