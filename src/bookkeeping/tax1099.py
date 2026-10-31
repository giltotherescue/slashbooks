"""Payment-based 1099 preparation. No tax identifiers, filing, or delivery state."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

RULES = {2025: "2025.1", 2026: "2026.1"}
CATEGORIES = {
    "nonemployee_compensation": ("1099-NEC", "1"),
    "legal_services": ("1099-NEC", "1"),
    "rent": ("1099-MISC", "1"),
    "royalties": ("1099-MISC", "2"),
    "other_income": ("1099-MISC", "3"),
    "medical_healthcare": ("1099-MISC", "6"),
    "attorney_gross_proceeds": ("1099-MISC", "10"),
}
CORPORATE_EXCEPTIONS = {"legal_services", "medical_healthcare", "attorney_gross_proceeds"}
CLASSIFICATIONS = {"individual", "sole_proprietor", "partnership", "estate", "c_corporation", "s_corporation"}
TIN_PATTERN = re.compile(r"(?<!\d)(?:\d{3}-\d{2}-\d{4}|\d{2}-\d{7}|\d{9})(?!\d)")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def reject_protected_data(value: Any, *, allow_opaque_ids: bool = False) -> None:
    """Reject identifiers before hashing, logging, or persisting routine inputs."""
    _reject_protected_data(value, allow_opaque_ids, ())


def _reject_protected_data(value: Any, allow_opaque_ids: bool, path: tuple) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if normalized.endswith(("tin", "ssn", "ein", "taxid", "taxidentifier", "taxpayerid", "taxpayeridentificationnumber", "employeridentificationnumber", "socialsecuritynumber")):
                raise ValueError("Protected tax identifiers require the privileged export path")
            # Only documented typed identities can contain an opaque bank ID.
            # Formatted SSNs/EINs are refused even in these fields. A caller
            # must still validate linkage; arbitrary free text is not an ID.
            if allow_opaque_ids and key in {"id", "party_id", "item_id", "source_id", "refund_of", "w9_evidence_id", "operation_id"} and isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:~-]+", item) and not re.search(r"\d{3}-\d{2}-\d{4}|\d{2}-\d{7}", item):
                continue
            # Canonical journal metadata uses hyphenated reference keys. A
            # generated reference can contain a nine-digit run, but a bare or
            # formatted taxpayer identifier is never a journal reference.
            if (allow_opaque_ids and len(path) == 3 and path[0] == "entries"
                    and isinstance(path[1], int) and path[2] == "meta"
                    and key in {"item-id", "source-id", "origin-source-id"}
                    and isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:~-]{1,220}", item)
                    and re.search(r"[A-Za-z]", item)
                    and not re.search(r"\d{3}-\d{2}-\d{4}|\d{2}-\d{7}", item)):
                continue
            if allow_opaque_ids and key in {"evidence_ids", "coverage_evidence_ids", "payment_ids", "source_ids"} and isinstance(item, list):
                for identity in item:
                    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.:~-]+", identity) or re.search(r"\d{3}-\d{2}-\d{4}|\d{2}-\d{7}", identity):
                        _reject_protected_data(identity, False, ())
                continue
            _reject_protected_data(item, allow_opaque_ids, (*path, key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_protected_data(item, allow_opaque_ids, (*path, index))
    elif isinstance(value, str) and not re.fullmatch(r"[0-9a-f]{64}", value) and TIN_PATTERN.search(value):
        raise ValueError("Possible protected tax identifier in routine input")
    elif type(value) is int and 100000000 <= abs(value) <= 999999999:
        raise ValueError("Possible protected tax identifier in routine input")


def money(value: Any) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Money must be an exact decimal string")
    try:
        result = Decimal(value)
        if not result.is_finite() or abs(result) > Decimal("99999999.99") or result != result.quantize(Decimal("0.01")):
            raise ValueError("Money must be finite cents within supported limits")
    except (InvalidOperation, ValueError):
        raise ValueError("Money must be finite cents within supported limits") from None
    return result.quantize(Decimal("0.01"))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _profile(raw: dict, recipient: bool = False) -> dict:
    keys = ("party_id", "legal_name", "tax_classification", "classification_verified", "w9_status", "tin_status", "verified", "w9_evidence_id", "phone", "payer_type")
    _only_fields(raw, {*keys, "id", "address"}, "tax profile")
    profile = {key: raw[key] for key in keys if key in raw}
    address = raw.get("address", {})
    _only_fields(address, {"line1", "line2", "city", "region", "postal_code", "country"}, "tax address")
    profile["address"] = {key: address[key] for key in ("line1", "line2", "city", "region", "postal_code", "country") if key in address}
    if not recipient and "id" in raw:
        profile["id"] = raw["id"]
    return profile


def _profile_issues(profile: dict, recipient: bool) -> list[str]:
    issues = []
    if not _text(profile.get("legal_name")):
        issues.append("missing_legal_name")
    address = profile.get("address", {})
    if any(not _text(address.get(key)) for key in ("line1", "city", "region", "postal_code")):
        issues.append("missing_address")
    if address.get("country") != "US":
        issues.append("unsupported_country")
    if profile.get("tin_status") != "verified":
        issues.append("missing_verified_tin")
    if recipient:
        if profile.get("classification_verified") is not True or profile.get("tax_classification") not in CLASSIFICATIONS:
            issues.append("unverified_or_unsupported_tax_classification")
        if profile.get("w9_status") != "verified":
            issues.append("missing_verified_w9")
    elif profile.get("verified") is not True:
        issues.append("unverified_payer")
    if not recipient and profile.get("payer_type", "ordinary_business") != "ordinary_business":
        issues.append("unsupported_payer_type")
    if not recipient and not _text(profile.get("id")):
        issues.append("missing_payer_identity")
    if not recipient and not _text(profile.get("phone")):
        issues.append("missing_payer_phone")
    return issues


def _only_fields(value: Any, allowed: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"Unsupported fields in {label}; use the documented facts contract")


def _payment(raw: dict) -> dict:
    _only_fields(raw, {"id", "party_id", "date", "currency", "gross_amount", "cash_amount", "federal_withholding", "state_withholding", "payment_method", "direction", "refund_of", "network_qualifies_1099k", "source", "evidence_ids", "allocations", "fee_amount", "recognition_lines", "excess"}, "paid facts")
    if "fee_amount" in raw and money(raw["fee_amount"]) < 0:
        raise ValueError("Payment fees must be nonnegative")
    for line in raw.get("recognition_lines", []):
        _only_fields(line, {"account", "amount"}, "recognition metadata")
        money(line.get("amount"))
    if "excess" in raw:
        _only_fields(raw["excess"], {"id", "account"}, "excess metadata")
    p = {key: raw[key] for key in ("id", "party_id", "date", "currency", "payment_method", "direction", "refund_of", "network_qualifies_1099k") if key in raw}
    if any(not _text(p.get(key)) for key in ("id", "party_id", "date")):
        raise ValueError("Payment identity, recipient and date are required")
    try:
        if date.fromisoformat(p["date"]).isoformat() != p["date"]:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("Payment date must be an ISO calendar date") from None
    for key in ("gross_amount", "cash_amount", "federal_withholding", "state_withholding"):
        p[key] = str(money(raw.get(key, "0.00")))
        if money(p[key]) < 0:
            raise ValueError("Payment amounts must be nonnegative; refunds use receive direction")
    if money(p["gross_amount"]) <= 0 or money(p["cash_amount"]) + money(p["federal_withholding"]) != money(p["gross_amount"]):
        raise ValueError("Payment gross must equal positive cash plus withholding")
    if p.get("direction") not in {"pay", "receive"}:
        raise ValueError("Payment direction must be pay or receive")
    source = raw.get("source", {})
    _only_fields(source, {"source_id", "bank_account", "mode", "amount"}, "payment source")
    if "amount" in source:
        fee = money(raw.get("fee_amount", "0.00"))
        expected = (-1 if p["direction"] == "pay" else 1) * money(p["cash_amount"]) - fee
        if money(source["amount"]) != expected:
            raise ValueError("Payment source amount does not match signed cash less fee")
    p["source"] = {key: source[key] for key in ("source_id", "bank_account", "mode") if key in source}
    p["evidence_ids"] = sorted(set(raw.get("evidence_ids", [])))
    p["allocations"] = []
    for allocation in raw.get("allocations", []):
        _only_fields(allocation, {"item_id", "amount", "tax_category", "classification_verified", "evidence_ids"}, "tax allocation")
        row = {key: allocation[key] for key in ("item_id", "tax_category", "classification_verified", "evidence_ids") if key in allocation}
        row["amount"] = str(money(allocation.get("amount")))
        if money(row["amount"]) < 0:
            raise ValueError("Payment allocations must be nonnegative")
        p["allocations"].append(row)
    if sum((money(a["amount"]) for a in p["allocations"]), Decimal(0)) != money(p["gross_amount"]):
        raise ValueError("Payment allocations must equal gross amount")
    return p


def seal_preparation(preparation: dict) -> dict:
    """Bind all report facts. The host owns authenticated review authorization."""
    preparation["content_hash"] = _hash({key: value for key, value in preparation.items() if key not in {"content_hash", "status"}})
    return preparation


def verify_preparation(preparation: dict) -> None:
    reject_protected_data(preparation, allow_opaque_ids=True)
    expected = _hash({key: value for key, value in preparation.items() if key not in {"content_hash", "status"}})
    if preparation.get("content_hash") != expected or preparation.get("rule_version") != RULES.get(preparation.get("tax_year")):
        raise ValueError("Preparation has changed or uses unsupported rules")


def prepare_1099(payload: dict) -> dict:
    """Prepare a deterministic review from confirmed paid facts; never infer facts."""
    reject_protected_data(payload, allow_opaque_ids=True)
    _only_fields(payload, {"tax_year", "rule_version", "payer_profile", "recipients", "payments", "coverage"}, "1099 input")
    year = payload.get("tax_year")
    if type(year) is not int or year not in RULES:
        raise ValueError("Only explicit tax years 2025 and 2026 are supported")
    version = RULES[year]
    if payload.get("rule_version", version) != version:
        raise ValueError("Unsupported rule version for tax year")
    payer = _profile(payload.get("payer_profile", {}))
    profiles = [_profile(raw, True) for raw in payload.get("recipients", [])]
    recipients = {p.get("party_id"): p for p in profiles}
    if None in recipients or "" in recipients or len(recipients) != len(profiles):
        raise ValueError("Recipient party IDs must be present and unique")
    payments = [_payment(raw) for raw in payload.get("payments", [])]
    issues: list[dict] = []
    def issue(code: str, party_id: str | None = None, payment_id: str | None = None) -> None:
        row = {"code": code}
        if party_id: row["party_id"] = party_id
        if payment_id: row["payment_id"] = payment_id
        if row not in issues: issues.append(row)
    for code in _profile_issues(payer, False): issue(code)
    coverage = payload.get("coverage", {})
    _only_fields(coverage, {"complete", "opening_payments_complete", "start_date", "end_date"}, "payment coverage")
    if coverage.get("complete") is not True or coverage.get("opening_payments_complete") is not True or coverage.get("start_date") != f"{year}-01-01" or coverage.get("end_date") != f"{year}-12-31":
        issue("incomplete_calendar_year_payment_coverage")
    # Every copy with conflicting canonical/source identity is blocked, independent of order.
    identities: dict[tuple, list[int]] = defaultdict(list)
    from .source_ids import source_id_variants
    for index, p in enumerate(payments):
        identities[("id", p["id"])].append(index)
        if p["source"].get("source_id"):
            for variant in source_id_variants(p["source"]["source_id"]):
                identities[("source", variant)].append(index)
    conflicts: set[int] = set()
    duplicates: set[int] = set()
    def facts(p: dict) -> str:
        return _json({**{k: v for k, v in p.items() if k not in {"id", "source", "evidence_ids"}}, "source_bank_account": p["source"].get("bank_account")})
    for indices in identities.values():
        if len({facts(payments[i]) for i in indices}) > 1:
            conflicts.update(indices)
        else:
            duplicates.update(indices[1:])
    originals = {p["id"]: p for i, p in enumerate(payments) if i not in conflicts and i not in duplicates}
    refund_totals: dict[str, Decimal] = defaultdict(Decimal)
    refund_categories: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for index, p in enumerate(payments):
        if index not in conflicts and index not in duplicates and p.get("refund_of"):
            refund_totals[p["refund_of"]] += money(p["gross_amount"])
            for allocation in p["allocations"]:
                refund_categories[(p["refund_of"], allocation.get("tax_category", "unknown"))] += money(allocation["amount"])
    rows = []
    candidates: list[tuple[dict, dict, Decimal]] = []
    bucket_totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    withholding_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for index, p in enumerate(payments):
        row = dict(p, amount=p["gross_amount"], withholding=p["federal_withholding"], status="excluded", reasons=[])
        rows.append(row)
        if date.fromisoformat(p["date"]).year != year:
            row["reasons"].append("outside_tax_year"); continue
        if index in conflicts:
            row["status"] = "blocked"; row["reasons"].append("conflicting_payment_identity"); issue("conflicting_payment_identity", p["party_id"], p["id"]); continue
        if index in duplicates:
            row["reasons"].append("duplicate_payment"); continue
        reasons = row["reasons"]
        recipient = recipients.get(p["party_id"])
        if recipient is None: reasons.append("missing_recipient_profile")
        if p.get("currency") != "USD": reasons.append("unsupported_currency")
        if money(p["state_withholding"]) > 0: reasons.append("state_withholding_not_supported")
        if not p["source"].get("source_id") or not p["evidence_ids"]: reasons.append("missing_payment_provenance")
        method = p.get("payment_method")
        if method not in {"bank_transfer", "check", "cash", "card", "payment_network"}: reasons.append("unknown_payment_method")
        if method == "payment_network" and type(p.get("network_qualifies_1099k")) is not bool:
            reasons.append("unknown_network_reporting_treatment")
        refund = p.get("refund_of")
        original = originals.get(refund) if refund else None
        if p["direction"] == "receive" and not refund: reasons.append("unclassified_received_payment")
        if refund:
            if not original or p["direction"] != "receive" or original.get("direction") != "pay" or original["party_id"] != p["party_id"]:
                reasons.append("invalid_refund_reference")
            elif original["date"][:4] != str(year): reasons.append("prior_year_refund_requires_review")
            elif original["date"] > p["date"] or refund_totals[refund] > money(original["gross_amount"]): reasons.append("invalid_refund_amount_or_date")
            elif original["payment_method"] != method or original["allocations"] != p["allocations"] and [(a.get("tax_category"), a.get("classification_verified")) for a in original["allocations"]] != [(a.get("tax_category"), a.get("classification_verified")) for a in p["allocations"]]:
                reasons.append("refund_classification_mismatch")
            if original:
                original_categories: dict[str, Decimal] = defaultdict(Decimal)
                for allocation in original["allocations"]:
                    original_categories[allocation.get("tax_category", "unknown")] += money(allocation["amount"])
                if any(refund_categories[(refund, category)] > amount for category, amount in original_categories.items()):
                    reasons.append("refund_category_exceeds_original")
            if money(p["federal_withholding"]) > 0: reasons.append("refund_withholding_requires_review")
        if reasons:
            row["status"] = "blocked"
            for code in sorted(set(reasons)): issue(code, p["party_id"], p["id"])
            continue
        if method == "card" or method == "payment_network" and p["network_qualifies_1099k"]:
            if money(p["federal_withholding"]) > 0:
                row["status"] = "blocked"; reasons.append("processor_payment_withholding_conflict"); issue(reasons[-1], p["party_id"], p["id"])
            else:
                reasons.append("payment_settlement_entity_reporting")
                row["review_allocations"] = []
                for allocation in p["allocations"]:
                    detail = dict(allocation)
                    if allocation.get("classification_verified") is not True or allocation.get("tax_category") not in {*CATEGORIES, "not_reportable"}:
                        detail.update(status="blocked", reason="unverified_or_unsupported_payment_category")
                        row["status"] = "blocked"
                        issue(detail["reason"], p["party_id"], p["id"])
                    else: detail.update(status="excluded", reason="payment_settlement_entity_reporting")
                    row["review_allocations"].append(detail)
            continue
        sign = Decimal(-1) if refund else Decimal(1)
        eligible = []
        for allocation in p["allocations"]:
            category = allocation.get("tax_category", "unknown")
            detail = dict(allocation)
            if allocation.get("classification_verified") is not True or category not in {*CATEGORIES, "not_reportable"}:
                detail.update(status="blocked", reason="unverified_or_unsupported_payment_category")
                row["status"] = "blocked"
                issue(detail["reason"], p["party_id"], p["id"])
            elif category == "not_reportable":
                detail.update(status="excluded", reason="confirmed_not_reportable")
            elif recipient.get("classification_verified") is not True or recipient.get("tax_classification") not in CLASSIFICATIONS:
                detail.update(status="blocked", reason="unverified_or_unsupported_tax_classification")
                row["status"] = "blocked"
                issue(detail["reason"], p["party_id"], p["id"])
            elif recipient.get("classification_verified") is True and recipient.get("tax_classification") in {"c_corporation", "s_corporation"} and category not in CORPORATE_EXCEPTIONS:
                detail.update(status="excluded", reason="corporation_exemption")
            else:
                form, box = CATEGORIES[category]
                if year == 2026 and form == "1099-NEC": box = "1a"
                amount = sign * money(allocation["amount"])
                detail.update(form=form, box=box, amount=str(amount), status="candidate")
                bucket_totals[(p["party_id"], form, box)] += amount
                candidates.append((row, detail, amount)); eligible.append(form)
            row.setdefault("review_allocations", []).append(detail)
        withholding = money(p["federal_withholding"])
        if withholding and len(set(eligible)) != 1:
            issue("withholding_form_allocation_required", p["party_id"], p["id"])
            row["status"] = "blocked"; reasons.append("withholding_form_allocation_required")
        elif withholding:
            withholding_totals[(p["party_id"], eligible[0])] += withholding
        if not eligible: reasons.append("no_reportable_categories")
    forms: dict[tuple[str, str], dict] = {}
    for row, detail, amount in candidates:
        key = (row["party_id"], detail["form"], detail["box"])
        box = detail["box"]
        threshold = Decimal("10") if detail["form"] == "1099-MISC" and box == "2" else Decimal("600") if detail["form"] == "1099-MISC" and box == "10" else Decimal("600" if year == 2025 else "2000")
        total = bucket_totals[key]
        if total < 0:
            detail.update(status="blocked", reason="negative_reportable_total"); row["status"] = "blocked"; issue("negative_reportable_total", row["party_id"])
        elif total >= threshold or withholding_totals[(row["party_id"], detail["form"])] > 0:
            detail.update(status="included", reason="threshold_met" if total >= threshold else "federal_withholding")
            if row["status"] != "blocked": row["status"] = "included"
            form_key = (row["party_id"], detail["form"])
            form = forms.setdefault(form_key, {"party_id": row["party_id"], "form": detail["form"], "boxes": {}, "payment_ids": []})
            form["boxes"][box] = str(total)
            if row["id"] not in form["payment_ids"]: form["payment_ids"].append(row["id"])
        else: detail.update(status="excluded", reason="below_box_threshold")
    for (party_id, form_name), form in forms.items():
        for code in _profile_issues(recipients[party_id], True):
            issue(code, party_id)
        withheld = withholding_totals[(party_id, form_name)]
        if withheld: form["boxes"]["4"] = str(withheld)
        form["ready"] = not any(i.get("party_id") in {None, party_id} for i in issues)
        form["payment_ids"].sort()
    totals = {key: "0.00" for key in ("included", "excluded", "blocked")}
    for row in rows:
        if "outside_tax_year" not in row["reasons"] and "duplicate_payment" not in row["reasons"]:
            sign = -1 if row.get("refund_of") else 1
            details = row.get("review_allocations")
            if details:
                for detail in details:
                    status = detail["status"]
                    totals[status] = str(money(totals[status]) + sign * abs(money(detail["amount"])))
            else:
                totals[row["status"]] = str(money(totals[row["status"]]) + sign * money(row["gross_amount"]))
        if not row["reasons"]:
            row["reasons"] = sorted({a.get("reason", "review_required") for a in row.get("review_allocations", [])})
    result = {"schema_version": 1, "tax_year": year, "rule_version": version, "input_hash": _hash(payload), "status": "prepared", "ready": not issues,
              "payer_profile": payer, "recipients": sorted(profiles, key=lambda p: p["party_id"]), "coverage": dict(coverage),
              "forms": sorted(forms.values(), key=lambda f: (f["party_id"], f["form"])), "payments": sorted(rows, key=lambda p: (p["date"], p["id"])),
              "issues": sorted(issues, key=_json), "totals": totals,
              "scope": {"filing": False, "delivery": False, "state_reporting": "not_prepared"}}
    return seal_preparation(result)


def export_cpa_package(preparation: dict, output_dir: str | Path, protected_profiles: dict | None = None) -> dict:
    """Write a new private handoff directory. Explicit protected export is separate."""
    verify_preparation(preparation)
    directory = Path(output_dir).resolve()
    if protected_profiles is not None:
        from .tax1099_pdf import validate_protected_profiles
        validate_protected_profiles(preparation, protected_profiles)
        if directory.is_relative_to(Path(__file__).resolve().parents[2]):
            raise ValueError("Protected exports must be outside the source repository")
    files: dict[str, str] = {}
    contents: dict[str, str] = {}
    def write(name: str, data: str) -> None:
        contents[name] = data
        files[name] = hashlib.sha256(data.encode()).hexdigest()
    write("preparation.json", json.dumps(preparation, indent=2, sort_keys=True) + "\n")
    import io
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Party ID", "Legal name", "Form", "Box", "Amount", "Tax year", "Ready"])
    profiles = {p["party_id"]: p for p in preparation["recipients"]}
    def csv_text(value: str) -> str:
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
    for form in preparation["forms"]:
        for box, amount in sorted(form["boxes"].items()):
            party = form["party_id"]
            writer.writerow([csv_text(party), csv_text(profiles[party]["legal_name"]), form["form"], box, amount, preparation["tax_year"], str(form["ready"]).lower()])
    write("form-totals.csv", output.getvalue())
    if protected_profiles is not None:
        write("protected-tax-profiles.json", json.dumps(protected_profiles, indent=2, sort_keys=True) + "\n")
    write("README.txt", "1099 preparation only. No government filing or recipient delivery has occurred.\nReview preparation.json payment detail, exclusions, unresolved issues and coverage.\nState reporting is not prepared. Copy 2 is supplied for recipient use only.\nThe customer or preparer must determine state duties and distribute approved copies.\nElectronic furnishing requires applicable recipient consent; printable copies are available.\n")
    write("manifest.json", json.dumps({"content_hash": preparation["content_hash"], "tax_year": preparation["tax_year"], "protected_profiles_included": protected_profiles is not None, "files": dict(files)}, indent=2) + "\n")
    lock_path = directory.with_name(directory.name + ".export-lock")
    lock = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    staging = None
    try:
        if directory.exists(): raise FileExistsError("Export directory already exists")
        staging = Path(tempfile.mkdtemp(prefix=".tax1099-export-", dir=directory.parent))
        for name, data in contents.items():
            fd = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
        # Publish a complete package under the task's exclusive export lock.
        if directory.exists(): raise FileExistsError("Export directory already exists")
        os.rename(staging, directory)
        staging = None
    finally:
        os.close(lock)
        lock_path.unlink()
        if staging is not None: shutil.rmtree(staging)
    return {"directory": str(directory), "files": sorted(files), "content_hash": preparation["content_hash"], "protected_profiles_included": protected_profiles is not None}
