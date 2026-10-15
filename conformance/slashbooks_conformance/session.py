"""A sandbox session: one directory, one saved world, one `books` command."""

from __future__ import annotations

import json
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import shim, world


class Session:
    def __init__(self, faults: list[str] | None = None, root: Path | None = None) -> None:
        self._tmp = None
        if root is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="slashbooks-conformance-")
            root = Path(self._tmp.name)
        self.workdir = root
        self.state_path = root / "state.json"
        self.state_path.write_text(json.dumps(world.new_state(faults)), encoding="utf-8")
        self.agent_commands: list[str] = []
        script = root / "books"
        script.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(shim.__file__)))} "
            f"{shlex.quote(str(self.state_path))} \"$@\"\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        self.shim_command = str(script)

    def call(self, argv: list[str]) -> tuple[bool, str]:
        return shim.run_command(self.state_path, [str(a) for a in argv])

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
