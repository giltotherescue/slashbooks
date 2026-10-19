"""Support for the golden output tests: fixed clock, CLI runner, normalizer, company builders.

The golden tests prove that existing companies keep producing byte-identical
outputs while the engine changes underneath them. Everything here is synthetic
and deterministic: fixed dates, fixed ids, a frozen clock and no network.

Normalizer (the only edits made to captured text)
-------------------------------------------------
1. Temporary directory paths become ``<COMPANY>`` (the company directory),
   ``<WORK>`` (the scratch directory holding it) and ``<REPO>`` (the checkout).
   The temp directory differs on every run, so this is the only volatile text.
2. A readiness document's ``package_id`` is a hash of the whole document, and a
   blocked document contains the company path in its "Run: books ..." actions.
   The id is recomputed over the path-normalized document so it no longer depends
   on the temp directory. Ready documents contain no path and keep the same id.
3. Nothing else. Timestamps are not rewritten: the clock is frozen
   (``FROZEN_NOW``) and ids come from a counter, so a value that changes is a
   real change.
"""
from __future__ import annotations

import contextlib
import datetime as _datetime_module
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import date as _RealDate, datetime as _RealDatetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
GOLDEN_ROOT = Path(__file__).resolve().parent / "fixtures" / "golden"
QB_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "accrual" / "acceptance"

FROZEN_NOW = _RealDatetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
FROZEN_TODAY = FROZEN_NOW.date()
READINESS_AS_OF = "2026-08-05"


# ---------------------------------------------------------------------------
# Frozen clock
# ---------------------------------------------------------------------------

class _DateMeta(type):
    def __instancecheck__(cls, obj: Any) -> bool:
        return isinstance(obj, cls._real)

    def __subclasscheck__(cls, sub: Any) -> bool:
        return issubclass(sub, cls._real) or type.__subclasscheck__(cls, sub)


class _FrozenDate(_RealDate, metaclass=_DateMeta):
    _real = _RealDate

    def __new__(cls, *args: Any, **kwargs: Any) -> _RealDate:
        return _RealDate(*args, **kwargs)

    @classmethod
    def today(cls) -> _RealDate:
        return FROZEN_TODAY

    @classmethod
    def fromisoformat(cls, text: str) -> _RealDate:
        return _RealDate.fromisoformat(text)


class _FrozenDatetime(_RealDatetime, metaclass=_DateMeta):
    _real = _RealDatetime

    def __new__(cls, *args: Any, **kwargs: Any) -> _RealDatetime:
        return _RealDatetime(*args, **kwargs)

    @classmethod
    def now(cls, tz: Any = None) -> _RealDatetime:
        return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW.replace(tzinfo=None)

    @classmethod
    def utcnow(cls) -> _RealDatetime:
        return FROZEN_NOW.replace(tzinfo=None)

    @classmethod
    def today(cls) -> _RealDatetime:
        return FROZEN_NOW.replace(tzinfo=None)

    @classmethod
    def fromisoformat(cls, text: str) -> _RealDatetime:
        return _RealDatetime.fromisoformat(text)

    @classmethod
    def combine(cls, *args: Any, **kwargs: Any) -> _RealDatetime:
        return _RealDatetime.combine(*args, **kwargs)

    @classmethod
    def strptime(cls, *args: Any, **kwargs: Any) -> _RealDatetime:
        return _RealDatetime.strptime(*args, **kwargs)


_uuid_counter = 0


def reset_ids() -> None:
    """Restart the deterministic id sequence. Call once before building each company."""
    global _uuid_counter
    _uuid_counter = 0


def _next_uuid() -> uuid.UUID:
    global _uuid_counter
    _uuid_counter += 1
    return uuid.UUID(int=_uuid_counter, version=4)


@contextlib.contextmanager
def frozen_clock() -> Iterator[None]:
    """Freeze ``date.today()`` and ``datetime.now()`` and make ``uuid.uuid4()`` a counter.

    The engine names schema-upgrade backups with a random uuid and records that
    name in the audit hash chain, so random ids would make the chain differ on
    every run. The counter restarts with ``reset_ids()``.

    The frozen classes only override constructors that read the clock; every
    instance they return is a real ``date`` or ``datetime``.
    """
    import bookkeeping.cli  # noqa: F401  - make sure the package is loaded before patching
    patches: list[tuple[Any, str, Any]] = []
    for target in [_datetime_module, *[m for n, m in list(sys.modules.items())
                                       if m is not None and (n == "bookkeeping" or n.startswith("bookkeeping."))]]:
        for name, real, fake in (("datetime", _RealDatetime, _FrozenDatetime), ("date", _RealDate, _FrozenDate)):
            if getattr(target, name, None) is real:
                patches.append((target, name, real))
                setattr(target, name, fake)
    real_uuid4 = uuid.uuid4
    uuid.uuid4 = _next_uuid
    try:
        yield
    finally:
        uuid.uuid4 = real_uuid4
        for target, name, real in reversed(patches):
            setattr(target, name, real)


