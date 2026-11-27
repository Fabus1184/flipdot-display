# EasyEDA Pro → KiCad migration

These scripts turned the EasyEDA Pro project into the KiCad projects in
[`../../kicad/`](../../kicad/). It was a one-time migration, done on
2026-11-25. The scripts are kept so it can be repeated: a re-run produces the
same netlists. Changes made in KiCad afterwards are not part of the
scripts: a company logo removed, revision 2.1 on the silkscreen and in the
title blocks, and consistent net and value names (`VMEGA` → `+HV`, `3.3V` →
`+3V3`, `BUSX`/`BUSY` → `HSD0`–`HSD15`, `SW`/`FB` → `BOOST_SW`/`BOOST_FB`,
panel lines `A1`/`B1`/`COM1` → `ROW0A`/`ROW0B`/`COL0`, resistor values like
`10k`).

The original `.epro2` is not in this repository; with a copy of it:

```bash
hardware/tools/easyeda-migration/migrate.sh <project.epro2> /tmp/flipdot-migration hardware/kicad
```

You need KiCad 10 with `kicad-cli`, `eeschema` and the `pcbnew` Python module,
plus `Xvfb` and `xdotool`. A run takes about two minutes.

## Why it is more than "File → Import"

KiCad 10 imports EasyEDA Pro projects only in the older `.epro` format. Current
EasyEDA Pro clients save `.epro2`, which holds one `.epru` file. Its records
are JSON objects instead of positional arrays. Schematic Y is flipped, styles
are inlined, and wires are split into child `LINE` records. See
[KiCad issue #22239](https://gitlab.com/kicad/code/kicad/-/issues/22239).

| Step | Script | What it does |
| --- | --- | --- |
| 1 | [`epro2_to_epro.py`](epro2_to_epro.py) | Rewrites `.epro2` as V1 `.epro`, one file per board, so KiCad does not ask which board to import. The field mapping was derived from documents present in both formats. It was checked against KiCad's parser source and against the November 2025 `.epro` backup, field by field, for every position KiCad reads. |
| 2 | [`import_schematics.sh`](import_schematics.sh) | `kicad-cli` cannot import foreign schematics. This script drives eeschema's *Import Non-KiCad Schematic* on a virtual X display. |
| 3 | [`import_pcb.py`](import_pcb.py) | The PCB import is headless, through the `pcbnew` module. |
| 4 | [`build_kicad.py`](build_kicad.py) | Rebuilds what the importer cannot do, listed below the table. |
| 5 | [`build_pcb.py`](build_pcb.py) | The PCB side, listed below the table. |
| 6 | [`finish_project.py`](finish_project.py), [`rename_nets.py`](rename_nets.py) | Writes the library tables, design rules and zone settings. Then renames the PCB nets to the schematic's names. That step refuses to run if the connectivity differs, apart from the two PCB8 erratum nets, which it merges into GND. |

**What `build_kicad.py` rebuilds:**

- EasyEDA's batch-reused `hss`/`lss` blocks become 32 KiCad hierarchical sheets.
  Each placement gets its own references, taken from EasyEDA's `INSTANCE`
  records.
- The bus lines and bus entries.
- Net labels and wires that the importer left a few hundredths of a millimetre
  off grid are snapped back onto it.
- Unused pins get no-connect flags.
- The ESP32 DevKit socket and the test pads get symbols on their own sheet.
- The EasyEDA drawing frames are replaced with KiCad title blocks.

**What `build_pcb.py` does:**

- Turns the EasyEDA free pads into the DevKit socket footprint and test points.
- Turns the corner cutouts into mounting holes.
- Links every footprint to its symbol.
- Exports the project footprint library.

`sexpr.py` is a small S-expression reader and writer. `esp_sheet.py` holds the
DevKit symbol and the pin-geometry helpers.
