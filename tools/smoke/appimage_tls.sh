#!/usr/bin/env bash
# Check the built AppImage's OpenSSL can find certificate authorities through
# its defaults, as Sigstore's trust-root update (Private Mode attestation)
# needs. Offline; no display.
#
#   tools/smoke/appimage_tls.sh [APPIMAGE]
#
# The Python base's OpenSSL 1.1 looks for /etc/pki/tls/cert.pem, missing on
# Debian and Ubuntu; AppRun sets SSL_CERT_FILE. 0.9.16's first build lacked
# that and every private/ model failed to attest ("Failed to refresh TUF
# metadata") while every test passed: the dev venv's OpenSSL is the system's.
set -euo pipefail
APP="$(readlink -f "${1:-$(ls -t dist/SealedLore-*-x86_64.AppImage | head -1)}")"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
cd "$WORK" && "$APP" --appimage-extract >/dev/null
ROOT="$WORK/squashfs-root"
# What AppRun exports, without starting the app: run it with its final exec
# swapped for printing the variable.
CERTS="$(sed 's/^exec .*/echo "$SSL_CERT_FILE"; exit 0/' "$ROOT/AppRun" | env -i PATH=/usr/bin:/bin HOME="$HOME" sh -s -- --version)"
[ -r "$CERTS" ] || { echo "AppRun set no readable SSL_CERT_FILE (got '$CERTS')" >&2; exit 1; }
env -i SSL_CERT_FILE="$CERTS" "$ROOT/opt/python3.12/bin/python3.12" -s -c '
import ssl, sys
count = ssl.create_default_context().cert_store_stats()["x509_ca"]
print(f"{ssl.OPENSSL_VERSION}: {count} CAs from {sys.argv[1]}")
sys.exit(0 if count else 1)
' "$CERTS"