# ---------------------------------------------------------------------------
# CLI runner and normalizer
# ---------------------------------------------------------------------------

class Normalizer:
    def __init__(self, company: Path | None = None, work: Path | None = None) -> None:
        pairs: list[tuple[str, str]] = []
        for path, label in ((company, "<COMPANY>"), (work, "<WORK>"), (REPO, "<REPO>")):
            if path is None:
                continue
            for variant in {str(path), str(path.resolve())}:
                pairs.append((variant, label))
        # Longest first so a company directory is not rewritten as its parent.
        self.pairs = sorted(pairs, key=lambda pair: -len(pair[0]))

    def __call__(self, text: str) -> str:
        for needle, label in self.pairs:
            text = text.replace(needle, label)
        return text


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run ``books`` in-process under the frozen clock; return (exit, stdout, stderr)."""
    from bookkeeping.cli import main

    out, err = io.StringIO(), io.StringIO()
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as empty, frozen_clock():
        os.chdir(empty)  # `books` reads ./.env; never read the developer's
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = main(list(argv))
                except SystemExit as exit_:
                    code = exit_.code if isinstance(exit_.code, int) else 1
        finally:
            os.chdir(cwd)
    return int(code or 0), out.getvalue(), err.getvalue()


class _Raw(str):
    """A JSON number kept as its original text, so 1.50 is never shown as 1.5."""


def _render(value: Any, indent: int = 0) -> str:
    pad = "  " * (indent + 1)
    if isinstance(value, _Raw):
        return str(value)
    if isinstance(value, dict):
        if not value:
            return "{}"
        body = ",\n".join(f"{pad}{json.dumps(k, ensure_ascii=False)}: {_render(v, indent + 1)}" for k, v in value.items())
        return "{\n" + body + "\n" + "  " * indent + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        body = ",\n".join(pad + _render(v, indent + 1) for v in value)
        return "[\n" + body + "\n" + "  " * indent + "]"
    return json.dumps(value, ensure_ascii=False)


def _stable_package_id(text: str) -> str:
    """Recompute a readiness ``package_id`` over the path-normalized document (see module doc)."""
    from bookkeeping.readiness import _hash

    try:
        document = json.loads(text)
    except ValueError:
        return text
    if not isinstance(document, dict) or "package_id" not in document:
        return text
    old_id = document.pop("package_id")
    document["package_id"] = _hash(document)
    return text.replace(f'"package_id": "{old_id}"', f'"package_id": "{document["package_id"]}"')


def pretty_json(text: str) -> str | None:
    """Re-indent JSON for reading, keeping key order and number text. None when not JSON."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return _render(json.loads(stripped, parse_float=_Raw, parse_int=_Raw)) + "\n"
    except ValueError:
        return None


