"""Fail before effects on platforms without the required file safety primitives."""
import sys


def require_supported_platform() -> None:
    # Git Bash changes the shell, not the Python interpreter's filesystem APIs.
    # Do not substitute chmod or unlocked files for Windows ACLs and locks.
    if sys.platform == "win32":
        from .hosted import HostedError
        raise HostedError(
            "UNSUPPORTED_PLATFORM",
            "Native Windows Python is not supported yet. Run your agent and Slashbooks "
            "inside WSL with Linux Python, or use macOS or Linux. Git Bash alone is not enough. "
            "No sign-in was started and no books were changed.",
        )
