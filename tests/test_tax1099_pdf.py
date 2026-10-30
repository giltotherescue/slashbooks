"""Official recipient copies use synthetic transient tax profiles only."""
import copy
import hashlib
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bookkeeping.tax1099 import prepare_1099
from bookkeeping.tax1099_pdf import render_recipient_pdf, TEMPLATES
from test_tax1099 import payload

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


def protected():
    return {"payer": {"id": "payer", "tin": "12" + "-" + "3456789"}, "recipient": {"party_id": "vendor", "tin": "987" + "-" + "65" + "-" + "4321"}}


@unittest.skipIf(PdfReader is None, "optional pdf extra not installed")
class RecipientPdfTests(unittest.TestCase):
    def test_checksums_and_revisions(self):
        manifest = json.loads((TEMPLATES/"manifest.json").read_text())
        self.assertEqual(len(manifest["templates"]), 4)
        for filename, template in manifest["templates"].items():
            self.assertEqual(hashlib.sha256((TEMPLATES/filename).read_bytes()).hexdigest(), template["sha256"])

    def test_all_templates_static_recipient_pages_and_amounts(self):
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        for year in [2025, 2026]:
            for category in ["nonemployee_compensation", "medical_healthcare"]:
                p = payload(year, category, "2500.00")
                p["payments"][0].update(cash_amount="2400.00", federal_withholding="100.00")
                p["recipients"][0]["legal_name"] = "Example Recipient With A Long Verified Legal Business Name"
                p["recipients"][0]["address"]["line1"] = "100 Example Boulevard"
                p["recipients"][0]["address"]["line2"] = "Suite 200"
                prep = prepare_1099(p)
                for reviewed in [False, True]:
                    if reviewed: prep["status"] = "reviewed"
                    with tempfile.TemporaryDirectory() as root:
                        target = Path(root)/"recipient.pdf"
                        result = render_recipient_pdf(prep, "vendor", protected(), target)
                        self.assertEqual(result["draft"], not reviewed)
                        r = PdfReader(target)
                        self.assertEqual(len(r.pages), 3)
                        text = "\n".join(page.extract_text() for page in r.pages)
                        self.assertIn("Copy B", text); self.assertIn("Copy 2", text); self.assertIn("Instructions for Recipient", text)
                        self.assertIn("2500.00", text); self.assertIn("100.00", text)
                        self.assertNotIn("Copy A", r.pages[0].extract_text())
                        self.assertIn("REVIEWED RECIPIENT COPY" if reviewed else "DRAFT", text)
                        self.assertFalse(r.get_fields()); self.assertNotIn("/AcroForm", r.trailer["/Root"])
                        self.assertTrue(all(not page.get("/Annots") for page in r.pages))
                        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_recipient_isolation_and_identity_binding(self):
        p = payload(); other = copy.deepcopy(p["recipients"][0]); other.update(party_id="other", legal_name="Secret Other Recipient"); p["recipients"].append(other)
        prep = prepare_1099(p)
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)/"recipient.pdf"; render_recipient_pdf(prep, "vendor", protected(), target)
            raw = target.read_bytes()
            self.assertNotIn(b"Secret Other Recipient", raw)
            self.assertNotIn("Secret Other Recipient", "\n".join(page.extract_text() for page in PdfReader(target).pages))
            wrong = protected(); wrong["recipient"]["party_id"] = "other"
            with self.assertRaises(ValueError): render_recipient_pdf(prep, "vendor", wrong, Path(root)/"wrong.pdf")
        self.assertNotIn(protected()["recipient"]["tin"], json.dumps(prep))

    def test_blocked_review_and_oversized_fields_refused(self):
        p = payload(); p["coverage"]["complete"] = False
        prep = prepare_1099(p); prep["status"] = "reviewed"
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "Blocked"): render_recipient_pdf(prep, "vendor", protected(), Path(root)/"blocked.pdf")
        p = payload(); p["recipients"][0]["legal_name"] = "X" * 500
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "exceeds"): render_recipient_pdf(prepare_1099(p), "vendor", protected(), Path(root)/"long.pdf")

    def test_pdf_snapshot_and_atomic_output_guards(self):
        prep = prepare_1099(payload()); prep["status"] = "reviewed"
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)/"form.pdf"
            changed = copy.deepcopy(prep); changed["forms"][0]["boxes"]["1a"] = "1.00"
            with self.assertRaisesRegex(ValueError, "changed"): render_recipient_pdf(changed, "vendor", protected(), target)
            self.assertFalse(target.exists())
            with patch("bookkeeping.tax1099_pdf.os.fsync", side_effect=OSError("synthetic disk failure")):
                with self.assertRaises(OSError): render_recipient_pdf(prep, "vendor", protected(), target)
            self.assertEqual(list(Path(root).iterdir()), [])
            render_recipient_pdf(prep, "vendor", protected(), target)
            original = target.read_bytes()
            with self.assertRaises(FileExistsError): render_recipient_pdf(prep, "vendor", protected(), target)
            self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__": unittest.main()
