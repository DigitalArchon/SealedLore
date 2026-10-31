"""A stand-in for tor, for tests/test_tor_service.py: run as tor would be
(`-f torrc --defaults-torrc file`), it reads the torrc, writes a cookie,
listens on the control socket and answers the commands phone access sends,
the way tor 0.4.9 does. Nothing goes on the network.

FAKE_TOR_MODE (environment):
  ok            bootstraps to 100% and publishes the onion
  crash         exits after bootstrapping, every time
  crash_once    exits after bootstrapping the first time (a file marks it)
  liar          a wrong SAFECOOKIE server hash: not tor
  refuse        refuses ADD_ONION
  obsolete, unrecommended, new
                what status/version/current says (else "recommended")
  no_version    refuses that question
  late_warning  recommended at first, then a DANGEROUS_VERSION event, as when a
                new consensus stops recommending it
  new_warning   a DANGEROUS_VERSION event saying it is newer than the list
FAKE_TOR_LOG: a file each command received is appended to.

Stops when its control connection closes (TAKEOWNERSHIP), as tor does.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import socket
import sys
import time

SERVER_TO_CONTROLLER = b"Tor safe cookie authentication server-to-controller hash"
CONTROLLER_TO_SERVER = b"Tor safe cookie authentication controller-to-server hash"


def unquote(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        out, i, inner = [], 0, value[1:-1]
        while i < len(inner):
            if inner[i] == "\\" and i + 1 < len(inner):
                i += 1
            out.append(inner[i])
            i += 1
        return "".join(out)
    return value


def read_torrc(path: str) -> dict[str, str]:
    settings = {}
    for line in open(path, encoding="utf-8"):
        key, _, value = line.strip().partition(" ")
        if key:
            settings[key] = value
    return settings


def main() -> None:
    args = sys.argv[1:]
    torrc = read_torrc(args[args.index("-f") + 1])
    mode = os.environ.get("FAKE_TOR_MODE", "ok")
    log = os.environ.get("FAKE_TOR_LOG")
    data = unquote(torrc["DataDirectory"])
    control = unquote(torrc["ControlPort"].removeprefix("unix:"))
    os.makedirs(data, exist_ok=True)
    cookie_path = os.path.join(data, "control_auth_cookie")
    cookie = secrets.token_bytes(32)
    with open(cookie_path, "wb") as f:
        f.write(cookie)
    marker = os.path.join(data, "crashed-once")
    print("Oct 19 00:00:00.000 [notice] Tor 0.4.9.14 (fake) starting", flush=True)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(control)
    server.listen(1)
    conn, _ = server.accept()
    files = conn.makefile("rwb")

    def send(*lines: str) -> None:
        files.write("".join(line + "\r\n" for line in lines).encode())
        files.flush()

    def event(line: str) -> None:
        send("650 " + line)

    client_nonce = server_nonce = b""
    service_id = ""
    while True:
        raw = files.readline()
        if not raw:
            return  # the controller went: tor exits
        line = raw.decode().rstrip("\r\n")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        command, _, rest = line.partition(" ")
        if command == "PROTOCOLINFO":
            send(
                "250-PROTOCOLINFO 1",
                f'250-AUTH METHODS=COOKIE,SAFECOOKIE COOKIEFILE="{cookie_path}"',
                '250-VERSION Tor="0.4.9.14"',
                "250 OK",
            )
        elif command == "AUTHCHALLENGE":
            client_nonce = bytes.fromhex(rest.split()[1])
            server_nonce = secrets.token_bytes(32)
            message = cookie + client_nonce + server_nonce
            server_hash = hmac.new(SERVER_TO_CONTROLLER, message, hashlib.sha256).digest()
            if mode == "liar":
                server_hash = bytes(32)
            send(
                f"250 AUTHCHALLENGE SERVERHASH={server_hash.hex()} SERVERNONCE={server_nonce.hex()}"
            )
        elif command == "AUTHENTICATE":
            message = cookie + client_nonce + server_nonce
            expected = hmac.new(CONTROLLER_TO_SERVER, message, hashlib.sha256).hexdigest()
            if rest.strip().lower() == expected:
                send("250 OK")
            else:
                send(
                    "515 Authentication failed: Safe cookie response did not match expected value."
                )
        elif command in ("TAKEOWNERSHIP", "SETEVENTS"):
            send("250 OK")
        elif command == "GETINFO" and rest == "status/version/current":
            if mode == "no_version":
                send('552 Unrecognized key "status/version/current"')
                continue
            status = mode if mode in ("obsolete", "unrecommended", "new") else "recommended"
            send(f"250-status/version/current={status}", "250 OK")
            reason = {"late_warning": "OBSOLETE", "new_warning": "NEW"}.get(mode)
            if reason:
                time.sleep(0.05)
                event(
                    f'STATUS_GENERAL WARN DANGEROUS_VERSION CURRENT="0.4.9.14" '
                    f'REASON={reason} RECOMMENDED="0.4.9.15, 0.4.9.16"'
                )
        elif command == "GETINFO":
            send(
                "250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS=50 TAG=loading_descriptors "
                'SUMMARY="Loading relay descriptors"',
                "250 OK",
            )
            for progress, summary in ((75, "Loaded enough directory info"), (100, "Done")):
                time.sleep(0.05)
                event(
                    f'STATUS_CLIENT NOTICE BOOTSTRAP PROGRESS={progress} TAG=x SUMMARY="{summary}"'
                )
            if mode == "crash" or (mode == "crash_once" and not os.path.exists(marker)):
                open(marker, "w").close()
                os._exit(1)
        elif command == "ADD_ONION":
            if mode == "refuse":
                send("512 Bad arguments to ADD_ONION")
                continue
            key = rest.split()[0]
            if key == "NEW:ED25519-V3":
                blob = base64.b64encode(secrets.token_bytes(64)).decode()
                private = f"ED25519-V3:{blob}"
            else:
                private = key
            digest = hashlib.sha256(private.encode()).digest() * 2
            service_id = base64.b32encode(digest).decode().lower()[:56]
            lines = [f"250-ServiceID={service_id}"]
            if key == "NEW:ED25519-V3":
                lines.append(f"250-PrivateKey={private}")
            send(*lines, "250 OK")
            time.sleep(0.05)
            event(f"HS_DESC UPLOAD {service_id} UNKNOWN $AAAA~relay xyz HSDIR_INDEX=abc")
            event(f"HS_DESC UPLOADED {service_id} UNKNOWN $AAAA~relay")
        else:
            send('510 Unrecognized command "' + command + '"')


if __name__ == "__main__":
    main()
