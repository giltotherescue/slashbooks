#!/usr/bin/env python3
"""Sync, check, and build the standalone hosted Slashbooks plugin."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_REL = PurePosixPath("plugins/slashbooks-cloud")
CANONICAL_REL = PurePosixPath("src/bookkeeping/procedures")
GUIDANCE_PATH = "references/agent-guidance.json"
MCP_GUIDANCE_PROCEDURES = ("hard-rules", "communication")
MAX_MCP_GUIDANCE_BYTES = 4096
PROCEDURES_BY_SKILL = {
    "books": (
        "communication",
        "hard-rules",
        "month-end-check",
        "non-blocking",
        "onboarding-profile",
        "question-rules",
    ),
    "get-started": (
        "communication",
        "hard-rules",
        "onboarding-profile",
    ),
}
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


class PackagingError(RuntimeError):
    pass


def _run(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip()
        raise PackagingError(detail or f"git {' '.join(args)} failed") from exc


def _procedure_module(repo: Path) -> Any:
    path = (repo / "src/bookkeeping/procedures.py").resolve()
    spec = importlib.util.spec_from_file_location("slashbooks_packaging_procedures", path)
    if spec is None or spec.loader is None:
        raise PackagingError(f"cannot load canonical procedure parser: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expected_reference_paths() -> dict[str, str]:
    return {
        f"skills/{skill}/references/procedures/{name}.md": name
        for skill, names in PROCEDURES_BY_SKILL.items()
        for name in names
    }


def _parse_procedure_bytes(parser: Any, name: str, data: bytes) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"{name}.md"
        path.write_bytes(data)
        try:
            return parser._parse(path)
        except (OSError, ValueError) as exc:
            raise PackagingError(f"invalid canonical procedure {name}: {exc}") from exc


def _guidance_bytes(parser: Any, canonical: dict[str, bytes]) -> bytes:
    parsed = {
        name: _parse_procedure_bytes(parser, name, data)
        for name, data in canonical.items()
    }
    mcp_sources = [parsed[name] for name in MCP_GUIDANCE_PROCEDURES]
    mcp_text = "\n\n".join(
        f"{procedure['title']} (procedure {procedure['name']}, version {procedure['version']}):\n"
        + "\n".join(f"- {rule}" for rule in procedure["rules"])
        for procedure in mcp_sources
    )
    mcp_bytes = len(mcp_text.encode("utf-8"))
    if mcp_bytes > MAX_MCP_GUIDANCE_BYTES:
        raise PackagingError(
            f"MCP server guidance is {mcp_bytes} UTF-8 bytes; maximum is {MAX_MCP_GUIDANCE_BYTES}"
        )
    payload = {
        "schema_version": 1,
        "mcp_server_instructions": {
            "text": mcp_text,
            "sources": [
                {
                    "name": procedure["name"],
                    "version": procedure["version"],
                    "sha256": procedure["sha256"],
                }
                for procedure in mcp_sources
            ],
        },
    }
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _check_links(files: dict[str, bytes]) -> None:
    for skill, names in PROCEDURES_BY_SKILL.items():
        skill_path = f"skills/{skill}/SKILL.md"
        try:
            text = files[skill_path].decode("utf-8")
        except KeyError as exc:
            raise PackagingError(f"missing hosted skill: {skill_path}") from exc
        except UnicodeDecodeError as exc:
            raise PackagingError(f"hosted skill is not UTF-8: {skill_path}") from exc

        expected = {f"references/procedures/{name}.md" for name in names}
        links = set()
        for raw_target in LINK_RE.findall(text):
            target = raw_target.split("#", 1)[0]
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
                continue
            path = PurePosixPath(target)
            if path.is_absolute() or ".." in path.parts:
                raise PackagingError(f"hosted skill reference escapes its directory: {skill_path} -> {raw_target}")
            resolved = str(PurePosixPath("skills", skill, path))
            if resolved not in files:
                raise PackagingError(f"hosted skill reference is missing: {skill_path} -> {raw_target}")
            links.add(target)
        missing = sorted(expected - links)
        if missing:
            raise PackagingError(f"{skill_path} does not reference generated procedures: {', '.join(missing)}")


def _validate_package_files(files: dict[str, bytes], canonical: dict[str, bytes], parser: Any) -> None:
    expected = _expected_reference_paths()
    actual = {
        path for path in files
        if re.fullmatch(r"skills/[^/]+/references/procedures/[^/]+\.md", path)
    }
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        raise PackagingError(f"generated procedure set differs; missing={missing}, extra={extra}")
    for path, name in expected.items():
        if name not in canonical:
            raise PackagingError(f"missing canonical procedure: {name}")
        if files[path] != canonical[name]:
            raise PackagingError(f"stale generated procedure: {path}")
    if files.get(GUIDANCE_PATH) != _guidance_bytes(parser, canonical):
        raise PackagingError(f"stale generated guidance: {GUIDANCE_PATH}")
    _check_links(files)


def _working_package_files(repo: Path) -> dict[str, bytes]:
    root = repo / PACKAGE_REL
    if not root.is_dir():
        raise PackagingError(f"missing hosted plugin package: {root}")
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PackagingError(f"hosted plugin contains a symlink: {path.relative_to(root)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PackagingError(f"hosted plugin contains a non-file: {path.relative_to(root)}")
        if path.stat().st_mode & 0o111:
            raise PackagingError(f"hosted plugin contains an executable: {path.relative_to(root)}")
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _working_canonical(repo: Path) -> dict[str, bytes]:
    parser = _procedure_module(repo)
    canonical: dict[str, bytes] = {}
    for names in PROCEDURES_BY_SKILL.values():
        for name in names:
            path = repo / CANONICAL_REL / f"{name}.md"
            if name in canonical:
                continue
            try:
                parser._parse(path)
                canonical[name] = path.read_bytes()
            except (OSError, ValueError) as exc:
                raise PackagingError(f"invalid canonical procedure {name}: {exc}") from exc
    return canonical


def _preflight_sync_paths(repo: Path) -> None:
    package = repo / PACKAGE_REL
    if not package.is_dir() or package.is_symlink():
        raise PackagingError(f"missing or unsafe hosted plugin package: {package}")
    relatives = [PurePosixPath(path) for path in _expected_reference_paths()]
    relatives.append(PurePosixPath(GUIDANCE_PATH))
    for relative in relatives:
        current = repo
        for part in (PACKAGE_REL / relative).parts:
            current = current / part
            if current.is_symlink():
                raise PackagingError(
                    f"refusing to write through hosted plugin symlink: {current.relative_to(repo)}"
                )


def sync(repo: Path = ROOT) -> None:
    repo = repo.resolve()
    _preflight_sync_paths(repo)
    canonical = _working_canonical(repo)
    guidance_data = _guidance_bytes(_procedure_module(repo), canonical)
    expected = _expected_reference_paths()
    package = repo / PACKAGE_REL
    for relative, name in expected.items():
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(canonical[name])
    guidance = package / GUIDANCE_PATH
    guidance.parent.mkdir(parents=True, exist_ok=True)
    guidance.write_bytes(guidance_data)
    for skill in PROCEDURES_BY_SKILL:
        generated = package / "skills" / skill / "references" / "procedures"
        for path in generated.iterdir():
            relative = path.relative_to(package).as_posix()
            if relative not in expected:
                if path.is_dir() and not path.is_symlink():
                    raise PackagingError(f"refusing unexpected generated directory: {relative}")
                path.unlink()


def check(repo: Path = ROOT) -> None:
    repo = repo.resolve()
    files = _working_package_files(repo)
    canonical = _working_canonical(repo)
    parser = _procedure_module(repo)
    _validate_package_files(files, canonical, parser)
    for path, name in _expected_reference_paths().items():
        generated = parser._parse(repo / PACKAGE_REL / path)
        source = parser.get_procedure(name)
        if generated != source:
            raise PackagingError(f"generated procedure meaning differs: {path}")


def _committed_files(repo: Path, commit: str) -> dict[str, bytes]:
    prefix = PACKAGE_REL.as_posix() + "/"
    tree = _run(repo, "ls-tree", "-r", "-z", commit, "--", PACKAGE_REL.as_posix())
    files: dict[str, bytes] = {}
    for record in tree.rstrip(b"\0").split(b"\0") if tree else []:
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, object_id = metadata.split(b" ")
        path = raw_path.decode("utf-8")
        if not path.startswith(prefix):
            raise PackagingError(f"archive path escaped plugin root: {path}")
        relative = path.removeprefix(prefix)
        if mode != b"100644" or kind != b"blob":
            raise PackagingError(f"refusing tracked mode/type {mode.decode()} {kind.decode()}: {relative}")
        files[relative] = _run(repo, "cat-file", "blob", object_id.decode())
    if not files:
        raise PackagingError(f"commit has no hosted plugin package: {commit}")
    return files


def _committed_canonical(repo: Path, commit: str) -> dict[str, bytes]:
    canonical: dict[str, bytes] = {}
    for name in sorted({name for names in PROCEDURES_BY_SKILL.values() for name in names}):
        path = f"{CANONICAL_REL.as_posix()}/{name}.md"
        try:
            canonical[name] = _run(repo, "show", f"{commit}:{path}")
        except PackagingError as exc:
            raise PackagingError(f"commit is missing canonical procedure {name}: {commit}") from exc
    return canonical


def build(repo: Path, commit: str, output: Path) -> None:
    repo = repo.resolve()
    resolved = _run(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    files = _committed_files(repo, resolved)
    _validate_package_files(files, _committed_canonical(repo, resolved), _procedure_module(repo))
    epoch = int(_run(repo, "show", "-s", "--format=%ct", resolved).decode().strip())
    stamp = datetime.fromtimestamp(max(epoch, 315532800), timezone.utc).timetuple()[:6]
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise PackagingError(f"output already exists: {output}")
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    try:
        with ZipFile(temporary, "w") as package:
            for name, data in sorted(files.items()):
                info = ZipInfo(name, stamp)
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = ZIP_DEFLATED
                package.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=9)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync", help="copy canonical procedures into hosted skill references")
    commands.add_parser("check", help="fail when hosted references or package paths drift")
    build_parser = commands.add_parser("build", help="build a deterministic ZIP from one commit")
    build_parser.add_argument("--commit", required=True)
    build_parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "sync":
            sync(args.repo)
        elif args.command == "check":
            check(args.repo)
        else:
            build(args.repo, args.commit, args.output)
    except PackagingError as exc:
        parser.exit(1, f"hosted plugin: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
