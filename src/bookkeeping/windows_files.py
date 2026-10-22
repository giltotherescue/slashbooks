"""Native Windows file security and locks, using only the Python standard library.

Private files are created with a protected DACL granting only the current user
full access. Checks use the opened handle, not a second path lookup. Reparse
points are refused. No chmod-based approximation of Windows permissions.
"""
from contextlib import contextmanager
import ctypes as c
from ctypes import wintypes as w
import msvcrt
import os
from pathlib import Path

kernel = c.WinDLL('kernel32', use_last_error=True)
advapi = c.WinDLL('advapi32', use_last_error=True)
P = c.c_void_p
DWORD = w.DWORD


def _api(lib, name, args, result=w.BOOL):
    fn = getattr(lib, name)
    fn.argtypes = args
    fn.restype = result
    return fn


class SECURITY_ATTRIBUTES(c.Structure):
    _fields_ = [('length', DWORD), ('descriptor', P), ('inherit', w.BOOL)]


class OVERLAPPED(c.Structure):
    _fields_ = [('internal', c.c_size_t), ('internal_high', c.c_size_t),
                ('offset', DWORD), ('offset_high', DWORD), ('event', w.HANDLE)]


class ACL(c.Structure):
    _fields_ = [('revision', w.BYTE), ('reserved', w.BYTE), ('size', w.WORD),
                ('count', w.WORD), ('reserved2', w.WORD)]


class ACE_HEADER(c.Structure):
    _fields_ = [('type', w.BYTE), ('flags', w.BYTE), ('size', w.WORD)]


