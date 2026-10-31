"""SealedLore's own tor for phone access: the control protocol (phone/tor_control.py),
the torrc and paths (phone/torrc.py), and the service that runs it
(gui/tor_service.py), against tests/data/fake_tor.py: nothing reaches the
network. The real tor was run through the same service by hand (bootstrap
to a published onion in 8 s from an empty data folder; SealedLoreDocs
design/phone.md)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest

from sealedlore.phone import tor_control
from sealedlore.phone.tor_control import ControlError, Reply, ReplyReader
from sealedlore.phone.torrc import (
    find_tor,
    onion_target,
    phone_paths,
    render_torrc,
    tor_environment,
)

FAKE_TOR = Path(__file__).parent / "data" / "fake_tor.py"


# --- the control protocol ------------------------------------------------------------


def test_replies_are_read_whole_as_bytes_come():
    reader = ReplyReader()
    data = (
        b"250-PROTOCOLINFO 1\r\n250-AUTH METHODS=COOKIE,SAFECOOKIE "
        b'COOKIEFILE="/a b/control_auth_cookie"\r\n250 OK\r\n'
        b'650 STATUS_CLIENT NOTICE BOOTSTRAP PROGRESS=45 TAG=x SUMMARY="Asking"\r\n'
    )
    replies = []
    for i in range(0, len(data), 7):
        replies += reader.feed(data[i : i + 7])
    assert [r.status for r in replies] == [250, 650]
    assert replies[0].lines[1].startswith("AUTH ")
    assert replies[1].event and tor_control.bootstrap_of(replies[1].text()).progress == 45
    assert tor_control.cookie_file(replies[0]) == "/a b/control_auth_cookie"


def test_a_data_block_is_read_to_its_dot():
    (reply,) = ReplyReader().feed(b"250+config-text=\r\nA 1\r\n..B\r\n.\r\n250 OK\r\n")
    assert reply.ok and reply.data == {"config-text": "A 1\n.B"}


def test_a_line_that_isnt_a_reply_is_refused():
    with pytest.raises(ControlError):
        ReplyReader().feed(b"HTTP/1.1 200 OK\r\n")


def test_safecookie_both_ways():
    cookie, client, server = os.urandom(32), os.urandom(32), os.urandom(32)
    message = cookie + client + server
    server_hash = hmac.new(tor_control.SERVER_TO_CONTROLLER, message, hashlib.sha256).hexdigest()
    challenge = Reply(250, [f"AUTHCHALLENGE SERVERHASH={server_hash} SERVERNONCE={server.hex()}"])
    command = tor_control.authenticate(cookie, client, challenge)
    expected = hmac.new(tor_control.CONTROLLER_TO_SERVER, message, hashlib.sha256).hexdigest()
    assert command == f"AUTHENTICATE {expected}"
    liar = Reply(250, [f"AUTHCHALLENGE SERVERHASH={'00' * 32} SERVERNONCE={server.hex()}"])
    with pytest.raises(ControlError, match="prove it is tor"):
        tor_control.authenticate(cookie, client, liar)


def test_no_safecookie_no_login():
    reply = Reply(250, ["PROTOCOLINFO 1", 'AUTH METHODS=NULL,COOKIE COOKIEFILE="/x"', "OK"])
    with pytest.raises(ControlError, match="SAFECOOKIE"):
        tor_control.cookie_file(reply)


def test_the_onion_service_command_and_answer():
    command = tor_control.add_onion(None, "unix:/run/x/phone.sock")
    assert command.startswith("ADD_ONION NEW:ED25519-V3 ")
    for part in ("MaxStreams=16", "PoWDefensesEnabled=1", "Port=80,unix:/run/x/phone.sock"):
        assert part in command
    assert "Detach" not in command
    assert tor_control.add_onion("ED25519-V3:abc", "unix:/s").startswith(
        "ADD_ONION ED25519-V3:abc "
    )
    service = "a" * 56
    onion = tor_control.onion_of(
        Reply(250, [f"ServiceID={service}", "PrivateKey=ED25519-V3:key", "OK"])
    )
    assert (onion.service_id, onion.private_key) == (service, "ED25519-V3:key")
    with pytest.raises(ControlError):
        tor_control.onion_of(Reply(512, ["Bad arguments"]))
    uploaded = Reply(650, [f"HS_DESC UPLOADED {service} UNKNOWN $AA~relay"])
    assert tor_control.descriptor_uploaded(uploaded, service)
    assert not tor_control.descriptor_uploaded(uploaded, "b" * 56)


# --- the torrc and the paths -----------------------------------------------------------


def test_the_torrc_quotes_paths_and_reads_nothing_of_the_system():
    paths = phone_paths(Path('/tmp/a "quoted" home'))
    text = render_torrc(paths, 4242)
    assert 'DataDirectory "/tmp/a \\"quoted\\" home/phone/tor"' in text
    assert "SocksPort 0" in text and "CookieAuthentication 1" in text
    assert "__OwningControllerProcess 4242" in text
    # A path with a quote or a space can't be an ADD_ONION target: the
    # sockets went to the runtime folder instead.
    assert " " not in str(paths.http) and '"' not in str(paths.http)
    assert onion_target(paths) == f"unix:{paths.http}"


def test_a_deep_data_folder_puts_the_sockets_in_the_runtime_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    shallow = phone_paths(Path("/home/me/.local/share/sealedlore"))
    assert shallow.sockets == Path("/home/me/.local/share/sealedlore/phone")
    deep = phone_paths(Path("/home/me/" + "x" * 120))
    assert deep.sockets.parent == tmp_path / "run"
    assert deep.data == Path("/home/me/" + "x" * 120) / "phone" / "tor"


def test_finding_tor(tmp_path, monkeypatch):
    bundled = tmp_path / "bundle" / "tor"
    bundled.parent.mkdir()
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    assert find_tor({"SEALEDLORE_TOR": str(bundled), "PATH": ""}) == bundled
    assert find_tor({"PATH": str(bundled.parent)}) == bundled
    assert find_tor({"PATH": str(tmp_path / "nothing")}) is None
    monkeypatch.setattr(sys, "platform", "win32")
    assert find_tor({"SEALEDLORE_TOR": str(bundled)}) is None


def test_tor_gets_a_clean_environment_and_its_own_libraries_only(tmp_path):
    tor = tmp_path / "tor"
    env = {
        "HOME": "/home/me",
        "PATH": "/usr/bin",
        "LD_LIBRARY_PATH": "/app/usr/lib/x11",
        "SECRET": "x",
    }
    assert tor_environment(tor, env) == {"HOME": "/home/me", "PATH": "/usr/bin"}
    for name in ("libevent-2.1.so.7", "libssl.so.3", "libcrypto.so.3"):
        (tmp_path / name).write_text("")
    assert tor_environment(tor, env)["LD_LIBRARY_PATH"] == str(tmp_path)


# --- the service, against the fake tor ----------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui import tor_service as tor_service_module  # noqa: E402
from sealedlore.gui.tor_service import TorService  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def place(monkeypatch):
    """A short folder for the data and the sockets (107-byte socket paths)."""
    folder = Path(tempfile.mkdtemp(prefix="slt"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(folder / "run"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def fake_tor(folder: Path, mode: str = "ok") -> tuple[Path, Path]:
    """An executable standing in for tor, and the file it logs commands to.
    tor's environment is made clean, so the mode is written into the script."""
    log = folder / f"commands-{mode}.txt"
    script = folder / f"tor-{mode}"
    script.write_text(
        f"#!/bin/sh\nFAKE_TOR_MODE={mode} FAKE_TOR_LOG='{log}' "
        f"exec '{sys.executable}' -I '{FAKE_TOR}' \"$@\"\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


def wait_for(app: QApplication, condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.005)


def run(app, place, mode="ok", key=None):
    tor, log = fake_tor(place, mode)
    service = TorService(tor, phone_paths(place / "data"), key=key)
    seen = {"steps": [], "address": [], "keys": []}
    service.changed.connect(lambda step, _words: seen["steps"].append(step))
    service.address.connect(seen["address"].append)
    service.key_made.connect(lambda k, s: seen["keys"].append((k, s)))
    service.start()
    return service, seen, log


def commands(log: Path) -> list[str]:
    return [line.split()[0] for line in log.read_text().splitlines()]


def test_tor_is_started_logged_into_and_publishes_the_onion(app, place):
    service, seen, log = run(app, place)
    wait_for(app, lambda: service.step == "ready")
    assert seen["steps"][:2] == ["starting", "connecting"]
    assert "publishing" in seen["steps"] and service.progress == 100
    ((key, service_id),) = seen["keys"]
    assert key.startswith("ED25519-V3:") and seen["address"] == [service_id + ".onion"]
    assert commands(log) == [
        "PROTOCOLINFO",
        "AUTHCHALLENGE",
        "AUTHENTICATE",
        "TAKEOWNERSHIP",
        "SETEVENTS",
        "GETINFO",
        "GETINFO",
        "ADD_ONION",
    ]
    add_onion = log.read_text().splitlines()[-1]
    assert f"Port=80,unix:{service.paths.http}" in add_onion
    assert (service.paths.data.stat().st_mode & 0o777) == 0o700
    assert (service.paths.torrc.stat().st_mode & 0o777) == 0o600

    service.stop()
    assert service.step == "stopped" and not service.running()
    assert not service.paths.control.exists() or not _listening(service.paths.control)


@pytest.mark.parametrize("mode", ["obsolete", "unrecommended"])
def test_a_tor_that_says_it_is_out_of_date_is_said_to_be_and_goes_on(app, place, mode):
    service, seen, _log = run(app, place, mode)
    wait_for(app, lambda: service.step == "ready")
    assert service.outdated and service.version_status == mode
    assert "0.4.9.14" in service.warning and "out of date" in service.warning
    assert "still works" in service.warning
    assert seen["address"], "phone access isn't held back by it"
    service.stop()


@pytest.mark.parametrize("mode", ["ok", "new", "no_version", "new_warning"])
def test_a_tor_that_is_current_or_says_nothing_has_no_warning(app, place, mode):
    """Newer than the list is a release the list hasn't caught up with. No
    answer at all must never stop phone access."""
    service, _seen, _log = run(app, place, mode)
    wait_for(app, lambda: service.step == "ready")
    app.processEvents()
    assert not service.outdated and service.warning == ""
    service.stop()


def test_a_warning_later_in_the_run_is_heard_and_says_it_changed(app, place):
    tor, _log = fake_tor(place, "late_warning")
    service = TorService(tor, phone_paths(place / "data"), key=None)
    heard: list[bool] = []
    service.changed.connect(lambda *_: heard.append(service.outdated))
    service.start()
    wait_for(app, lambda: service.outdated)
    assert service.version_status == "obsolete"
    assert heard[-1] is True, "the window hears of it through `changed`"
    wait_for(app, lambda: service.step == "ready")
    service.stop()


def test_what_to_do_about_it_depends_on_whose_tor_it_is(app, place):
    from sealedlore.phone.torrc import BUNDLED_LIBRARIES, bundled

    bundle = place / "app" / "usr" / "lib" / "tor"
    bundle.mkdir(parents=True)
    (bundle / "tor").touch()
    assert not bundled(bundle / "tor"), "its libraries aren't beside it"
    for name in BUNDLED_LIBRARIES:
        (bundle / name).touch()
    assert bundled(bundle / "tor")
    assert not bundled(Path("/usr/bin/tor"))

    theirs = TorService(Path("/usr/bin/tor"), phone_paths(place / "data"), key=None)
    theirs.version_status = "obsolete"
    assert "Update the tor at /usr/bin/tor." in theirs.warning
    ours = TorService(bundle / "tor", phone_paths(place / "data"), key=None)
    ours.version_status = "obsolete"
    assert "A newer SealedLore brings a newer tor." in ours.warning


def test_what_tor_says_of_its_version_is_read(app):
    reader = ReplyReader()
    (info,) = reader.feed(b'250-PROTOCOLINFO 1\r\n250-VERSION Tor="0.4.9.15"\r\n250 OK\r\n')
    assert tor_control.version_of(info) == "0.4.9.15"
    (answer,) = reader.feed(b"250-status/version/current=new in series\r\n250 OK\r\n")
    assert tor_control.version_status(answer) == "new in series"
    (refused,) = reader.feed(b'552 Unrecognized key "status/version/current"\r\n')
    assert tor_control.version_status(refused) == ""
    said = 'STATUS_GENERAL WARN DANGEROUS_VERSION CURRENT="0.4.8.1" REASON=OBSOLETE RECOMMENDED="x"'
    assert tor_control.dangerous_version(said) == "obsolete"
    assert tor_control.dangerous_version("STATUS_GENERAL NOTICE CLOCK_JUMPED TIME=120") == ""


def _listening(path: Path) -> bool:
    import socket

    probe = socket.socket(socket.AF_UNIX)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def test_a_kept_key_keeps_the_address(app, place):
    service, seen, log = run(app, place, key="ED25519-V3:kept-key")
    wait_for(app, lambda: service.step == "ready")
    assert seen["keys"] == []
    assert "ADD_ONION ED25519-V3:kept-key " in log.read_text()
    first = seen["address"]
    service.stop()
    again, seen_again, _ = run(app, place, key="ED25519-V3:kept-key")
    wait_for(app, lambda: again.step == "ready")
    assert seen_again["address"] == first
    again.stop()


def test_a_tor_that_dies_is_started_again(app, place, monkeypatch):
    monkeypatch.setattr(tor_service_module, "RESTART_SECONDS", (0, 0, 0))
    service, seen, _ = run(app, place, "crash_once")
    wait_for(app, lambda: service.step == "ready")
    assert "restarting" in seen["steps"]
    service.stop()


def test_a_tor_that_keeps_dying_is_given_up_on_with_why(app, place, monkeypatch):
    monkeypatch.setattr(tor_service_module, "RESTART_SECONDS", (0, 0, 0))
    service, seen, _ = run(app, place, "crash")
    wait_for(app, lambda: service.step == "failed")
    assert seen["steps"].count("restarting") == 3
    assert "Tor stopped" in service.words and not service.running()
    assert any("fake" in line for line in service.log)


def test_something_that_isnt_tor_on_the_socket_is_never_logged_into(app, place, monkeypatch):
    monkeypatch.setattr(tor_service_module, "RESTART_SECONDS", ())
    service, _, log = run(app, place, "liar")
    wait_for(app, lambda: service.step == "failed")
    assert "didn't prove it is tor" in service.words
    assert "AUTHENTICATE" not in commands(log)


def test_a_refused_onion_is_said(app, place, monkeypatch):
    monkeypatch.setattr(tor_service_module, "RESTART_SECONDS", ())
    service, _, _ = run(app, place, "refuse")
    wait_for(app, lambda: service.step == "failed")
    assert "refused the onion service" in service.words


# --- the window ---------------------------------------------------------------------------

from sealedlore.gui import window_phone  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.storage.repository import save_config  # noqa: E402


def test_the_window_turns_phone_access_on_and_off(app, place, monkeypatch, memory_keychain):
    tor, _ = fake_tor(place)
    monkeypatch.setenv("SEALEDLORE_TOR", str(tor))
    root = place / "data"
    window = MainWindow(root=root, use_mock=True)
    changes = []
    window.phone_access_changed.connect(lambda: changes.append(window.phone_step))
    assert window.phone_step == "off"
    assert window.start_phone_access() is None
    wait_for(app, lambda: window.phone_step == "ready")
    assert window.phone_on and window._phone.onion.endswith(".onion")
    key = window.config.phone.onion.api_key
    assert key.startswith("ED25519-V3:")
    assert key not in (root / "config.json").read_text()  # in the keychain
    assert window._phone.onion == window.config.phone.service_id + ".onion"
    assert "ready" in changes

    window.stop_phone_access()
    assert window.phone_step == "off" and not window.phone_on
    paths = phone_paths(root)
    assert not paths.http.exists()  # nothing listens for phones
    assert not _listening(paths.control)  # and tor is gone
    window.close()


def test_a_lost_key_is_never_replaced_by_itself(app, place, monkeypatch):
    tor, _ = fake_tor(place)
    monkeypatch.setenv("SEALEDLORE_TOR", str(tor))
    window = MainWindow(root=place / "data", use_mock=True)
    window.config.keychain_slots = ["phone"]  # listed, but the keychain gave nothing back
    assert window.start_phone_access() == window_phone.KEY_MISSING
    assert window.phone_step == "off" and not window.phone_on
    window.close()


def test_no_tor_is_said(app, place, monkeypatch):
    monkeypatch.delenv("SEALEDLORE_TOR", raising=False)
    monkeypatch.setenv("PATH", str(place))
    window = MainWindow(root=place / "data", use_mock=True)
    assert window.start_phone_access() == window_phone.NO_TOR
    window.close()


def test_closing_the_window_ends_tor(app, place, monkeypatch, memory_keychain):
    tor, _ = fake_tor(place)
    monkeypatch.setenv("SEALEDLORE_TOR", str(tor))
    window = MainWindow(root=place / "data", use_mock=True)
    window.start_phone_access()
    wait_for(app, lambda: window.phone_step == "ready")
    service = window._tor
    window.close()
    assert window._tor is None and not service.running()
    save_config(window.config, root=place / "data")
    assert json.loads((place / "data" / "config.json").read_text())["phone"]["service_id"]
