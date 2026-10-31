#!/usr/bin/env bash
# Start the AppImage's own tor the way phone access does, and wait until its
# onion address is published. Needs the network (it joins the live Tor
# network: ~10 s, now and then minutes); no display. Run it by hand after a
# build that changed tor or the packaging.
#
#   tools/smoke/appimage_tor.sh [APPIMAGE]
#
# AppRun runs with its last line swapped for the check, so everything is as in
# the app: AppRun's environment, the AppImage's Python and QtNetwork,
# find_tor() choosing the bundled tor, TorService giving it only its own
# folder as its library path. It passes once the address is published, and
# then only if tor stops when asked and nothing of it is left running.
set -euo pipefail
APP="$(readlink -f "${1:-$(ls -t dist/SealedLore-*-x86_64.AppImage | head -1)}")"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
cd "$WORK" && "$APP" --appimage-extract >/dev/null
ROOT="$WORK/squashfs-root"
TOR="$ROOT/usr/lib/tor/tor"

cat >"$WORK/check.py" <<'CHECK'
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from sealedlore.gui.tor_service import TorService
from sealedlore.phone.torrc import find_tor, phone_paths

app = QCoreApplication([])
tor = find_tor()
if tor is None or str(tor) != os.environ.get("SEALEDLORE_TOR"):
    sys.exit(f"the app would run {tor}, not AppRun's {os.environ.get('SEALEDLORE_TOR')}")
service = TorService(tor, phone_paths(Path(sys.argv[1])), key=None)
start = time.monotonic()
service.changed.connect(
    lambda step, words: print(f"{time.monotonic() - start:6.1f} s  {step}: {words}", flush=True)
)
service.start()
while time.monotonic() - start < 300 and service.step not in ("ready", "failed"):
    app.processEvents()
    time.sleep(0.02)
reached = service.step
print(f"tor {service.version} says of its version: {service.version_status or 'nothing'}")
if reached != "ready":
    print("\n".join(list(service.log)[-15:]))
service.stop()
if service.running():
    sys.exit("tor still running after stop")
sys.exit(0 if reached == "ready" else f"tor reached {reached}, not ready")
CHECK

sed 's|^exec "$PYTHON" -s -m sealedlore.gui.app "$@"$|exec "$PYTHON" -s "$WORK_CHECK" "$@"|' \
    "$ROOT/AppRun" >"$ROOT/AppRun.check"
grep -q 'WORK_CHECK' "$ROOT/AppRun.check" || { echo "AppRun's last line has changed" >&2; exit 1; }
chmod +x "$ROOT/AppRun.check"
mkdir -p -m 700 "$WORK/home"
env -i PATH=/usr/bin:/bin HOME="$WORK/home" LANG=C.UTF-8 QT_QPA_PLATFORM=offscreen \
    ${XDG_RUNTIME_DIR:+XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"} WORK_CHECK="$WORK/check.py" \
    "$ROOT/AppRun.check" "$WORK/data"

left="$(ps -eo pid=,args= | awk -v tor="$TOR" '$2 == tor { print $1 }')"
if [ -n "$left" ]; then
    echo "the bundled tor is still running: $left" >&2
    exit 1
fi
echo "PASS: the AppImage's tor published an onion address and stopped"
