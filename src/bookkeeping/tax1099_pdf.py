"""Static official recipient copies. Protected identifiers are transient inputs."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import textwrap
import tempfile
from typing import Any

from .tax1099 import verify_preparation

TEMPLATES = Path(__file__).with_name("tax1099_templates")


def _tin(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:\d{9}|\d{2}-\d{7}|\d{3}-\d{2}-\d{4})", value) or set(value.replace("-", "")) == {"0"}:
        raise ValueError("A complete protected tax identifier is required for export")
    return value


def _check_protected(profile: dict, protected: dict, identity_key: str) -> None:
    if set(protected) - {identity_key, "tin", "legal_name", "address"}:
        raise ValueError("Unsupported protected profile fields")
    if protected.get(identity_key) != profile.get(identity_key):
        raise ValueError("Protected tax profile identity does not match preparation")
    _tin(protected.get("tin"))
    for key in ("legal_name", "address"):
        if key in protected and protected[key] != profile.get(key):
            raise ValueError("Protected tax profile facts differ from reviewed preparation")


def validate_protected_profiles(preparation: dict, profiles: dict) -> None:
    verify_preparation(preparation)
    if set(profiles) != {"payer", "recipients"}:
        raise ValueError("Protected profiles require payer and recipients only")
    _check_protected(preparation["payer_profile"], profiles.get("payer", {}), "id")
    supplied = profiles.get("recipients", {})
    expected = {p["party_id"]: p for p in preparation["recipients"]}
    if set(supplied) != set(expected):
        raise ValueError("Protected export must match the preparation's recipients")
    for party_id, profile in expected.items():
        _check_protected(profile, supplied[party_id], "party_id")


def _field_values(preparation: dict, recipient: dict, protected: dict, form: dict) -> dict[int, str]:
    year = preparation["tax_year"]
    payer = preparation["payer_profile"]
    pa, ra = payer["address"], recipient["address"]
    boxes = form["boxes"]
    values = {1: str(year)}
    if year == 2025:
        values.update({2: "\n".join(filter(None, [payer["legal_name"], pa["line1"], pa.get("line2"), f'{pa["city"]}, {pa["region"]} {pa["postal_code"]} US', payer.get("phone")])),
                       3: _tin(protected["payer"]["tin"]), 4: _tin(protected["recipient"]["tin"]),
                       5: recipient["legal_name"], 6: ", ".join(filter(None, [ra["line1"], ra.get("line2")])),
                       7: f'{ra["city"]}, {ra["region"]} {ra["postal_code"]} US'})
        mapping = {"1": 9, "4": 11} if form["form"] == "1099-NEC" else {"1": 9, "2": 10, "3": 11, "4": 12, "6": 14, "10": 17}
    else:
        values.update({2: payer["legal_name"], 3: pa["line1"], 4: pa.get("line2", ""), 5: pa["city"], 6: payer.get("phone", ""),
                       7: pa["region"], 8: "US", 9: pa["postal_code"], 10: _tin(protected["payer"]["tin"]),
                       11: _tin(protected["recipient"]["tin"]), 12: recipient["legal_name"], 13: ra["line1"], 14: ra.get("line2", ""),
                       15: ra["city"], 16: ra["region"], 17: "US", 18: ra["postal_code"]})
        mapping = {"1a": 20, "4": 26} if form["form"] == "1099-NEC" else {"1": 20, "2": 21, "3": 22, "4": 23, "6": 25, "10": 28}
    if set(boxes) - set(mapping):
        raise ValueError("Form contains unsupported recipient-copy boxes")
    values.update({mapping[box]: amount for box, amount in boxes.items()})
    return values


def _pdf_literal(text: str) -> bytes:
    try:
        encoded = text.encode("cp1252")
    except UnicodeEncodeError:
        raise ValueError("Recipient form text requires a supported Latin-script rendering") from None
    if any(ord(c) < 32 and c != "\n" for c in text):
        raise ValueError("Unsupported control character in form text")
    return encoded.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _paint(text: str, rect: list[float]) -> bytes:
    """Courier has exact 0.6-em advances; fail rather than clip or truncate."""
    x1, y1, x2, y2 = map(float, rect)
    width, height = x2 - x1 - 3, y2 - y1 - 1
    for size in (10, 9, 8, 7):
        columns = int(width / (0.6 * size))
        if columns < 1: continue
        lines = []
        for paragraph in text.split("\n"):
            lines.extend(textwrap.wrap(paragraph, columns, break_long_words=False, break_on_hyphens=False) or [""])
        if len(lines) * size <= height and all(len(line) <= columns for line in lines):
            result = bytearray()
            for index, line in enumerate(lines):
                baseline = y2 - 1.5 - size * (index + 0.8)
                result.extend(f"BT /SBMono {size} Tf 0 g 1 0 0 1 {x1+1.5:.3f} {baseline:.3f} Tm (".encode())
                result.extend(_pdf_literal(line)); result.extend(b") Tj ET\n")
            return bytes(result)
    raise ValueError("Tax form text exceeds available space; shorten verified address lines before export")


def render_recipient_pdf(preparation: dict, recipient_id: str, protected_profile: dict, output: str | Path) -> dict:
    """Render only this recipient's Copy B, original instructions and Copy 2.

    Host code must authorize protected export and attest review. Status alone is
    not an authorization token. Prepared reports remain visibly draft.
    """
    verify_preparation(preparation)
    if preparation.get("status") not in {"prepared", "reviewed"}:
        raise ValueError("Unsupported preparation state")
    recipient = next((p for p in preparation["recipients"] if p["party_id"] == recipient_id), None)
    if recipient is None: raise ValueError("Recipient is not part of this preparation")
    _check_protected(preparation["payer_profile"], protected_profile.get("payer", {}), "id")
    _check_protected(recipient, protected_profile.get("recipient", {}), "party_id")
    forms = [form for form in preparation["forms"] if form["party_id"] == recipient_id]
    if not forms: raise ValueError("No reportable recipient form is present")
    reviewed = preparation["status"] == "reviewed"
    if reviewed and (not preparation["ready"] or any(not f["ready"] for f in forms)):
        raise ValueError("Blocked preparation cannot produce reviewed recipient copies")
    if not preparation["payer_profile"].get("phone"):
        raise ValueError("Payer telephone number is required on recipient copies")
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject
    except ImportError:
        raise ValueError("Install the optional pdf extra to create recipient copies") from None
    manifest = json.loads((TEMPLATES / "manifest.json").read_text())
    writer = PdfWriter()
    sources = []
    for form in forms:
        filename = f'{form["form"].lower()}-{preparation["tax_year"]}.pdf'
        data = (TEMPLATES / filename).read_bytes()
        expected = manifest["templates"][filename]
        if hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise ValueError("Official tax template checksum mismatch")
        reader = PdfReader(io.BytesIO(data))
        values = _field_values(preparation, recipient, protected_profile, form)
        for index in expected["recipient_pages"]:
            original = reader.pages[index]
            # No form field tree, annotations, attachments or other form copies
            # are cloned. All inserted data are static page text.
            page = writer.add_page(original, excluded_keys=["/Annots", "/AA"])
            if index != expected["instruction_page"]:
                fields = {a.get_object().get("/T"): a.get_object().get("/Rect") for a in original.get("/Annots", [])}
                commands = bytearray(b"q\n")
                for number, value in values.items():
                    key = f"f2_{number}[0]"
                    if key not in fields: raise ValueError("Official template field map has changed")
                    if value: commands.extend(_paint(value, fields[key]))
                label = "REVIEWED RECIPIENT COPY - NOT FILED - STATE REPORTING NOT PREPARED" if reviewed else "DRAFT - NOT FOR DISTRIBUTION - NOT FILED"
                commands.extend(_paint(label, [50, 390, 575, 410])); commands.extend(b"Q\n")
                resources = page["/Resources"].get_object()
                fonts = resources.setdefault(NameObject("/Font"), DictionaryObject()).get_object()
                fonts[NameObject("/SBMono")] = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Courier"), NameObject("/Encoding"): NameObject("/WinAnsiEncoding")}))
                stream = DecodedStreamObject(); stream.set_data(bytes(commands))
                existing = page.get("/Contents")
                if existing is None: contents = []
                elif isinstance(existing, ArrayObject): contents = list(existing)
                else: contents = [existing]
                page[NameObject("/Contents")] = ArrayObject(contents + [writer._add_object(stream)])
        sources.append({"form": form["form"], "template_sha256": expected["sha256"], "revision": expected["revision"]})
    writer.add_metadata({"/Title": "Recipient tax copies", "/Subject": "Preparation only; no filing or delivery", "/Author": "Slashbooks"})
    target = Path(output).resolve()
    repo = Path(__file__).resolve().parents[2]
    if target.is_relative_to(repo): raise ValueError("Protected exports must be outside the source repository")
    buffer = io.BytesIO(); writer.write(buffer); rendered = buffer.getvalue()
    checked = PdfReader(io.BytesIO(rendered))
    if checked.get_fields() or "/AcroForm" in checked.trailer["/Root"] or any(page.get("/Annots") for page in checked.pages):
        raise ValueError("Recipient PDF contains unexpected interactive data")
    fd, temporary = tempfile.mkstemp(prefix=".tax1099-", suffix=".pdf", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(rendered); stream.flush(); os.fsync(stream.fileno())
        # Hard-link publication is atomic and refuses an existing destination.
        os.link(temporary, target)
    finally:
        Path(temporary).unlink()
    return {"path": str(target), "sha256": hashlib.sha256(rendered).hexdigest(), "page_count": len(checked.pages), "draft": not reviewed,
            "content_hash": preparation["content_hash"], "sources": sources, "filed": False, "delivered": False, "state_reporting": "not_prepared"}
