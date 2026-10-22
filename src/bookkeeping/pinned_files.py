"""No-follow migration traversal: POSIX dir_fd or pinned Windows directory handles."""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
from .file_safety import windows, sync_directory


@dataclass
class WindowsDirectory:
    path: Path
    fd: int


def _name(name):
    if name in ('', '.', '..') or '/' in name or (windows() and (any(c in name for c in '\\:') or name.rstrip(' .') != name)):
        raise ValueError('Expected a single file name.')
    return name


def info(directory):
    return os.fstat(directory.fd if isinstance(directory, WindowsDirectory) else directory)


@contextmanager
def directory(path):
    path = Path(path)
    if '..' in path.parts:
        raise ValueError('Unsafe directory path.')
    absolute = Path(os.path.abspath(path))
    with ExitStack() as stack:
        if windows():
            from .windows_files import open_directory
            current = Path(absolute.anchor)
            fd = open_directory(current)
            stack.callback(os.close, fd)
            for part in absolute.parts[1:]:
                current = current / _name(part)
                fd = open_directory(current)
                stack.callback(os.close, fd)
            yield WindowsDirectory(current, fd), absolute
        else:
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, fd)
            for part in absolute.parts[1:]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, fd)
            yield fd, absolute


@contextmanager
def child(directory, name):
    name = _name(name)
    if isinstance(directory, WindowsDirectory):
        from .windows_files import open_directory
        path = directory.path / name
        fd = open_directory(path)
        result = WindowsDirectory(path, fd)
    else:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        result = fd
    try:
        yield result
    finally:
        os.close(fd)


def names(directory):
    return os.listdir(directory.path if isinstance(directory, WindowsDirectory) else directory)


def stat_child(directory, name):
    name = _name(name)
    if isinstance(directory, WindowsDirectory):
        result = (directory.path / name).lstat()
        if getattr(result, 'st_file_attributes', 0) & 0x400:
            raise PermissionError('Reparse points are not supported for migration.')
        return result
    return os.stat(name, dir_fd=directory, follow_symlinks=False)


def open_child(directory, name, *, create=False):
    name = _name(name)
    if isinstance(directory, WindowsDirectory):
        from .windows_files import open_file
        return open_file(directory.path / name, write=create, create=create, exclusive=create, private=create)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY | os.O_NONBLOCK)
    return os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=directory)


def unlink_child(directory, name):
    name = _name(name)
    if isinstance(directory, WindowsDirectory):
        (directory.path / name).unlink()
    else:
        os.unlink(name, dir_fd=directory)


def sync(directory):
    if isinstance(directory, WindowsDirectory):
        sync_directory(directory.path)
    else:
        os.fsync(directory)
