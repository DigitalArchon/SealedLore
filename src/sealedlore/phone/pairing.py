"""Pairing a phone, and the tokens paired phones carry.

The desktop shows a QR code for `http://<onion>/#pair=<code>`. The phone's
page posts the code with a name; the code is spent then, whatever the answer.
The desktop asks "Allow this phone? It shows 4821", the same four digits the
phone shows, so that a claim made from somewhere else with a photographed
code is told apart. On Allow the phone is given its token once; only the
token's hash is kept. Tor Browser forgets everything when it closes, so the
page then moves to `#k=<token>` and the author bookmarks that: the bookmark
is the login. The part after `#` never reaches a server or a Referer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

CODE_SECONDS = 5 * 60  # a pairing sheet's code lives this long
CLAIM_SECONDS = 5 * 60  # a claim waits this long for the desktop's answer
NAME_LENGTH = 40
# There is no limit on wrong codes, and none on claims: a code is 128 bits, so
# guessing gets nowhere, while a limit would let anyone with the onion address
# spend the author's code or keep the right one out by trying wrong ones
# first. A wrong code makes nothing; only the right one makes a claim, once.


class PairingRefused(Exception):
    def __init__(self, status: int, reason: str, *, wrong_code: bool = False) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.wrong_code = wrong_code  # someone tried a code that isn't the one shown


@dataclass
class Claim:
    id: str
    name: str
    check: str  # the four digits both screens show
    made_at: float
    state: str = "waiting"  # waiting, approved, refused, expired
    token: str = ""  # given once, on approval


def new_code() -> str:
    """128 bits, as 26 lower-case base32 letters and digits."""
    return base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def device_for(token: str, devices: Iterable[tuple[str, str]]) -> str | None:
    """The device whose token this is, from (device id, token hash) pairs.
    Every hash is compared, in constant time."""
    if not token:
        return None
    digest = token_hash(token)
    found = None
    for device_id, stored in devices:
        if hmac.compare_digest(digest, stored):
            found = device_id
    return found


def clean_name(name: object) -> str:
    """A phone's name as the desktop shows it: printable, one line, short."""
    text = "".join(
        " " if ch.isspace() else ch for ch in str(name or "") if ch.isprintable() or ch.isspace()
    )
    return " ".join(text.split())[:NAME_LENGTH] or "A phone"


class Pairing:
    """The pairing sheet's state: at most one live code, and the claims made
    with it. Clock injected for tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._code: str | None = None
        self._code_made = 0.0
        self._claims: dict[str, Claim] = {}

    # --- the desktop's side ------------------------------------------------

    def open(self) -> str:
        """A new code; any earlier one is spent."""
        self._code = new_code()
        self._code_made = self._clock()
        return self._code

    def close(self) -> None:
        """The sheet is closed: the code is spent and unanswered claims expire."""
        self._code = None
        for claim in self._claims.values():
            if claim.state == "waiting":
                claim.state = "expired"

    @property
    def code(self) -> str | None:
        self._expire()
        return self._code

    def waiting(self) -> list[Claim]:
        self._expire()
        return [c for c in self._claims.values() if c.state == "waiting"]

    def answer(self, claim_id: str, allow: bool) -> str | None:
        """The desktop's answer. Allowed, the new token (to keep its hash);
        refused, or a claim that's gone, None."""
        self._expire()
        claim = self._claims.get(claim_id)
        if claim is None or claim.state != "waiting":
            return None
        if not allow:
            claim.state = "refused"
            return None
        claim.state = "approved"
        claim.token = new_token()
        return claim.token

    # --- the phone's side --------------------------------------------------

    def claim(self, code: object, name: object) -> Claim:
        self._expire()
        now = self._clock()
        if self._code is None:
            raise PairingRefused(
                410, "Pairing isn't open on the desktop. Show the code there and scan it again."
            )
        # As bytes: compare_digest refuses a str with anything beyond ASCII in
        # it (TypeError, a 500).
        if not isinstance(code, str) or not hmac.compare_digest(
            code.strip().lower().encode("utf-8", "surrogatepass"), self._code.encode("ascii")
        ):
            raise PairingRefused(403, "That code isn't the one on the desktop.", wrong_code=True)
        self._code = None  # spent, whatever the desktop answers
        claim = Claim(
            id=secrets.token_urlsafe(16),
            name=clean_name(name),
            check=f"{secrets.randbelow(10000):04d}",
            made_at=now,
        )
        self._claims[claim.id] = claim
        return claim

    def poll(self, claim_id: str) -> tuple[str, str]:
        """(state, token): the token only once, on the first poll after
        approval; the claim is forgotten once it is answered and told."""
        self._expire()
        claim = self._claims.get(claim_id)
        if claim is None:
            return "expired", ""
        if claim.state == "waiting":
            return "waiting", ""
        del self._claims[claim_id]
        return claim.state, claim.token

    def _expire(self) -> None:
        now = self._clock()
        if self._code is not None and now - self._code_made > CODE_SECONDS:
            self._code = None
        for claim_id, claim in list(self._claims.items()):
            if now - claim.made_at > CLAIM_SECONDS:
                if claim.state == "waiting":
                    claim.state = "expired"
                # Long answered and never collected: nothing waits for it.
                if now - claim.made_at > 2 * CLAIM_SECONDS:
                    del self._claims[claim_id]
