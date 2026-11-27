#!/usr/bin/env bash
# Imports each <work>/imp/<name>/<name>.epro into KiCad schematics by driving
# eeschema's "File > Import > Non-KiCad Schematic" on a virtual X display.
# kicad-cli cannot import EasyEDA schematics, so this is the only scripted way.
#
# usage: import_schematics.sh <work dir>      needs: Xvfb, xdotool, eeschema
set -euo pipefail
WORK="$(realpath "$1")"
DISP=:77
export DISPLAY=$DISP GDK_BACKEND=x11
unset WAYLAND_DISPLAY  # GTK would otherwise open the window on the real desktop
Xvfb $DISP -screen 0 1920x1080x24 >/dev/null 2>&1 &
XVFB=$!
trap 'kill $XVFB 2>/dev/null || true' EXIT
sleep 1

# a throw-away KiCad configuration so the first-run wizard does not show up
FAKEHOME="$WORK/kicad-home"
mkdir -p "$FAKEHOME/.config"
[ -d "$FAKEHOME/.config/kicad" ] || cp -r "$HOME/.config/kicad" "$FAKEHOME/.config/" 2>/dev/null || true

window() { xdotool search --onlyvisible --name "Schematic Editor" 2>/dev/null | head -1; }

for dir in "$WORK"/imp/*/; do
  name=$(basename "$dir")
  [ -f "$dir/$name.kicad_sch" ] && continue
  echo "importing $name"
  (cd "$dir" && HOME="$FAKEHOME" setsid eeschema >/dev/null 2>&1 &)
  for _ in $(seq 1 30); do sleep 1; w=$(window); [ -n "$w" ] && break; done
  sleep 2
  xdotool windowmove "$w" 0 0 windowsize "$w" 1920 1080; sleep 1.5
  # File > Import > Non-KiCad Schematic...
  xdotool mousemove 21 13 click 1; sleep 1; xdotool mousemove 60 204 click 1; sleep 0.8
  xdotool key Right; sleep 0.8; xdotool key Return; sleep 2
  xdotool key ctrl+l; sleep 0.4; xdotool type --delay 3 "$dir$name.epro"; sleep 0.4; xdotool key Return
  for _ in $(seq 1 60); do
    sleep 2
    case "$(xdotool getwindowname "$w")" in *untitled*) ;; *) sleep 3; break ;; esac
  done
  xdotool windowfocus "$w"; xdotool key ctrl+s; sleep 3
  for p in $(pgrep -x eeschema); do kill "$p"; done; sleep 1
  [ -f "$dir/$name.kicad_sch" ] || { echo "import of $name failed"; exit 1; }
done
