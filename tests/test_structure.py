"""Advanced edits preserve native API structures, locations, revisions and responses."""

import copy
import unittest
from unittest import mock

from googleapiclient.errors import HttpError

import docs_edit
from tests.helpers import (
    DOC_ID, DocBuilder, DocsTestCase, deleted, http_error, inserted, saved_as_suggestion,
)


def tabbed(doc: dict, tab_id: str = "t.main", **extra) -> dict:
    return {
        "documentId": DOC_ID,
        "revisionId": "rev.1",
        "tabs": [{"tabProperties": {"tabId": tab_id}, "documentTab": doc}],
        **extra,
    }


class RawStructureTest(DocsTestCase):
    def test_get_preserves_all_structure_and_inline_suggestions(self):
        body = DocBuilder().paragraph("😀", deleted("old", "s1"), inserted("new", "s1")).build()
        body.update({
            "headers": {"h1": {"content": [{"paragraph": {"elements": []}}]}},
            "footers": {"f1": {"content": []}},
            "footnotes": {"fn1": {"content": []}},
            "inlineObjects": {"img1": {"inlineObjectProperties": {"embeddedObject": {}}}},
            "namedRanges": {"Name": {"namedRanges": [{"namedRangeId": "n1"}]}},
        })
        doc = tabbed(body)
        doc["tabs"][0]["childTabs"] = tabbed(DocBuilder().table([["Child"]]).build(), "t.child")["tabs"]
        self.serve(doc)
        before = copy.deepcopy(doc)

        self.assertEqual(docs_edit.get_raw(DOC_ID), before)
        self.assertEqual(doc, before)
        self.docs.documents().get.assert_called_once_with(
            documentId=DOC_ID, includeTabsContent=True, suggestionsViewMode="SUGGESTIONS_INLINE",
        )

    def test_field_mask_keeps_revision_metadata(self):
        self.serve({"revisionId": "r1"})
        docs_edit.get_raw(DOC_ID, " tabs(documentTab(headers)),title ")
        self.assertEqual(
            self.docs.documents().get.call_args.kwargs["fields"],
            "documentId,revisionId,tabs(documentTab(headers)),title",
        )

    def test_batch_preserves_native_requests_order_and_complete_response(self):
        requests = [
            {"insertText": {"location": {"index": 0, "tabId": "t.child", "segmentId": "h1"}, "text": "Header"}},
            {"updateDocumentTabProperties": {"tabProperties": {"tabId": "t.child", "title": "Costs"}, "fields": "title"}},
            {"createFooter": {"type": "DEFAULT", "sectionBreakLocation": {"index": 0, "tabId": "t.child"}}},
        ]
        response = {"documentId": DOC_ID, "replies": [{}, {}, {"createFooter": {"footerId": "f2"}}],
                    "writeControl": {"requiredRevisionId": "rev.2"}}
        self.reply(response)
        before = copy.deepcopy(requests)

        result = docs_edit.advanced_edit(DOC_ID, requests, "rev.1")

        self.assertEqual(result["response"], response)
        self.assertEqual(result["request_count"], 3)
        self.assertEqual(self.body(), {"requests": before, "writeControl": {"requiredRevisionId": "rev.1"}})
        self.assertEqual(requests, before)
        self.batch_update.assert_called_once()
        self.docs.documents().get.assert_not_called()

    def test_dry_run_requires_no_credentials_and_does_not_claim_api_validation(self):
        # New Google request types must not be rejected by a stale local allowlist.
        requests = [{"futureRequest": {"newField": 42}}]
        with mock.patch.object(docs_edit, "_get_service") as service:
            result = docs_edit.advanced_edit(DOC_ID, requests, "rev.1", suggest=True, dry_run=True)
        service.assert_not_called()
        self.assertEqual(result["body"]["requests"], requests)
        self.assertEqual(result["body"]["writeControl"], {"requiredRevisionId": "rev.1", "writeMode": "SUGGEST"})
        self.assertIn("not been validated", result["note"])
        self.assertNotIn("suggested", result)

    def test_invalid_envelopes_and_missing_revision_do_not_write(self):
        bad = [None, {}, [], "[]", [None], [[]], [{}], [{"a": {}, "b": {}}],
               [{"insertText": "text"}], [{"": {}}], [{"   ": {}}]]
        for requests in bad:
            with self.subTest(requests=requests), self.assertRaises(ValueError):
                docs_edit.advanced_edit(DOC_ID, requests, "rev.1")
        for revision in ("", "  ", None, 1):
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "revisionId"):
                docs_edit.advanced_edit(DOC_ID, [{"createHeader": {"type": "DEFAULT"}}], revision)
        self.batch_update.assert_not_called()

    def test_suggestions_keep_revision_and_verify_google_confirmation(self):
        self.reply(saved_as_suggestion(["s1"]))
        result = docs_edit.advanced_edit(DOC_ID, [{"insertTable": {"rows": 2, "columns": 2}}], "rev.1", suggest=True)
        self.assertEqual(result["suggestion_ids"], ["s1"])
        self.assertEqual(self.body()["writeControl"], {"requiredRevisionId": "rev.1", "writeMode": "SUGGEST"})

        self.reply({"replies": [{}], "commentUpdateState": "ALL_FAILED_UNKNOWN_REASON"})
        with self.assertRaises(docs_edit.SuggestionNotSavedError):
            docs_edit.advanced_edit(DOC_ID, [{"insertTable": {"rows": 2, "columns": 2}}], "rev.1", suggest=True)
        self.assertEqual(self.batch_update.call_count, 2)  # No direct-edit fallback.

    def test_revision_conflict_is_not_retried_or_replaced(self):
        failure = http_error(400, "The provided revision ID does not match the current revision")
        self.batch_update.return_value.execute.side_effect = failure
        self.drive_file("Contract", docs_edit.GOOGLE_DOC_MIME_TYPE)
        with self.assertRaises(HttpError) as caught:
            docs_edit.advanced_edit(DOC_ID, [{"deleteTab": {"tabId": "t2"}}], "old-rev")
        self.assertIs(caught.exception, failure)
        self.batch_update.assert_called_once()
        self.docs.documents().get.assert_not_called()

    def test_deleting_suggested_structures_can_remove_an_existing_suggestion(self):
        self.reply({"replies": [{}], "commentUpdateState": "ALL_SAVED",
                    "suggestionResponses": [{"deletedSuggestionIds": ["s1"]}]})
        for kind, payload in (
            ("deleteTableRow", {"tableCellLocation": {"tableStartLocation": {"index": 10}, "rowIndex": 0, "columnIndex": 0}}),
            ("deleteTableColumn", {"tableCellLocation": {"tableStartLocation": {"index": 10}, "rowIndex": 0, "columnIndex": 0}}),
            ("deletePositionedObject", {"objectId": "img1"}),
        ):
            with self.subTest(kind=kind):
                result = docs_edit.advanced_edit(DOC_ID, [{kind: payload}], "rev.1", suggest=True)
                self.assertTrue(result["suggested"])
                self.assertEqual(result["removed_suggestion_ids"], ["s1"])

    def test_header_creation_and_content_edit_use_separate_revisions(self):
        self.reply({"replies": [{"createHeader": {"headerId": "h.new"}}],
                    "writeControl": {"requiredRevisionId": "rev.2"}})
        result = docs_edit.advanced_edit(DOC_ID, [{"createHeader": {
            "type": "DEFAULT", "sectionBreakLocation": {"index": 0, "tabId": "t.child"},
        }}], "rev.1")
        header_id = result["response"]["replies"][0]["createHeader"]["headerId"]
        self.serve(tabbed({"headers": {header_id: {"content": []}}}, "t.child", revisionId="rev.2"))
        snapshot = docs_edit.get_raw(DOC_ID)
        self.reply({"replies": [{}]})
        docs_edit.advanced_edit(DOC_ID, [{"insertText": {
            "endOfSegmentLocation": {"segmentId": header_id, "tabId": "t.child"}, "text": "Confidential",
        }}], snapshot["revisionId"])
        self.assertEqual(self.body()["writeControl"], {"requiredRevisionId": "rev.2"})
        self.assertEqual(self.requests()[0]["insertText"]["endOfSegmentLocation"],
                         {"segmentId": "h.new", "tabId": "t.child"})

    def test_office_file_errors_keep_the_existing_conversion_guidance(self):
        self.batch_update.return_value.execute.side_effect = http_error(400, "The document must not be an Office file")
        self.drive_file("Contract.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        with self.assertRaisesRegex(docs_edit.NotAGoogleDocError, "docs_convert_to_google_doc"):
            docs_edit.advanced_edit(DOC_ID, [{"deleteTableRow": {}}], "rev.1")


if __name__ == "__main__":
    unittest.main()
