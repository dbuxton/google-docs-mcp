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


def _thread(comment_id: str, status: str = "OPEN", replies: list | None = None) -> dict:
    return {
        "commentId": comment_id,
        "status": status,
        "anchorId": f"anchor-{comment_id}",
        "plainTextQuote": "one month",
        "headPost": {
            "postId": f"{comment_id}-head",
            "content": f"Comment {comment_id}",
            "author": {"displayName": "Dana"},
            "createTime": "2026-10-09T10:00:00Z",
            "assigneeEmail": "hr@example.com",
        },
        "replies": replies or [],
    }


class CommentTest(unittest.TestCase):
    """Comments go through the Docs API's native comment requests, not Drive."""

    def setUp(self):
        self.docs = mock.MagicMock()
        self.docs.documents().get().execute.return_value = {
            "title": "Contract",
            "body": {
                "content": [
                    {
                        "startIndex": 1,
                        "endIndex": 26,
                        "paragraph": {
                            "elements": [
                                {
                                    "startIndex": 1,
                                    "endIndex": 26,
                                    "textRun": {"content": "Notice period: one month\n"},
                                }
                            ]
                        },
                    }
                ]
            },
        }
        self.batch_update = self.docs.documents().batchUpdate
        self.drive = mock.MagicMock()
        # read_comments asks Drive what the file is before choosing where its comments live.
        self.drive.files.return_value.get.return_value.execute.return_value = {
            "name": "Contract",
            "mimeType": docs_edit.GOOGLE_DOC_MIME_TYPE,
        }
        services = {"docs": self.docs, "drive": self.drive}
        patcher = mock.patch.object(
            docs_edit, "_get_service", side_effect=lambda api, version: services[api]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sent_requests(self) -> list:
        return self.batch_update.call_args.kwargs["body"]["requests"]

    def test_add_comment_anchors_to_matched_text(self):
        self.batch_update().execute.return_value = {
            "replies": [{"insertComment": {"commentThread": {"commentId": "c1"}}}]
        }

        result = docs_edit.add_comment(DOC_ID, "Too short?", "one month")

        self.assertEqual(
            self._sent_requests(),
            [
                {
                    "insertComment": {
                        "range": {"startIndex": 16, "endIndex": 25},
                        "content": "Too short?",
                    }
                }
            ],
        )
        self.assertEqual(result["comment_id"], "c1")
        self.assertEqual(result["anchored_to"], "one month")
        self.assertEqual(result["at_index"], 16)
        self.drive.comments.assert_not_called()

    def test_add_comment_can_assign_someone(self):
        self.batch_update().execute.return_value = {
            "replies": [{"insertComment": {"commentThread": {"commentId": "c1"}}}]
        }

        docs_edit.add_comment(
            DOC_ID, "Please confirm", "Notice", assignee_email="hr@example.com"
        )

        self.assertEqual(
            self._sent_requests()[0]["insertComment"]["assigneeEmailAddress"],
            "hr@example.com",
        )

    def test_read_comments_returns_open_threads_with_quoted_text(self):
        self.docs.documents().get().execute.return_value = {
            "comments": [
                _thread(
                    "c1",
                    replies=[
                        {
                            "postId": "r1",
                            "content": "Agreed",
                            "author": {"displayName": "Sam"},
                            "createTime": "2026-10-09T11:00:00Z",
                        }
                    ],
                ),
                _thread("c2", status="RESOLVED"),
            ]
        }

        result = docs_edit.read_comments(DOC_ID)

        self.docs.documents().get.assert_called_with(
            documentId=DOC_ID,
            suggestionsViewMode="SUGGESTIONS_INLINE",
            commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED",
            includeTabsContent=True,
            fields="comments",
        )
        self.assertEqual(result["count"], 1)
        comment = result["comments"][0]
        self.assertEqual(comment["id"], "c1")
        self.assertEqual(comment["content"], "Comment c1")
        self.assertEqual(comment["quoted_text"], "one month")
        self.assertEqual(comment["assignee"], "hr@example.com")
        self.assertFalse(comment["resolved"])
        self.assertEqual(
            comment["replies"],
            [{"id": "r1", "author": "Sam", "content": "Agreed", "created": "2026-10-09T11:00:00Z"}],
        )

    def test_read_comments_can_include_resolved(self):
        self.docs.documents().get().execute.return_value = {
            "comments": [_thread("c1"), _thread("c2", status="RESOLVED")]
        }

        result = docs_edit.read_comments(DOC_ID, include_resolved=True)

        self.assertEqual([c["id"] for c in result["comments"]], ["c1", "c2"])
        self.assertTrue(result["comments"][1]["resolved"])

    def test_reply_to_comment(self):
        self.batch_update().execute.return_value = {
            "replies": [{"addCommentReply": {"post": {"postId": "r9", "content": "Done"}}}]
        }

        result = docs_edit.reply_to_comment(DOC_ID, "c1", "Done")

        self.assertEqual(
            self._sent_requests(),
            [{"addCommentReply": {"commentId": "c1", "post": {"content": "Done"}}}],
        )
        self.assertEqual(result["reply_id"], "r9")

    def test_resolve_comment_with_final_reply_is_one_post(self):
        self.batch_update().execute.return_value = {
            "replies": [{"addCommentReply": {"post": {"postId": "r9"}}}]
        }

        docs_edit.resolve_comment(DOC_ID, "c1", "Fixed in v2")

        self.assertEqual(
            self._sent_requests(),
            [
                {
                    "addCommentReply": {
                        "commentId": "c1",
                        "post": {"commentAction": "RESOLVE", "content": "Fixed in v2"},
                    }
                }
            ],
        )

    def test_resolve_comment_without_reply(self):
        self.batch_update().execute.return_value = {
            "replies": [{"addCommentReply": {"post": {"postId": "r9"}}}]
        }

        docs_edit.resolve_comment(DOC_ID, "c1")

        self.assertEqual(
            self._sent_requests()[0]["addCommentReply"]["post"], {"commentAction": "RESOLVE"}
        )

    def test_delete_comment(self):
        self.batch_update().execute.return_value = {"replies": [{}]}

        docs_edit.delete_comment(DOC_ID, "c1")

        self.assertEqual(self._sent_requests(), [{"deleteComment": {"commentId": "c1"}}])


if __name__ == "__main__":
    unittest.main()