CreateFile = _api(kernel, 'CreateFileW', [w.LPCWSTR, DWORD, DWORD, P, DWORD, DWORD, w.HANDLE], w.HANDLE)
CloseHandle = _api(kernel, 'CloseHandle', [w.HANDLE])
LocalFree = _api(kernel, 'LocalFree', [P], P)
GetCurrentProcess = _api(kernel, 'GetCurrentProcess', [], w.HANDLE)
OpenProcessToken = _api(advapi, 'OpenProcessToken', [w.HANDLE, DWORD, c.POINTER(w.HANDLE)])
GetTokenInformation = _api(advapi, 'GetTokenInformation', [w.HANDLE, c.c_int, P, DWORD, c.POINTER(DWORD)])
ConvertSidToStringSid = _api(advapi, 'ConvertSidToStringSidW', [P, c.POINTER(P)])
ConvertSD = _api(advapi, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', [w.LPCWSTR, DWORD, c.POINTER(P), P])
GetSecurityInfo = _api(advapi, 'GetSecurityInfo', [w.HANDLE, c.c_int, DWORD, c.POINTER(P), P, c.POINTER(P), P, c.POINTER(P)], DWORD)
EqualSid = _api(advapi, 'EqualSid', [P, P])
GetAce = _api(advapi, 'GetAce', [P, DWORD, c.POINTER(P)])
GetFileInformation = _api(kernel, 'GetFileInformationByHandleEx', [w.HANDLE, c.c_int, P, DWORD])
LockFileEx = _api(kernel, 'LockFileEx', [w.HANDLE, DWORD, DWORD, DWORD, DWORD, c.POINTER(OVERLAPPED)])
UnlockFileEx = _api(kernel, 'UnlockFileEx', [w.HANDLE, DWORD, DWORD, DWORD, c.POINTER(OVERLAPPED)])
MoveFileEx = _api(kernel, 'MoveFileExW', [w.LPCWSTR, w.LPCWSTR, DWORD])
INVALID_HANDLE = c.c_void_p(-1).value


def _check(ok):
    if not ok:
        raise c.WinError(c.get_last_error())


@contextmanager
def _user_sid():
    token = w.HANDLE()
    _check(OpenProcessToken(GetCurrentProcess(), 8, c.byref(token)))  # TOKEN_QUERY
    try:
        size = DWORD()
        GetTokenInformation(token, 1, None, 0, c.byref(size))  # TokenUser
        if not size.value:
            raise c.WinError(c.get_last_error())
        buffer = c.create_string_buffer(size.value)
        _check(GetTokenInformation(token, 1, buffer, size, c.byref(size)))
        # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first member is PSID.
        yield c.cast(buffer, c.POINTER(P))[0]
    finally:
        CloseHandle(token)


@contextmanager
def _private_security():
    with _user_sid() as sid:
        text = P()
        _check(ConvertSidToStringSid(sid, c.byref(text)))
        try:
            user = c.wstring_at(text)
        finally:
            LocalFree(text)
    descriptor = P()
    _check(ConvertSD(f'O:{user}D:P(A;;FA;;;{user})', 1, c.byref(descriptor), None))
    try:
        yield SECURITY_ATTRIBUTES(c.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
    finally:
        LocalFree(descriptor)


def _reject_reparse(handle):
    # FileAttributeTagInfo = 9, two DWORDs: attributes and reparse tag.
    info = (DWORD * 2)()
    _check(GetFileInformation(handle, 9, info, c.sizeof(info)))
    if info[0] & 0x400:
        raise PermissionError('Reparse points are not supported for protected files.')


def is_private(fd: int) -> bool:
    """Accept exactly the current owner's full-access ACE, with no other grants."""
    handle = msvcrt.get_osfhandle(fd)
    _reject_reparse(handle)
    owner, dacl, descriptor = P(), P(), P()
    result = GetSecurityInfo(handle, 1, 5, c.byref(owner), None, c.byref(dacl), None, c.byref(descriptor))
    if result:
        raise c.WinError(result)
    try:
        if not owner or not dacl or c.cast(dacl, c.POINTER(ACL)).contents.count != 1:
            return False
        ace = P()
        _check(GetAce(dacl, 0, c.byref(ace)))
        header = c.cast(ace, c.POINTER(ACE_HEADER)).contents
        if header.type != 0 or header.flags != 0 or header.size < 20:
            return False
        mask = c.cast(ace.value + 4, c.POINTER(DWORD))[0]
        with _user_sid() as sid:
            return bool(mask == 0x1F01FF and EqualSid(owner, sid) and EqualSid(ace.value + 8, sid))
    finally:
        LocalFree(descriptor)


def open_file(path: Path, *, write=False, create=False, exclusive=False, private=False) -> int:
    """Create private files atomically; existing ACLs are validated, never repaired."""
    access = 0x80000000 | (0x40000000 if write else 0)  # GENERIC_READ / WRITE
    disposition = 1 if exclusive else 4 if create else 3  # CREATE_NEW / OPEN_ALWAYS / OPEN_EXISTING
    flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    # Do not share deletion: the file cannot be replaced while it is checked/read.
    with _private_security() as security:
        handle = CreateFile(str(path), access, 3, c.byref(security) if private else None,
                            disposition, flags, None)
    if handle == INVALID_HANDLE:
        raise c.WinError(c.get_last_error())
    try:
        _reject_reparse(handle)
        fd = msvcrt.open_osfhandle(handle, (os.O_RDWR if write else os.O_RDONLY) | os.O_BINARY)
    except BaseException:
        CloseHandle(handle)
        raise
    try:
        if private and not is_private(fd):
            raise PermissionError('Private file must be owned by and accessible only to the current user.')
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def locked(fd: int, *, shared=False):
    handle = msvcrt.get_osfhandle(fd)
    overlap = OVERLAPPED()
    # Synchronous handle: wait until acquired. A shared lock works on read-only handles.
    _check(LockFileEx(handle, 0 if shared else 2, 0, 1, 0, c.byref(overlap)))
    try:
        yield
    finally:
        _check(UnlockFileEx(handle, 0, 1, 0, c.byref(overlap)))


def replace(source: Path, target: Path, *, exclusive=False):
    # The caller flushed file contents before publication. WRITE_THROUGH waits
    # for the move; omitting REPLACE_EXISTING preserves exclusive-create semantics.
    _check(MoveFileEx(str(source), str(target), 8 | (0 if exclusive else 1)))


def open_directory(path: Path) -> int:
    # Keep directories pinned against rename/removal while traversing descendants.
    handle = CreateFile(str(path), 0x80000000, 3, None, 3, 0x02200000, None)
    if handle == INVALID_HANDLE:
        raise c.WinError(c.get_last_error())
    try:
        _reject_reparse(handle)
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        CloseHandle(handle)
        raise
