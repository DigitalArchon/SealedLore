"""The data folder kept to its owner on Windows, as 0700 keeps it on Linux.

`os.chmod` on Windows only sets the read-only flag, so the folder gets an
explicit, protected DACL instead: full control for the user running the app
and for SYSTEM, inherited by everything in it, nothing inherited from above.
Under %LOCALAPPDATA% the profile's own ACL is close to that already; on a
second drive (`--data-dir D:\\...`) the folder would otherwise inherit read
access for every account on the machine ("Authenticated Users"), and the
folder holds the API key and every prompt sent.

Administrators are left out, as root is not kept out on Linux: they can take
ownership of anything, so an entry for them would promise nothing.

Imported only on Windows (`paths.ensure_data_home`). ctypes, no dependency.
"""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from pathlib import Path

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

TOKEN_QUERY = 0x0008
TOKEN_USER = 1  # the TOKEN_INFORMATION_CLASS value
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SDDL_REVISION_1 = 1
SYSTEM_SID = "S-1-5-18"

_PVOID = ctypes.c_void_p
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.LocalFree.argtypes = [_PVOID]
_kernel32.LocalFree.restype = _PVOID
_advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    _PVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_advapi32.ConvertSidToStringSidW.argtypes = [_PVOID, ctypes.POINTER(wintypes.LPWSTR)]
_advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_PVOID)]
_advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.POINTER(_PVOID),
    ctypes.POINTER(wintypes.ULONG),
]
_advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    _PVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.LPWSTR),
    ctypes.POINTER(wintypes.ULONG),
]
_advapi32.GetSecurityDescriptorDacl.argtypes = [
    _PVOID,
    ctypes.POINTER(wintypes.BOOL),
    ctypes.POINTER(_PVOID),
    ctypes.POINTER(wintypes.BOOL),
]
_advapi32.SetNamedSecurityInfoW.argtypes = [
    wintypes.LPWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    _PVOID,
    _PVOID,
    _PVOID,
    _PVOID,
]
_advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
_advapi32.GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    ctypes.POINTER(_PVOID),
    ctypes.POINTER(_PVOID),
    ctypes.POINTER(_PVOID),
    ctypes.POINTER(_PVOID),
    ctypes.POINTER(_PVOID),
]
_advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", _PVOID), ("Attributes", wintypes.DWORD)]


def _check(ok: int) -> None:
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def user_sid() -> str:
    """The SID of the user this process runs as, e.g. "S-1-5-21-…-1001"."""
    token = wintypes.HANDLE()
    _check(
        _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token))
    )
    try:
        size = wintypes.DWORD()
        _advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        _check(_advapi32.GetTokenInformation(token, TOKEN_USER, buffer, size, ctypes.byref(size)))
        return _sid_text(_SidAndAttributes.from_buffer(buffer).Sid)
    finally:
        _kernel32.CloseHandle(token)


def _sid_text(sid: int | None) -> str:
    text = wintypes.LPWSTR()
    _check(_advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)))
    try:
        return str(text.value)
    finally:
        _kernel32.LocalFree(ctypes.cast(text, _PVOID))


def full_sid(trustee: str) -> str:
    """A trustee as SDDL writes it, as its SID: SDDL abbreviates well-known
    accounts, SYSTEM as "SY" and also the machine's built-in Administrator,
    whose own SID comes back as "LA" (GitHub's Windows runners run as it)."""
    if trustee.startswith("S-"):
        return trustee
    sid = _PVOID()
    _check(_advapi32.ConvertStringSidToSidW(trustee, ctypes.byref(sid)))
    try:
        return _sid_text(sid.value)
    finally:
        _kernel32.LocalFree(sid)


def owner_only_sddl(sid: str) -> str:
    """Protected (P), full access (FA) for the user and SYSTEM (SY), inherited
    by files (OI) and folders (CI) inside."""
    return f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)"


def folder_sddl(folder: Path) -> str:
    """The folder's DACL as Windows writes it out, e.g. "D:PAI(A;OICI;FA;;;SY)…"."""
    descriptor = _PVOID()
    error = _advapi32.GetNamedSecurityInfoW(
        str(folder),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        None,
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    try:
        text = wintypes.LPWSTR()
        _check(
            _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, SDDL_REVISION_1, DACL_SECURITY_INFORMATION, ctypes.byref(text), None
            )
        )
        try:
            return str(text.value)
        finally:
            _kernel32.LocalFree(ctypes.cast(text, _PVOID))
    finally:
        _kernel32.LocalFree(descriptor)


def entries(path: Path) -> set[str]:
    """The DACL's entries, each "type;flags;rights;;;SID" with the SID in
    full."""
    found = set()
    for entry in re.findall(r"\(([^)]*)\)", folder_sddl(path)):
        *head, trustee = entry.split(";")
        found.add(";".join([*head, full_sid(trustee)]))
    return found


def is_owner_only(folder: Path, sid: str) -> bool:
    """Protected, and exactly the two entries `owner_only_sddl` sets.
    Windows adds its own flags ("AI") and abbreviates, so the entries are
    compared by SID, not the string."""
    sddl = folder_sddl(folder)
    if not sddl.startswith("D:") or "(" not in sddl:
        return False
    flags = sddl[2 : sddl.index("(")]
    return "P" in flags and entries(folder) == {
        f"A;OICI;FA;;;{sid}",
        f"A;OICI;FA;;;{SYSTEM_SID}",
    }


def restrict_to_owner(folder: Path) -> None:
    """Give `folder` the owner-only DACL. SetNamedSecurityInfo (not
    SetFileSecurity) carries it down to what the folder already holds."""
    sid = user_sid()
    if is_owner_only(folder, sid):
        return
    descriptor = _PVOID()
    _check(
        _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            owner_only_sddl(sid), SDDL_REVISION_1, ctypes.byref(descriptor), None
        )
    )
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        dacl = _PVOID()
        _check(
            _advapi32.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            )
        )
        error = _advapi32.SetNamedSecurityInfoW(
            str(folder),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        _kernel32.LocalFree(descriptor)
