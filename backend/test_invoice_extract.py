"""Text-first invoice extraction remains bounded and review-only."""
import tempfile
import unittest
from pathlib import Path

from shimline.invoice_extract import extract_document, parse_invoice_text
from shimline.paddle_ocr import MAX_OCR_LINES, read_text

SAMPLE = """ONTARIO LUMBER SUPPLY
Invoice #: OLS-88213
Date: 2026-05-19
Job: Basement #241
Subtotal: $1,825.00
TOTAL DUE: $2,062.25
"""


class InvoiceExtractionTests(unittest.TestCase):
    def test_deterministic_parser_finds_review_fields(self):
        fields = parse_invoice_text(SAMPLE)
        self.assertEqual(fields["vendor"], "ONTARIO LUMBER SUPPLY")
        self.assertEqual(fields["invoice_number"], "OLS-88213")
        self.assertEqual(fields["job_reference"], "Basement #241")
        self.assertEqual(fields["total"], "2062.25")
        self.assertEqual(fields["dates"], ["2026-05-19"])

    def test_image_uses_injected_ocr_and_never_becomes_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invoice.png"
            path.write_bytes(b"synthetic image bytes")
            result = extract_document(path, ocr_reader=lambda _path: SAMPLE)
        self.assertEqual(result["extraction_method"], "ocr")
        self.assertEqual(result["authority"], "proposal_only")
        self.assertTrue(result["review_required"])
        self.assertNotIn("raw_text", result)

    def test_unsupported_files_are_rejected_before_parsing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invoice.csv"
            path.write_text(SAMPLE, encoding="utf-8")
            with self.assertRaises(ValueError):
                extract_document(path)

    def test_paddle_adapter_accepts_current_result_shape_and_bounds_lines(self):
        class FakePipeline:
            def predict(self, _path):
                return [{"res": {"rec_texts": ["Line"] * (MAX_OCR_LINES + 10)}}]

        text = read_text(Path("synthetic.png"), pipeline_factory=FakePipeline)
        self.assertEqual(len(text.splitlines()), MAX_OCR_LINES)


if __name__ == "__main__":
    unittest.main()
