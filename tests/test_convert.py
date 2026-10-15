"""Making a Google Doc from a Word, text or PDF file, so every other tool works on it."""

import unittest

from googleapiclient.errors import HttpError

import docs_edit
from tests.helpers import DOC_ID, DocsTestCase, http_error

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SHEET = "application/vnd.google-apps.spreadsheet"
NEW_ID = "1newCopyId"


class ConvertToGoogleDocTest(DocsTestCase):
    def setUp(self):
        super().setUp()
        self.copy = self.drive.files.return_value.copy
        self.copy.return_value.execute.return_value = {
            "id": NEW_ID,
            "name": "MSA",
            "webViewLink": f"https://docs.google.com/document/d/{NEW_ID}/edit",
        }

    def test_a_word_file_is_copied_as_a_google_doc(self):
        self.drive_file("MSA.docx", DOCX)

        result = docs_edit.convert_to_google_doc(DOC_ID)

        self.drive.files.return_value.get.assert_called_once_with(
            fileId=DOC_ID, fields="id,name,mimeType", supportsAllDrives=True
        )
        self.copy.assert_called_once_with(
            fileId=DOC_ID,
            body={"name": "MSA", "mimeType": docs_edit.GOOGLE_DOC_MIME_TYPE},
            supportsAllDrives=True,
            fields="id,name,webViewLink",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["converted"])
        self.assertEqual(result["id"], NEW_ID)
        self.assertEqual(result["name"], "MSA")
        self.assertEqual(result["webViewLink"], f"https://docs.google.com/document/d/{NEW_ID}/edit")
        self.assertEqual(
            result["converted_from"], {"id": DOC_ID, "name": "MSA.docx", "type": "a Word document (.docx)"}
        )
        self.assertIn("suggestions", result["note"])
        self.assertIn("comments", result["note"])

    def test_the_copy_is_named_like_drives_own_save_as_google_docs(self):
        cases = {
            "MSA.docx": "MSA",
            "Contract v2.DOC": "Contract v2",
            "notes.final.docx": "notes.final",
            "Terms.pdf": "Terms",
            "Template.dotx": "Template",
            "README": "README",
            ".docx": ".docx",  # never an empty title
            "Order form - safe to delete": "Order form - safe to delete",
        }
        for original, expected in cases.items():
            with self.subTest(original):
                self.drive_file(original, DOCX)

                docs_edit.convert_to_google_doc(DOC_ID)

                self.assertEqual(self.copy.call_args.kwargs["body"]["name"], expected)

    def test_an_explicit_name_wins(self):
        self.drive_file("MSA.docx", DOCX)

        docs_edit.convert_to_google_doc(DOC_ID, "MSA - working copy")

        self.assertEqual(self.copy.call_args.kwargs["body"]["name"], "MSA - working copy")

    def test_a_file_that_is_already_a_google_doc_is_returned_untouched(self):
        self.drive_file("Contract", docs_edit.GOOGLE_DOC_MIME_TYPE)

        result = docs_edit.convert_to_google_doc(DOC_ID)

        self.assertEqual(result["id"], DOC_ID)
        self.assertFalse(result["converted"])
        self.assertIn("Already a Google Doc", result["note"])
        self.copy.assert_not_called()

    def test_files_that_cannot_be_a_google_doc_are_refused_before_anything_is_copied(self):
        for mime_type in (SHEET, "image/png", "application/vnd.google-apps.folder",
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
            with self.subTest(mime_type):
                self.drive_file("Thing", mime_type)

                with self.assertRaisesRegex(ValueError, '"Thing" is .* cannot be converted to a Google Doc.*Word, RTF'):
                    docs_edit.convert_to_google_doc(DOC_ID)

        self.copy.assert_not_called()

    def test_every_convertible_type_is_accepted(self):
        for mime_type in sorted(docs_edit.CONVERTIBLE_MIME_TYPES):
            with self.subTest(mime_type):
                self.copy.reset_mock()
                self.drive_file("Thing", mime_type)

                self.assertTrue(docs_edit.convert_to_google_doc(DOC_ID)["converted"])
                self.copy.assert_called_once()

    def test_a_pdf_comes_with_a_warning_about_layout(self):
        self.drive_file("Terms.pdf", "application/pdf")

        result = docs_edit.convert_to_google_doc(DOC_ID)

        self.assertIn("text recognition", result["note"])
        self.assertIn("docs_get", result["note"])
        self.assertEqual(result["converted_from"]["type"], "a PDF")

    def test_a_link_is_built_when_drive_sends_none(self):
        self.drive_file("MSA.docx", DOCX)
        self.copy.return_value.execute.return_value = {"id": NEW_ID, "name": "MSA"}

        result = docs_edit.convert_to_google_doc(DOC_ID)

        self.assertEqual(result["webViewLink"], f"https://docs.google.com/document/d/{NEW_ID}/edit")

    def test_drive_errors_reach_the_caller(self):
        self.drive_file("MSA.docx", DOCX)
        self.copy.return_value.execute.side_effect = http_error(403, "The user does not have sufficient permissions")
        with self.assertRaises(HttpError):
            docs_edit.convert_to_google_doc(DOC_ID)

        self.drive.files.return_value.get.return_value.execute.side_effect = http_error(404, "File not found")
        with self.assertRaises(HttpError):
            docs_edit.convert_to_google_doc(DOC_ID)


class HintsInTheNotAGoogleDocErrorsTest(unittest.TestCase):
    def test_files_that_can_be_converted_are_pointed_to_the_tool(self):
        word = str(docs_edit.NotAGoogleDocError(DOC_ID, "MSA.docx", DOCX, None))
        pdf = str(docs_edit.NotAGoogleDocError(DOC_ID, "Terms.pdf", "application/pdf", None))

        self.assertIn("docs_convert_to_google_doc makes an editable Google Doc copy", word)
        self.assertIn("docs_convert_to_google_doc can make an approximate, editable copy", pdf)

    def test_files_that_cannot_be_converted_are_not(self):
        sheet = str(docs_edit.NotAGoogleDocError(DOC_ID, "Budget", SHEET, None))

        self.assertNotIn("docs_convert_to_google_doc", sheet)


if __name__ == "__main__":
    unittest.main()
