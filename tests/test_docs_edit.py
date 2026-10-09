import unittest
from unittest import mock

import httplib2
from googleapiclient.errors import HttpError

import docs_edit

DOC_ID = "1gFJPPFPv2tuw11jAzzH4tjE-bQjSWQa6"


def _make_pdf(text: str) -> bytes:
    """Build a minimal one-page PDF whose page draws `text`."""
    stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return out


def _invalid_argument() -> HttpError:
    return HttpError(
        httplib2.Response({"status": 400}),
        b'{"error": {"code": 400, "message": "Request contains an invalid argument."}}',
    )


class NonGoogleDocTest(unittest.TestCase):
    """The Docs API rejects Drive files that are not Google Docs with a bare 400."""

    def setUp(self):
        self.docs = mock.MagicMock()
        self.docs.documents().get().execute.side_effect = _invalid_argument()
        self.drive = mock.MagicMock()
        services = {"docs": self.docs, "drive": self.drive}
        patcher = mock.patch.object(
            docs_edit, "_get_service", side_effect=lambda api, version: services[api]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _drive_file(self, name: str, mime_type: str, size: int = 1024):
        self.drive.files().get().execute.return_value = {
            "name": name,
            "mimeType": mime_type,
            "size": str(size),
        }

    def test_get_reads_pdf_text_with_warning(self):
        self._drive_file("VAT registration.pdf", "application/pdf")
        self.drive.files().get_media().execute.return_value = _make_pdf(
            "VAT registration number 123"
        )

        result = docs_edit.get(DOC_ID)

        self.assertEqual(result["title"], "VAT registration.pdf")
        self.assertEqual(result["mime_type"], "application/pdf")
        self.assertIn("VAT registration number 123", result["plain_text"])
        self.assertIn("PDF", result["warning"])
        self.assertEqual(result["paragraphs"], [])
        self.drive.files().get_media.assert_called_with(
            fileId=DOC_ID, supportsAllDrives=True
        )

    def test_get_refuses_to_download_oversized_pdf(self):
        self._drive_file("Huge.pdf", "application/pdf", size=docs_edit.MAX_PDF_DOWNLOAD_BYTES + 1)

        with self.assertRaisesRegex(RuntimeError, "too large"):
            docs_edit.get(DOC_ID)

        self.drive.files().get_media().execute.assert_not_called()

    def test_get_explains_other_file_types(self):
        self._drive_file(
            "Contract.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        with self.assertRaisesRegex(docs_edit.NotAGoogleDocError, '"Contract.docx" is .* not a Google Doc'):
            docs_edit.get(DOC_ID)

    def test_editing_a_pdf_explains_it_is_not_a_google_doc(self):
        self._drive_file("VAT registration.pdf", "application/pdf")

        with self.assertRaisesRegex(
            docs_edit.NotAGoogleDocError, '"VAT registration.pdf" is a PDF, not a Google Doc'
        ):
            docs_edit.search_replace(DOC_ID, "old", "new")

    def test_real_google_doc_keeps_original_error(self):
        self._drive_file("Policy", docs_edit.GOOGLE_DOC_MIME_TYPE)

        with self.assertRaises(HttpError):
            docs_edit.get(DOC_ID)


if __name__ == "__main__":
    unittest.main()
