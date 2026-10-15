"""
Comments on files the Docs API refuses (Word files in Office editing mode, PDFs).

On a real Google Doc the Docs API is used, because Docs only shows those comments as
anchored. Anywhere else the comment tools fall back to Drive comments on the file.
"""

import unittest

from googleapiclient.errors import HttpError

import docs_edit
from tests.helpers import DOC_ID, DocBuilder, DocsTestCase, http_error

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OFFICE_FILE_ERROR = (
    "This operation is not supported for this document. The document must not be an Office file."
)
BARE_400 = "Request contains an invalid argument."


def _docs_thread(comment_id: str) -> dict:
    return {
        "commentId": comment_id,
        "status": "OPEN",
        "plainTextQuote": "one month",
        "headPost": {"content": f"Comment {comment_id}", "author": {"displayName": "Dana"}},
        "replies": [],
    }


class OfficeFileCommentsTest(DocsTestCase):
    """A .docx opened in Drive's Office editing mode: the Docs API answers every write with a 400."""

    def setUp(self):
        super().setUp()
        self.drive_file("MSA.docx", DOCX)
        self.batch_update.return_value.execute.side_effect = http_error(400, OFFICE_FILE_ERROR)
        self.docs.documents().get().execute.side_effect = http_error(400, BARE_400)

    def drive_comments(self):
        return self.drive.comments()

    def drive_replies(self):
        return self.drive.replies()

    # --- writing -------------------------------------------------------------

    def test_reply_becomes_a_drive_reply(self):
        self.drive_replies().create.return_value.execute.return_value = {"id": "r1", "content": "Agreed"}

        result = docs_edit.reply_to_comment(DOC_ID, "c1", "Agreed")

        self.drive_replies().create.assert_called_once_with(
            fileId=DOC_ID, commentId="c1", body={"content": "Agreed"}, fields="id,content"
        )
        self.assertEqual(
            result,
            {"ok": True, "reply_id": "r1", "comment_id": "c1", "content": "Agreed", "source": "drive"},
        )

    def test_resolve_becomes_a_drive_reply_that_resolves(self):
        result = docs_edit.resolve_comment(DOC_ID, "c1", "Fixed in v2")

        self.drive_replies().create.assert_called_once_with(
            fileId=DOC_ID,
            commentId="c1",
            body={"action": "resolve", "content": "Fixed in v2"},
            fields="id,action",
        )
        self.assertEqual(
            result, {"ok": True, "comment_id": "c1", "resolved": True, "source": "drive"}
        )

    def test_resolve_without_a_reply_sends_no_content(self):
        docs_edit.resolve_comment(DOC_ID, "c1")

        self.assertEqual(
            self.drive_replies().create.call_args.kwargs["body"], {"action": "resolve"}
        )

    def test_delete_becomes_a_drive_delete(self):
        result = docs_edit.delete_comment(DOC_ID, "c1")

        self.drive_comments().delete.assert_called_once_with(fileId=DOC_ID, commentId="c1")
        self.drive_comments().delete.return_value.execute.assert_called_once_with()
        self.assertEqual(result, {"ok": True, "comment_id": "c1", "source": "drive"})

    def test_add_comment_is_posted_on_the_file_with_the_quoted_text(self):
        self.drive_comments().create.return_value.execute.return_value = {"id": "c9"}

        result = docs_edit.add_comment(DOC_ID, "Check the term", "Initial Term")

        self.drive_comments().create.assert_called_once_with(
            fileId=DOC_ID,
            body={"content": 'Re: "Initial Term"\n\nCheck the term'},
            fields="id,content",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["comment_id"], "c9")
        self.assertFalse(result["anchored"])
        self.assertIsNone(result["anchored_to"])
        self.assertIsNone(result["at_index"])
        self.assertEqual(result["source"], "drive")
        self.assertIn('"MSA.docx"', result["note"])
        self.assertIn("is a Word document (.docx), not a Google Doc", result["note"])

    def test_add_comment_still_falls_back_when_only_the_write_is_refused(self):
        # documents.get may succeed on the file while batchUpdate refuses it.
        self.docs.documents().get().execute.side_effect = None
        self.docs.documents().get().execute.return_value = (
            DocBuilder().paragraph("Initial Term: 12 months").build()
        )
        self.drive_comments().create.return_value.execute.return_value = {"id": "c9"}

        result = docs_edit.add_comment(DOC_ID, "Check the term", "Initial Term")

        self.batch_update.assert_called_once()
        self.assertEqual(result["source"], "drive")
        self.assertEqual(result["comment_id"], "c9")

    def test_add_comment_notes_the_occurrence_and_the_assignee(self):
        self.drive_comments().create.return_value.execute.return_value = {"id": "c9"}

        result = docs_edit.add_comment(
            DOC_ID, "Please confirm", "Term", occurrence=2, assignee_email="hr@example.com"
        )

        self.assertEqual(
            self.drive_comments().create.call_args.kwargs["body"]["content"],
            'Re: "Term" (occurrence 2)\n\n+hr@example.com Please confirm',
        )
        self.assertIn("cannot be assigned", result["assignee_note"])

    def test_a_long_quote_is_shortened_and_whitespace_collapsed(self):
        self.drive_comments().create.return_value.execute.return_value = {"id": "c9"}

        docs_edit.add_comment(DOC_ID, "Too long", "word  " * 100)

        content = self.drive_comments().create.call_args.kwargs["body"]["content"]
        heading = content.split("\n")[0]
        self.assertTrue(heading.endswith('..."'), heading)
        self.assertLess(len(heading), 220)
        self.assertNotIn("  ", heading)

    # --- reading -------------------------------------------------------------

    def drive_thread(self, comment_id, **overrides):
        return {
            "id": comment_id,
            "content": f"Comment {comment_id}",
            "author": {"displayName": "Dana"},
            "createdTime": "2026-10-09T10:00:00Z",
            "resolved": False,
            "quotedFileContent": {"value": "one month"},
            "replies": [],
            **overrides,
        }

    def test_read_comments_lists_drive_comments_in_the_usual_shape(self):
        reply = {
            "id": "r1",
            "content": "Agreed",
            "author": {"displayName": "Sam"},
            "createdTime": "2026-10-09T11:00:00Z",
        }
        self.drive_comments().list.return_value.execute.side_effect = [
            {"comments": [self.drive_thread("c1", replies=[reply])], "nextPageToken": "p2"},
            {"comments": [self.drive_thread("c2", resolved=True), self.drive_thread("c3")]},
        ]

        result = docs_edit.read_comments(DOC_ID)

        pages = self.drive_comments().list.call_args_list
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0].kwargs["fileId"], DOC_ID)
        self.assertEqual(pages[0].kwargs["includeDeleted"], False)
        self.assertIsNone(pages[0].kwargs["pageToken"])
        self.assertEqual(pages[1].kwargs["pageToken"], "p2")
        self.assertIn("replies(", pages[0].kwargs["fields"])
        self.assertEqual(result["source"], "drive")
        self.assertEqual(result["count"], 2)
        self.assertEqual([c["id"] for c in result["comments"]], ["c1", "c3"])
        self.assertEqual(
            result["comments"][0],
            {
                "id": "c1",
                "content": "Comment c1",
                "author": "Dana",
                "quoted_text": "one month",
                "assignee": None,
                "resolved": False,
                "created": "2026-10-09T10:00:00Z",
                "origin": "drive",
                "replies": [
                    {"id": "r1", "author": "Sam", "content": "Agreed", "created": "2026-10-09T11:00:00Z"}
                ],
            },
        )
        self.assertIn('"MSA.docx"', result["note"])

    def test_read_comments_can_include_resolved_and_shows_resolving_replies(self):
        resolving = {"id": "r2", "action": "resolve", "author": {"displayName": "Sam"}}
        removed = {"id": "r3", "deleted": True}
        self.drive_comments().list.return_value.execute.return_value = {
            "comments": [
                self.drive_thread("c2", resolved=True, replies=[resolving, removed]),
                self.drive_thread("c4", deleted=True),
            ]
        }

        result = docs_edit.read_comments(DOC_ID, include_resolved=True)

        self.assertEqual([c["id"] for c in result["comments"]], ["c2"])
        self.assertTrue(result["comments"][0]["resolved"])
        self.assertEqual(
            result["comments"][0]["replies"],
            [{"id": "r2", "author": "Sam", "content": "", "created": "", "action": "resolve"}],
        )

    # --- other file types ----------------------------------------------------

    def test_pdfs_take_drive_comments_too(self):
        self.drive_file("Terms.pdf", "application/pdf")
        self.drive_comments().create.return_value.execute.return_value = {"id": "c9"}

        result = docs_edit.add_comment(DOC_ID, "Unclear", "Fees")

        self.assertEqual(result["source"], "drive")
        self.assertIn('"Terms.pdf" is a PDF', result["note"])

    def test_editing_tools_still_refuse_the_file_but_point_to_the_comment_tools(self):
        with self.assertRaisesRegex(
            docs_edit.NotAGoogleDocError, '"MSA.docx" is .* not a Google Doc.*comment tools'
        ):
            docs_edit.search_replace(DOC_ID, "old", "new")

    def test_editing_tools_explain_a_refusal_that_only_comes_at_write_time(self):
        # documents.get can succeed on the file while batchUpdate refuses it, which used to
        # surface as the bare 400 instead of saying what the file is.
        self.docs.documents().get().execute.side_effect = None
        self.docs.documents().get().execute.return_value = (
            DocBuilder().paragraph("Initial Term: 12 months").build()
        )
        edits = {
            "search_replace": lambda suggest: docs_edit.search_replace(
                DOC_ID, "12 months", "24 months", suggest=suggest
            ),
            "insert_after": lambda suggest: docs_edit.insert_after(DOC_ID, "Initial", "x", suggest=suggest),
            "append": lambda suggest: docs_edit.append(DOC_ID, "x", suggest=suggest),
            "delete_paragraph": lambda suggest: docs_edit.delete_paragraph(DOC_ID, "Initial", suggest=suggest),
            "review_suggestions": lambda suggest: docs_edit.review_suggestions(DOC_ID, "accept", ["s1"]),
        }
        for name, edit in edits.items():
            for suggest in (False, True):
                with self.subTest(name, suggest=suggest):
                    with self.assertRaisesRegex(
                        docs_edit.NotAGoogleDocError, '"MSA.docx" is .* not a Google Doc'
                    ):
                        edit(suggest)


