"""Platform-specific file locks and private-file permissions behind one interface."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import sys
import uuid


def windows() -> bool:
    return sys.platform == 'win32'


def is_private(fd: int) -> bool:
    if windows():
        from . import windows_files
        return windows_files.is_private(fd)
    info = os.fstat(fd)
    return info.st_uid == os.getuid() and not info.st_mode & 0o077


def open_private(path: Path, *, create=False, exclusive=False) -> int:
    if windows():
        from . import windows_files
        return windows_files.open_file(path, write=create, create=create, exclusive=exclusive, private=True)
    return os.open(path, (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY)
                   | (os.O_EXCL if exclusive else 0) | getattr(os, 'O_NOFOLLOW', 0)
                   | getattr(os, 'O_NONBLOCK', 0), 0o600)


def open_read(path: Path) -> int:
    if windows():
        from . import windows_files
        return windows_files.open_file(path)
    return os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))


@contextmanager
def locked(fd: int, *, shared=False):
    if windows():
        from . import windows_files
        with windows_files.locked(fd, shared=shared):
            yield
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def private_lock(path: Path):
    from .hosted import HostedError
    try:
        fd = open_private(path, create=True)
    except OSError as exc:
        raise HostedError('INSECURE_FILE', 'Cannot open private lock. Check folder access and file permissions.') from exc
    with os.fdopen(fd, 'r+') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or not is_private(fd):
            raise HostedError('INSECURE_FILE', 'Lock file must be accessible only to the current user.')
        with locked(fd):
            yield


def sync_directory(path: Path):
    # Windows does not expose POSIX directory fsync. Files are flushed before
    # publication; private files additionally use MoveFileEx(WRITE_THROUGH).
    if windows():
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace(source: Path, target: Path, *, exclusive=False):
    if windows():
        from . import windows_files
        windows_files.replace(source, target, exclusive=exclusive)
    elif exclusive:
        os.link(source, target)
    else:
        os.replace(source, target)


def private_temp(parent: Path) -> tuple[int, Path]:
    if windows():
        path = parent / ('.hosted-' + uuid.uuid4().hex)
        return open_private(path, create=True, exclusive=True), path
    import tempfile
    fd, path = tempfile.mkstemp(prefix='.hosted-', dir=parent)
    return fd, Path(path)


def reject_links(path: Path):
    if sys.platform == 'darwin':
        for alias in (Path('/tmp'), Path('/var')):
            if path.is_relative_to(alias):
                path = alias.resolve() / path.relative_to(alias)
                break
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PermissionError('Symbolic links and junctions are not supported for protected files.')


def check_private_storage():
    """Verify native ACL/locking support before asking for browser approval."""
    if not windows():
        return
    from .hosted import HostedError
    temporary = None
    try:
        root = Path.home() / '.config/slashbooks/agents'
        reject_links(root)
        root.mkdir(parents=True, exist_ok=True)
        fd, temporary = private_temp(root)
        with os.fdopen(fd, 'w') as stream:
            with locked(stream.fileno()):
                if not is_private(stream.fileno()):
                    raise PermissionError('Private storage check failed.')
    except OSError as exc:
        raise HostedError('PRIVATE_STORAGE_UNAVAILABLE',
                          'Sign-in needs a folder that supports private Windows permissions. '
                          'Check access to your user profile on a local NTFS drive. No sign-in was started.') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
