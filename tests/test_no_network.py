"""The suite's own guard against tests that use the network (conftest's
`_no_network`): it must refuse, and remember, any connection or name lookup
past this computer, and let a test's own local servers be."""

from __future__ import annotations

import socket
import threading

import httpx
import pytest

from tests import conftest


def _forget_what_was_tried() -> list[str]:
    tried, conftest._REACHED[:] = list(conftest._REACHED), []
    return tried


def test_a_connection_past_this_computer_is_refused_and_remembered():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(conftest.NetworkInTest, match="tests never use the network"):
            sock.connect(("216.150.1.1", 443))
    assert _forget_what_was_tried() == ["('216.150.1.1', 443)"]


def test_a_name_lookup_is_refused_before_any_query_goes():
    with pytest.raises(conftest.NetworkInTest):
        socket.getaddrinfo("nano-gpt.com", 443)
    assert _forget_what_was_tried() == ["the name 'nano-gpt.com'"]


def test_an_attempt_caught_by_the_code_under_test_is_still_seen():
    """httpx turns the refusal into a ConnectError, which code may catch and
    go on from: the fixture fails the test anyway."""
    with httpx.Client() as client, pytest.raises(httpx.ConnectError):
        client.get("https://198.51.100.7/")
    # httpx looks the address up first, so the lookup is what is refused.
    assert _forget_what_was_tried() == ["the name '198.51.100.7'"]


def test_a_local_server_is_reachable():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    accepted = threading.Thread(target=lambda: server.accept()[0].close())
    accepted.start()
    with socket.create_connection(server.getsockname(), timeout=5):
        pass
    accepted.join(5)
    server.close()
    assert socket.getaddrinfo("localhost", 80)
    assert not conftest._REACHED