class WordFileReadsButRefusesWritesTest(DocsTestCase):
    """
    What the real API does with a .docx: documents.get reads it without complaint (and shows no comments,
    whatever Drive holds), while every write, comments included, is refused as an Office file.
    """

    def setUp(self):
        super().setUp()
        self.drive_file("MSA.docx", DOCX)
        self.serve(DocBuilder().paragraph("Initial Term: 12 months").build())
        self.batch_update.return_value.execute.side_effect = http_error(400, OFFICE_FILE_ERROR)

    def test_the_text_still_reads_through_the_docs_api(self):
        self.assertEqual(docs_edit.get(DOC_ID)["plain_text"], "Initial Term: 12 months")

    def test_reading_comments_looks_in_drive_because_the_docs_api_shows_none(self):
        self.drive.comments.return_value.list.return_value.execute.return_value = {
            "comments": [
                {
                    "id": "c1",
                    "content": 'Re: "Initial Term"\n\nIs 12 months right?',
                    "author": {"displayName": "Dana"},
                    "createdTime": "2026-10-15T19:45:00Z",
                    "resolved": False,
                    "replies": [],
                }
            ]
        }

        result = docs_edit.read_comments(DOC_ID)

        self.assertEqual(result["source"], "drive")
        self.assertEqual([c["id"] for c in result["comments"]], ["c1"])
        self.assertIn('"MSA.docx"', result["note"])

    def test_a_word_file_with_no_comments_anywhere_reads_as_empty_from_drive(self):
        self.drive.comments.return_value.list.return_value.execute.return_value = {}

        result = docs_edit.read_comments(DOC_ID)

        self.assertEqual((result["count"], result["source"]), (0, "drive"))

    def test_comments_saved_inside_the_word_file_are_listed_beside_the_drive_ones(self):
        embedded = _docs_thread("w1")
        embedded["headPost"]["content"] = "Can we shorten the term?"
        self.serve({"comments": [embedded]})
        self.drive.comments.return_value.list.return_value.execute.return_value = {
            "comments": [{"id": "c1", "content": "Our own note.", "author": {"displayName": "Dana"}}]
        }

        result = docs_edit.read_comments(DOC_ID)

        self.assertEqual(
            [(c["id"], c["origin"]) for c in result["comments"]], [("w1", "embedded"), ("c1", "drive")]
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["source"], "drive")
        self.assertIn("saved inside the file", result["note"])

    def test_embedded_comments_cannot_be_replied_to_resolved_or_deleted_and_the_error_says_why(self):
        for call in (
            lambda: docs_edit.reply_to_comment(DOC_ID, "w1", "Noted"),
            lambda: docs_edit.resolve_comment(DOC_ID, "w1"),
            lambda: docs_edit.delete_comment(DOC_ID, "w1"),
        ):
            self.drive.replies.return_value.create.return_value.execute.side_effect = http_error(404, "Comment not found: w1.")
            self.drive.comments.return_value.delete.return_value.execute.side_effect = http_error(404, "Comment not found: w1.")
            with self.assertRaisesRegex(docs_edit.EmbeddedCommentError, 'not a Drive comment on "MSA.docx".*Convert the file to a Google Doc'):
                call()

    def test_a_drive_error_other_than_not_found_is_left_alone(self):
        self.drive.replies.return_value.create.return_value.execute.side_effect = http_error(403, "Forbidden")

        with self.assertRaises(HttpError):
            docs_edit.reply_to_comment(DOC_ID, "c1", "Noted")

    def test_every_write_is_explained_or_rerouted(self):
        self.drive.comments.return_value.create.return_value.execute.return_value = {"id": "c9"}
        self.drive.replies.return_value.create.return_value.execute.return_value = {"id": "r1", "content": "ok"}

        self.assertEqual(docs_edit.add_comment(DOC_ID, "Check", "Initial Term")["source"], "drive")
        self.assertEqual(docs_edit.reply_to_comment(DOC_ID, "c9", "ok")["source"], "drive")
        self.assertEqual(docs_edit.resolve_comment(DOC_ID, "c9")["source"], "drive")
        self.assertEqual(docs_edit.delete_comment(DOC_ID, "c9")["source"], "drive")
        with self.assertRaisesRegex(docs_edit.NotAGoogleDocError, "cannot edit it"):
            docs_edit.search_replace(DOC_ID, "12 months", "24 months")