class Capture:
    """Ordered collection of named outputs for one company."""

    def __init__(self, normalize: Normalizer) -> None:
        self.normalize = normalize
        self.files: dict[str, str] = {}
        self.manifest: dict[str, dict[str, Any]] = {}

    def put(self, name: str, text: str, **info: Any) -> None:
        if name in self.files:
            raise AssertionError(f"Duplicate golden name {name}")
        text = self.normalize(text)
        if not text.endswith("\n") and text:
            text += "\n"
        self.files[name] = text
        if info:
            self.manifest[name] = info

    def cli(self, name: str, argv: list[str], *, kind: str | None = None) -> tuple[int, str, str]:
        """Run a command and record stdout (pretty JSON or text), exit code and stderr."""
        code, out, err = run_cli(argv)
        out, err = self.normalize(out), self.normalize(err)
        if argv and argv[0] == "readiness":
            out = _stable_package_id(out)
        pretty = pretty_json(out)
        extension = kind or ("json" if pretty is not None else "txt")
        info: dict[str, Any] = {"argv": [self.normalize(a) for a in argv], "exit": code,
                                "stdout_sha256": hashlib.sha256(out.encode()).hexdigest()}
        if err:
            info["stderr"] = err
        self.put(f"{name}.{extension}", pretty if pretty is not None and extension == "json" else out, **info)
        return code, out, err

    def data(self, name: str, value: Any) -> None:
        self.put(f"{name}.json", json.dumps(value, indent=2, ensure_ascii=False, default=str, sort_keys=True) + "\n")

    def tree(self, prefix: str, directory: Path) -> None:
        """Record a generated directory: sorted file list, CSV text, XLSX as per-sheet text."""
        names = sorted(str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file())
        self.put(f"{prefix}/FILES.txt", "\n".join(names) + "\n")
        for relative in names:
            path = directory / relative
            if path.suffix == ".csv":
                self.put(f"{prefix}/{relative}", path.read_text(encoding="utf-8"))
            elif path.suffix == ".xlsx":
                self.put(f"{prefix}/{relative}.sheets.txt", xlsx_text(path))
            elif path.suffix in {".json", ".txt", ".md"}:
                text = path.read_text(encoding="utf-8")
                self.put(f"{prefix}/{relative}", pretty_json(text) or text)
            else:
                self.put(f"{prefix}/{relative}.sha256.txt", hashlib.sha256(path.read_bytes()).hexdigest())


def xlsx_text(path: Path) -> str:
    """Dump every sheet of an XLSX as tab-separated text, ignoring packaging metadata."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    with zipfile.ZipFile(path) as archive:
        strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = ["".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")) for si in root.findall("m:si", ns)]
        book = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        lines: list[str] = []
        for sheet in book.find("m:sheets", ns):
            target = targets[sheet.get(rel_ns)].lstrip("/")
            member = target if target.startswith("xl/") else "xl/" + target
            lines.append(f"=== sheet: {sheet.get('name')}")
            root = ET.fromstring(archive.read(member))
            for row in root.iter(f"{{{ns['m']}}}row"):
                cells = []
                for cell in row.findall("m:c", ns):
                    value = cell.find("m:v", ns)
                    kind = cell.get("t")
                    if kind == "s" and value is not None:
                        text = strings[int(value.text)]
                    elif kind == "inlineStr":
                        text = "".join(t.text or "" for t in cell.iter(f"{{{ns['m']}}}t"))
                    else:
                        text = value.text if value is not None else ""
                    cells.append(f"{cell.get('r')}={text}")
                lines.append("\t".join(cells))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Golden comparison
# ---------------------------------------------------------------------------

REGENERATE_HELP = (
    "These golden outputs changed. They are the proof that existing companies keep producing\n"
    "byte-identical books, reports and exports. A change here is a user-visible behavior change.\n"
    "If the change is intended, regenerate with:\n\n"
    "    UPDATE_GOLDEN=1 .venv/bin/python -m unittest tests.test_golden_outputs\n\n"
    "WARNING: regenerating requires a written reason in the pull request that says what\n"
    "changed for users and why. Do not regenerate to make a refactor pass."
)


def _manifest_text(capture: Capture) -> str:
    return json.dumps(capture.manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def compare_or_update(company: str, capture: Capture) -> list[str]:
    """Compare captured outputs with tests/fixtures/golden/<company>. Return diff blocks."""
    directory = GOLDEN_ROOT / company
    files = dict(capture.files)
    files["MANIFEST.json"] = _manifest_text(capture)
    if os.environ.get("UPDATE_GOLDEN"):
        if directory.exists():
            shutil.rmtree(directory)
        for name, text in files.items():
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return []
    problems: list[str] = []
    existing = {str(p.relative_to(directory)): p for p in directory.rglob("*") if p.is_file()} if directory.exists() else {}
    for name in sorted(set(files) | set(existing)):
        if name not in existing:
            problems.append(f"NEW output with no golden file: {company}/{name}")
        elif name not in files:
            problems.append(f"MISSING output (golden file exists, nothing produced): {company}/{name}")
        else:
            expected = existing[name].read_text(encoding="utf-8")
            if expected != files[name]:
                diff = list(difflib.unified_diff(
                    expected.splitlines(), files[name].splitlines(),
                    f"golden/{company}/{name}", f"actual/{company}/{name}", lineterm="", n=2))
                shown = diff[:60]
                if len(diff) > len(shown):
                    shown.append(f"... {len(diff) - len(shown)} more diff lines")
                problems.append("\n".join(shown))
    return problems
