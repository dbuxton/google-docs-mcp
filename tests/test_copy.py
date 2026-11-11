"""Duplicating a doc (File > Make a copy), so a template can be filled in without being changed."""

import unittest

import docs_edit
from tests.helpers import DOC_ID, DocsTestCase

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
NEW_ID = "1newCopyId"


class CopyDocTest(DocsTestCase):
    def setUp(self):
        super().setUp()
        self.copy = self.drive.files.return_value.copy
        self.copy.return_value.execute.return_value = {
            "id": NEW_ID,
            "name": "Copy of Order Form",
            "webViewLink": f"https://docs.google.com/document/d/{NEW_ID}/edit",
        }
        self.drive_file("Order Form", docs_edit.GOOGLE_DOC_MIME_TYPE)

    def test_a_google_doc_is_copied_with_drives_default_name_and_place(self):
        result = docs_edit.copy_doc(DOC_ID)

        self.drive.files.return_value.get.assert_called_once_with(
            fileId=DOC_ID, fields="id,name,mimeType", supportsAllDrives=True
        )
        # No mimeType (that would convert), and no parents (Drive then keeps the original's).
        self.copy.assert_called_once_with(
            fileId=DOC_ID,
            body={"name": "Copy of Order Form"},
            supportsAllDrives=True,
            fields="id,name,webViewLink",
        )
        self.assertEqual(
            result,
            {
                "ok": True,
                "id": NEW_ID,
                "name": "Copy of Order Form",
                "webViewLink": f"https://docs.google.com/document/d/{NEW_ID}/edit",
                "copied_from": {"id": DOC_ID, "name": "Order Form", "type": "a Google Doc"},
                "note": result["note"],
            },
        )
        self.assertIn("beside the original", result["note"])
        self.assertIn("original is unchanged", result["note"])

    def test_the_copy_can_be_named_and_put_in_a_folder(self):
        self.copy.return_value.execute.return_value["name"] = "OysterHR Order Form"

        result = docs_edit.copy_doc(DOC_ID, "OysterHR Order Form", "folder42")

        self.copy.assert_called_once_with(
            fileId=DOC_ID,
            body={"name": "OysterHR Order Form", "parents": ["folder42"]},
            supportsAllDrives=True,
            fields="id,name,webViewLink",
        )
        self.assertEqual(result["name"], "OysterHR Order Form")
        self.assertIn("folder you named", result["note"])

    def test_a_word_file_is_copied_as_a_word_file_not_converted(self):
        self.drive_file("MSA.docx", DOCX)

        result = docs_edit.copy_doc(DOC_ID)

        self.assertNotIn("mimeType", self.copy.call_args.kwargs["body"])
        self.assertEqual(result["copied_from"]["type"], "a Word document (.docx)")

    def test_a_folder_is_refused_before_anything_is_copied(self):
        self.drive_file("Contracts", "application/vnd.google-apps.folder")

        with self.assertRaisesRegex(ValueError, "folder"):
            docs_edit.copy_doc(DOC_ID)

        self.copy.assert_not_called()

    def test_the_link_is_built_when_drive_leaves_it_out(self):
        self.copy.return_value.execute.return_value = {"id": NEW_ID, "name": "X"}

        result = docs_edit.copy_doc(DOC_ID, "X")

        self.assertEqual(result["webViewLink"], f"https://docs.google.com/document/d/{NEW_ID}/edit")


if __name__ == "__main__":
    unittest.main()