class FileTypeNamesTest(unittest.TestCase):
    def test_common_office_files_are_named_plainly_and_others_by_mime_type(self):
        cases = {
            DOCX: "a Word document (.docx)",
            "application/msword": "a Word document (.doc)",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "an Excel spreadsheet (.xlsx)",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": "a PowerPoint file (.pptx)",
            "application/pdf": "a PDF",
            "image/png": "a file of type image/png",
        }
        for mime_type, expected in cases.items():
            with self.subTest(mime_type):
                error = docs_edit.NotAGoogleDocError(DOC_ID, "File", mime_type, None)

                self.assertEqual(error.kind, expected)
                self.assertIn(f'"File" is {expected}, not a Google Doc', str(error))


class RealGoogleDocsKeepTheDocsApiTest(DocsTestCase):
    """Drive comments read "Original content deleted" in Docs, so a real Doc must not get them."""

    def setUp(self):
        super().setUp()
        self.drive_file("Contract", docs_edit.GOOGLE_DOC_MIME_TYPE)

    def assert_drive_comments_untouched(self):
        self.drive.comments.assert_not_called()
        self.drive.replies.assert_not_called()

    def test_working_requests_never_ask_drive_anything(self):
        self.reply({"replies": [{"addCommentReply": {"post": {"postId": "r1"}}}, {}]})

        docs_edit.reply_to_comment(DOC_ID, "c1", "Done")
        docs_edit.resolve_comment(DOC_ID, "c1")
        docs_edit.delete_comment(DOC_ID, "c1")

        self.drive.files.assert_not_called()
        self.assert_drive_comments_untouched()

    def test_reading_comments_checks_the_file_type_once_and_stays_on_the_docs_api(self):
        self.serve({"comments": [_docs_thread("c1")]})

        result = docs_edit.read_comments(DOC_ID)

        self.assertEqual([c["id"] for c in result["comments"]], ["c1"])
        self.assertNotIn("source", result)
        self.assertNotIn("origin", result["comments"][0])
        self.drive.files.return_value.get.assert_called_once()
        self.assert_drive_comments_untouched()

    def test_a_real_doc_with_no_comments_reads_as_empty_from_the_docs_api(self):
        self.serve({})

        result = docs_edit.read_comments(DOC_ID)

        self.assertEqual(result, {"count": 0, "comments": []})
        self.assert_drive_comments_untouched()

    def test_a_400_that_is_about_something_else_is_not_rerouted(self):
        self.batch_update.return_value.execute.side_effect = http_error(400, "Invalid commentId")

        with self.assertRaises(HttpError):
            docs_edit.reply_to_comment(DOC_ID, "nope", "Done")

        self.assert_drive_comments_untouched()

    def test_errors_other_than_400_are_not_rerouted_or_investigated(self):
        self.batch_update.return_value.execute.side_effect = http_error(403, "Forbidden")

        with self.assertRaises(HttpError) as caught:
            docs_edit.delete_comment(DOC_ID, "c1")

        self.assertEqual(caught.exception.resp.status, 403)
        self.drive.files.assert_not_called()
        self.assert_drive_comments_untouched()


if __name__ == "__main__":
    unittest.main()
