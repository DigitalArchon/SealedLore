#!/usr/bin/env bash
# Smoke-test the built AppImage on the real display: open a story, type a
# turn, close cleanly. Needs the fake servers (see README.md here) and xdotool.
#
#   tools/smoke/appimage.sh DATA_DIR STORY_ID [APPIMAGE]
#
# The app's window is found through the launcher's *process tree*, never by
# title: an editor with the project open has "SealedLore" in its title too, and
# a title match once closed the author's VSCodium. Typing uses real key events
# (XTEST); `xdotool type --window` sends synthetic events Qt ignores.
set -euo pipefail
DATA="$1"; STORY="$2"; APP="${3:-$(ls -t dist/SealedLore-*-x86_64.AppImage | head -1)}"
export DISPLAY="${DISPLAY:-:0}"
nodes() { python3 -c "import json;print(len(json.load(open('$DATA/stories/$STORY/nodes.json'))))"; }
BEFORE="$(nodes)"

"$APP" --appimage-extract-and-run --data-dir "$DATA" --story "$STORY" & PID=$!
# Whatever happens, nothing it started outlives the script: the launcher's
# child (the app's Python) survived a killed launcher and held the terminal's
# pipe open.
tree() { pstree -p "$PID" 2>/dev/null | grep -o '([0-9]*)' | tr -d '()' || true; }
# The whole tree, gathered before anything is killed: once the launcher dies
# its child is re-parented and pstree can't find it.
cleanup() { for p in $(tree); do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT
WID=""
for _ in $(seq 1 60); do
    sleep 1
    for p in $(tree); do
        # A search that finds nothing fails; under pipefail that ended the
        # script, silently, before the window was up.
        WID="$(xdotool search --onlyvisible --pid "$p" 2>/dev/null | head -1 || true)"
        [ -n "$WID" ] && break
    done
    [ -n "$WID" ] && break
done
NAME="$(xdotool getwindowname "$WID" 2>/dev/null || true)"
case "$NAME" in *SealedLore*) echo "window: $NAME" ;; *) echo "no app window" >&2; exit 1 ;; esac

sleep 2
# No --sync: after a reboot it waited for good for a focus confirmation
# (2026-09-24); the check below says whether activation took. It sometimes
# doesn't the first time, so it gets three tries.
for _ in 1 2 3; do
    timeout 10 xdotool windowactivate "$WID" || true; sleep 1
    [ "$(xdotool getactivewindow)" = "$WID" ] && break
done
[ "$(xdotool getactivewindow)" = "$WID" ] || { echo "could not activate the app window: $(xdotool getactivewindow getwindowname 2>&1)" >&2; exit 1; }
eval "$(xdotool getwindowgeometry --shell "$WID")"
# The composer sits in the lower part of the centre column.
xdotool mousemove $((X + WIDTH * 30 / 100)) $((Y + HEIGHT * 83 / 100)) click 1; sleep 0.5
xdotool type --delay 12 "I walk out into the street and squint at the sun."; sleep 0.3
xdotool key Return
for _ in $(seq 1 60); do sleep 1; [ "$(nodes)" -ge $((BEFORE + 2)) ] && break; done
echo "nodes before $BEFORE, after $(nodes)"

wmctrl -i -c "$WID"
for _ in $(seq 1 30); do sleep 1; kill -0 "$PID" 2>/dev/null || break; done
wait "$PID" && echo "closed cleanly"
