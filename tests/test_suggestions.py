"""Suggest mode: writes become suggestions, reads understand them, and they can be reviewed."""

import unittest

from googleapiclient.errors import HttpError

import docs_edit
from tests.helpers import (
    DOC_ID,
    DocBuilder,
    DocsTestCase,
    deleted,
    http_error,
    inserted,
    restyled,
    saved_as_suggestion,
)

PLAIN = "Payment is due within 14 days of invoice."


def _own_style(suggestion_id: str) -> dict:
    """The style entry Google adds under a suggestion's own ID to the text it inserts or deletes."""
    return {
        suggestion_id: {
            "textStyle": {},
            "textStyleSuggestionState": {"boldSuggested": True, "italicSuggested": True, "linkSuggested": True},
        }
    }


def inserted_and_styled(text: str, suggestion_id: str) -> dict:
    """Inserted text as Google reports it: also marked with the suggestion's own style."""
    return {**inserted(text, suggestion_id), "suggestedTextStyleChanges": _own_style(suggestion_id)}


def deleted_and_styled(text: str, suggestion_id: str) -> dict:
    """Deleted text as Google reports it (a replace-all does this): also marked with the suggestion's own style."""
    return {**deleted(text, suggestion_id), "suggestedTextStyleChanges": _own_style(suggestion_id)}


def plain_doc() -> dict:
    # "14 days" is at 23-30.
    return DocBuilder().paragraph(PLAIN).paragraph("Governing law: England.").build()


def doc_with_pending_replacement() -> dict:
    # "Pay within " 1-12, inserted "30 days" 12-19, deleted "14 days" 19-26, " of invoice." 26-38.
    return (
        DocBuilder()
        .paragraph("Pay within ", inserted("30 days", "s1"), deleted("14 days", "s1"), " of invoice.")
        .paragraph("Governing law: England.")
        .build()
    )


class SuggestingWritesTest(DocsTestCase):
    def setUp(self):
        super().setUp()
        self.serve(plain_doc())

    def test_edits_are_direct_unless_asked(self):
        self.reply({"replies": [{}, {}]})

        result = docs_edit.search_replace(DOC_ID, "14 days", "30 days")

        self.assertNotIn("writeControl", self.body())
        self.assertNotIn("suggested", result)
        self.assertNotIn("suggestion_ids", result)

    def test_suggesting_a_replacement_sets_the_write_mode(self):
        self.reply(saved_as_suggestion(["suggest.a"], ["suggest.a"]))

        result = docs_edit.search_replace(DOC_ID, "14 days", "30 days", suggest=True)

        self.assertEqual(self.body()["writeControl"], {"writeMode": "SUGGEST"})
        self.assertEqual(
            self.requests(),
            [
                {"insertText": {"location": {"index": 23}, "text": "30 days"}},
                {"deleteContentRange": {"range": {"startIndex": 30, "endIndex": 37}}},
            ],
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["suggested"])
        self.assertEqual(result["suggestion_ids"], ["suggest.a"])
        self.assertEqual(result["replaced"], "14 days")

    def test_one_suggestion_named_twice_is_reported_once(self):
        self.reply(saved_as_suggestion(["suggest.a"], ["suggest.a", "suggest.b"]))

        result = docs_edit.search_replace(DOC_ID, "14 days", "30 days", suggest=True)

        self.assertEqual(result["suggestion_ids"], ["suggest.a", "suggest.b"])

    def test_a_change_folded_into_an_existing_suggestion_counts_but_is_not_called_new(self):
        # The existing suggestion may be somebody else's, so it is not reported as one the
        # edit created.
        self.reply({
            "replies": [{}],
            "commentUpdateState": "ALL_SAVED",
            "suggestionResponses": [{"updatedSummarySuggestionIds": ["suggest.a"]}],
        })

        result = docs_edit.search_replace(DOC_ID, "14 days", "30 days", suggest=True)

        self.assertTrue(result["suggested"])
        self.assertEqual(result["suggestion_ids"], [])
        self.assertEqual(result["updated_suggestion_ids"], ["suggest.a"])

    def test_created_and_updated_suggestions_are_reported_apart(self):
        self.reply({
            "replies": [{}, {}],
            "commentUpdateState": "ALL_SAVED",
            "suggestionResponses": [
                {"createdSuggestionIds": ["suggest.new"], "updatedSummarySuggestionIds": ["suggest.old"]},
                {"updatedSummarySuggestionIds": ["suggest.new"]},
            ],
        })

        result = docs_edit.search_replace(DOC_ID, "14 days", "30 days", suggest=True)

        self.assertEqual(result["suggestion_ids"], ["suggest.new"])
        self.assertEqual(result["updated_suggestion_ids"], ["suggest.old"])
        self.assertNotIn("removed_suggestion_ids", result)

    def test_deleting_text_a_suggestion_inserted_is_not_a_false_alarm(self):
        # Taking text out of a pending suggestion can leave no new suggestion behind.
        self.reply({
            "replies": [{}],
            "commentUpdateState": "ALL_SAVED",
            "suggestionResponses": [{"deletedSuggestionIds": ["suggest.a"]}],
        })

        result = docs_edit.search_replace(DOC_ID, " of invoice", "", suggest=True)

        self.assertEqual(len(self.requests()), 1)
        self.assertIn("deleteContentRange", self.requests()[0])
        self.assertTrue(result["suggested"])
        self.assertEqual(result["suggestion_ids"], [])
        self.assertEqual(result["removed_suggestion_ids"], ["suggest.a"])

    def test_a_removed_suggestion_does_not_excuse_an_edit_that_inserts_text(self):
        # An insertion always creates or updates a suggestion; if Google names none, the
        # text may have gone in directly.
        self.reply({
            "replies": [{}, {}],
            "commentUpdateState": "ALL_SAVED",
            "suggestionResponses": [{}, {"deletedSuggestionIds": ["suggest.a"]}],
        })

        with self.assertRaises(docs_edit.SuggestionNotSavedError):
            docs_edit.search_replace(DOC_ID, "14 days", "30 days", suggest=True)

    def test_a_delete_that_names_no_suggestion_at_all_is_still_an_error(self):
        self.reply({"replies": [{}], "commentUpdateState": "ALL_SAVED", "suggestionResponses": [{}]})

        with self.assertRaises(docs_edit.SuggestionNotSavedError):
            docs_edit.delete_paragraph(DOC_ID, "Governing", suggest=True)

    def test_every_edit_sends_the_same_requests_whether_suggesting_or_not(self):
        operations = {
            "search_replace": lambda suggest: docs_edit.search_replace(
                DOC_ID, "14 days", "30 days", suggest=suggest
            ),
            "search_replace_all": lambda suggest: docs_edit.search_replace(
                DOC_ID, "England", "Wales", occurrence=0, suggest=suggest
            ),
            "search_replace_regex": lambda suggest: docs_edit.search_replace(
                DOC_ID, r"1\d days", "30 days", regex=True, suggest=suggest
            ),
            "search_replace_delete": lambda suggest: docs_edit.search_replace(
                DOC_ID, " of invoice", "", suggest=suggest
            ),
            "insert_after": lambda suggest: docs_edit.insert_after(
                DOC_ID, "Governing", "# New heading\n- one\n- two", suggest=suggest
            ),
            "insert_before": lambda suggest: docs_edit.insert_before(
                DOC_ID, "Governing", "A **bold** claim", suggest=suggest
            ),
            "delete_paragraph": lambda suggest: docs_edit.delete_paragraph(
                DOC_ID, "Governing", suggest=suggest
            ),
            "append": lambda suggest: docs_edit.append(DOC_ID, "The end", suggest=suggest),
            "batch_replace": lambda suggest: docs_edit.batch_replace(
                DOC_ID,
                [{"find": "14 days", "replace": "30 days"}, {"find": "England", "replace": "Wales"}],
                suggest=suggest,
            ),
            "batch_replace_all_and_regex": lambda suggest: docs_edit.batch_replace(
                DOC_ID,
                [
                    {"find": r"\d+ days", "replace": "30 days", "regex": True},
                    {"find": "a", "replace": "A", "occurrence": 0},
                ],
                suggest=suggest,
            ),
        }
        for name, operation in operations.items():
            with self.subTest(name):
                self.reply({
                    "replies": [{"replaceAllText": {"occurrencesChanged": 1}}] * 12,
                    "commentUpdateState": "ALL_SAVED",
                    "suggestionResponses": [{"createdSuggestionIds": ["suggest.a"]}] * 12,
                })
                operation(False)
                direct = self.body()
                operation(True)
                suggested = self.body()

                self.assertNotIn("writeControl", direct)
                self.assertEqual(suggested.pop("writeControl"), {"writeMode": "SUGGEST"})
                self.assertEqual(suggested, direct)

    def test_every_edit_reports_the_suggestions_it_made(self):
        operations = [
            lambda: docs_edit.insert_after(DOC_ID, "Governing", "Text", suggest=True),
            lambda: docs_edit.insert_before(DOC_ID, "Governing", "Text", suggest=True),
            lambda: docs_edit.delete_paragraph(DOC_ID, "Governing", suggest=True),
            lambda: docs_edit.append(DOC_ID, "Text", suggest=True),
            lambda: docs_edit.batch_replace(
                DOC_ID, [{"find": "England", "replace": "Wales"}], suggest=True
            ),
        ]
        for operation in operations:
            self.reply(saved_as_suggestion(*[["suggest.a"]] * 10))
            result = operation()
            self.assertEqual(result["suggestion_ids"], ["suggest.a"])
            self.assertTrue(result["suggested"])

    def test_replace_all_can_be_suggested(self):
        self.reply({
            "replies": [{"replaceAllText": {"occurrencesChanged": 2}}],
            "commentUpdateState": "ALL_SAVED",
            "suggestionResponses": [{"createdSuggestionIds": ["suggest.a", "suggest.b"]}],
        })

        result = docs_edit.search_replace(DOC_ID, "England", "Wales", occurrence=0, suggest=True)

        self.assertEqual(self.body()["writeControl"], {"writeMode": "SUGGEST"})
        self.assertEqual(result["occurrences_changed"], 2)
        self.assertEqual(result["suggestion_ids"], ["suggest.a", "suggest.b"])

    def test_replace_all_that_matches_nothing_is_not_an_error(self):
        self.reply({
            "replies": [{"replaceAllText": {}}],
            "commentUpdateState": "NO_UPDATES_REQUESTED",
            "suggestionResponses": [{}],
        })

        result = docs_edit.search_replace(DOC_ID, "Narnia", "Wales", occurrence=0, suggest=True)

        self.assertEqual(result["occurrences_changed"], 0)
        self.assertEqual(result["suggestion_ids"], [])

    def test_replace_all_that_matched_but_made_no_suggestion_is_an_error(self):
        self.reply({"replies": [{"replaceAllText": {"occurrencesChanged": 2}}]})

        with self.assertRaises(docs_edit.SuggestionNotSavedError):
            docs_edit.search_replace(DOC_ID, "England", "Wales", occurrence=0, suggest=True)


class UnconfirmedSuggestionsTest(DocsTestCase):
    """A suggest-mode batch can commit its text changes yet fail to save the suggestion."""

    def setUp(self):
        super().setUp()
        self.serve(plain_doc())

    def test_failed_suggestion_save_is_reported_as_a_possible_live_edit(self):
        self.reply({
            "replies": [{}, {}],
            "commentUpdateState": "ALL_FAILED_UNKNOWN_REASON",
            "suggestionResponses": [{}, {}],
        })

        with self.assertRaisesRegex(
            docs_edit.SuggestionNotSavedError, "ALL_FAILED_UNKNOWN_REASON.*applied directly"
        ):
            docs_edit.search_replace(DOC_ID, "14 days", "30 days", suggest=True)

    def test_no_suggestion_ids_means_the_edit_cannot_be_called_a_suggestion(self):
        # What an API that ignored the write mode would send back.
        self.reply({"replies": [{}, {}]})

        with self.assertRaisesRegex(docs_edit.SuggestionNotSavedError, "commentUpdateState=missing"):
            docs_edit.search_replace(DOC_ID, "14 days", "30 days", suggest=True)

    def test_ids_alongside_a_failed_state_are_still_an_error(self):
        self.reply({
            "commentUpdateState": "ALL_FAILED_UNKNOWN_REASON",
            "suggestionResponses": [{"createdSuggestionIds": ["suggest.a"]}],
        })

        with self.assertRaises(docs_edit.SuggestionNotSavedError):
            docs_edit.append(DOC_ID, "Text", suggest=True)

    def test_a_direct_edit_needs_no_confirmation(self):
        self.reply({"replies": [{}, {}]})

        self.assertTrue(docs_edit.search_replace(DOC_ID, "14 days", "30 days")["ok"])


class ReadingSuggestionsTest(DocsTestCase):
    def test_get_shows_the_document_as_it_will_read_once_accepted(self):
        self.serve(doc_with_pending_replacement())

        result = docs_edit.get(DOC_ID)

        self.assertEqual(result["paragraphs"][0]["text"], "Pay within 30 days of invoice.")
        self.assertEqual(result["plain_text"], "Pay within 30 days of invoice.\nGoverning law: England.")
        self.assertEqual(result["paragraphs"][0]["suggestions"], ["s1"])
        self.assertNotIn("suggestions", result["paragraphs"][1])
        self.assertEqual(result["suggestion_ids"], ["s1"])
        self.assertIn("accepted", result["note"])

    def test_indices_are_still_those_of_the_inline_view(self):
        self.serve(doc_with_pending_replacement())

        paragraph = docs_edit.get(DOC_ID)["paragraphs"][0]

        self.assertEqual((paragraph["start"], paragraph["end"]), (1, 39))

    def test_a_document_without_suggestions_reads_exactly_as_before(self):
        self.serve(plain_doc())

        result = docs_edit.get(DOC_ID)

        self.assertNotIn("suggestion_ids", result)
        self.assertNotIn("note", result)
        self.assertEqual(
            result["paragraphs"][0], {"text": PLAIN, "style": "NORMAL_TEXT", "start": 1, "end": 43}
        )

    def test_a_paragraph_a_suggestion_deletes_reads_as_empty_but_is_flagged(self):
        self.serve(
            DocBuilder()
            .paragraph("Keep me")
            .paragraph(deleted("Drop me\n", "s2"), newline=False)
            .build()
        )

        paragraphs = docs_edit.get(DOC_ID)["paragraphs"]

        self.assertEqual(paragraphs[1]["text"], "")
        self.assertEqual(paragraphs[1]["suggestions"], ["s2"])

    def test_formatting_suggestions_flag_their_paragraphs(self):
        self.serve(DocBuilder().paragraph("Heading", restyled("Term", "s3", bold=True)).build())

        paragraph = docs_edit.get(DOC_ID)["paragraphs"][0]

        self.assertEqual(paragraph["text"], "HeadingTerm")
        self.assertEqual(paragraph["suggestions"], ["s3"])

    def test_paragraph_style_and_bullet_suggestions_flag_their_paragraphs(self):
        self.serve(
            DocBuilder()
            .paragraph(
                "Item",
                suggestedParagraphStyleChanges={"s4": {}},
                suggestedBulletChanges={"s5": {}},
            )
            .build()
        )

        self.assertEqual(docs_edit.get(DOC_ID)["paragraphs"][0]["suggestions"], ["s4", "s5"])

    def test_a_suggested_page_break_or_image_flags_its_paragraph(self):
        builder = DocBuilder().paragraph("Before")  # indices 1-8
        builder.content.append({
            "startIndex": 8,
            "endIndex": 10,
            "paragraph": {
                "elements": [
                    {"startIndex": 8, "endIndex": 9, "pageBreak": {"suggestedInsertionIds": ["s8"]}},
                    {"startIndex": 9, "endIndex": 10, "textRun": {"content": "\n"}},
                ]
            },
        })
        self.serve(builder.build())

        result = docs_edit.get(DOC_ID)

        self.assertNotIn("suggestions", result["paragraphs"][0])
        self.assertEqual(result["paragraphs"][1]["suggestions"], ["s8"])
        self.assertEqual(result["suggestion_ids"], ["s8"])


class AnchoringOnProposedTextTest(DocsTestCase):
    """Anchors quote what the document will read, so a second edit can follow the first."""

    def setUp(self):
        super().setUp()
        self.serve(doc_with_pending_replacement())

    def test_comment_anchor_can_span_a_pending_insertion_and_deletion(self):
        self.reply({"replies": [{"insertComment": {"commentThread": {"commentId": "c1"}}}]})

        result = docs_edit.add_comment(DOC_ID, "Is 30 right?", "30 days of invoice")

        # From the inserted "30 days" (12) to the end of " of invoice" (37), across the
        # deleted "14 days" that is still in the inline index space.
        self.assertEqual(
            self.requests()[0]["insertComment"]["range"], {"startIndex": 12, "endIndex": 37}
        )
        self.assertEqual(result["anchored_to"], "30 days of invoice")

    def test_text_a_suggestion_deletes_cannot_be_matched(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            docs_edit.search_replace(DOC_ID, "14 days", "45 days", suggest=True)

    def test_a_further_edit_can_target_text_a_suggestion_inserted(self):
        self.reply(saved_as_suggestion(["s9"], ["s9"]))

        docs_edit.search_replace(DOC_ID, "30 days", "45 days", suggest=True)

        self.assertEqual(
            self.requests(),
            [
                {"insertText": {"location": {"index": 12}, "text": "45 days"}},
                {"deleteContentRange": {"range": {"startIndex": 19, "endIndex": 26}}},
            ],
        )

    def test_an_already_deleted_paragraph_is_not_matched_again(self):
        self.serve(
            DocBuilder()
            .paragraph(deleted("Governing law: England.\n", "s2"), newline=False)
            .build()
        )

        with self.assertRaisesRegex(ValueError, "No paragraph containing anchor"):
            docs_edit.delete_paragraph(DOC_ID, "Governing", suggest=True)


class ListingSuggestionsTest(DocsTestCase):
    def thread(self, suggestion_id, status="OPEN", summary="", author="Dana", replies=None):
        return {
            "suggestionId": suggestion_id,
            "status": status,
            "summaryText": summary,
            "headPost": {
                "postId": f"{suggestion_id}-head",
                "author": {"displayName": author},
                "createTime": "2026-10-14T09:00:00Z",
            },
            "replies": replies or [],
        }

    def serve_in_tabs(self, doc: dict, **extra) -> None:
        self.serve({
            "title": doc["title"],
            "tabs": [{"tabProperties": {"tabId": "t.0"}, "documentTab": {"body": doc["body"]}}],
            **extra,
        })

    def test_lists_open_suggestions_with_what_they_change(self):
        self.serve_in_tabs(
            doc_with_pending_replacement(),
            suggestions=[
                self.thread("s1", summary="Replace 14 days with 30 days"),
                self.thread("s0", status="ACCEPTED"),
            ],
        )

        result = docs_edit.list_suggestions(DOC_ID)

        self.docs.documents().get.assert_called_with(
            documentId=DOC_ID,
            suggestionsViewMode="SUGGESTIONS_INLINE",
            commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED",
            includeTabsContent=True,
        )
        self.assertEqual(result["count"], 1)
        suggestion = result["suggestions"][0]
        self.assertEqual(suggestion["id"], "s1")
        self.assertEqual(suggestion["status"], "OPEN")
        self.assertEqual(suggestion["author"], "Dana")
        self.assertEqual(suggestion["created"], "2026-10-14T09:00:00Z")
        self.assertEqual(suggestion["summary"], "Replace 14 days with 30 days")
        self.assertEqual(
            suggestion["changes"],
            [
                {"type": "insertion", "text": "30 days", "context": "Pay within 30 days of invoice."},
                {"type": "deletion", "text": "14 days", "context": "Pay within 30 days of invoice."},
            ],
        )

    def test_closed_suggestions_are_listed_on_request(self):
        self.serve_in_tabs(
            doc_with_pending_replacement(),
            suggestions=[self.thread("s1"), self.thread("s0", status="REJECTED")],
        )

        result = docs_edit.list_suggestions(DOC_ID, include_closed=True)

        self.assertEqual([s["id"] for s in result["suggestions"]], ["s1", "s0"])
        self.assertEqual(result["suggestions"][1]["status"], "REJECTED")
        self.assertEqual(result["suggestions"][1]["changes"], [])

    def test_a_suggestion_marked_in_the_text_but_missing_from_the_threads_is_listed(self):
        self.serve_in_tabs(doc_with_pending_replacement(), suggestions=[])

        result = docs_edit.list_suggestions(DOC_ID)

        self.assertEqual([s["id"] for s in result["suggestions"]], ["s1"])
        self.assertEqual(result["suggestions"][0]["status"], "OPEN")
        self.assertIsNone(result["suggestions"][0]["author"])
        self.assertEqual(len(result["suggestions"][0]["changes"]), 2)

    def test_a_thread_with_no_markers_is_still_listed(self):
        # e.g. a header or document-style suggestion
        self.serve_in_tabs(plain_doc(), suggestions=[self.thread("s7", summary="Change margins")])

        suggestion = docs_edit.list_suggestions(DOC_ID)["suggestions"][0]

        self.assertEqual(suggestion["id"], "s7")
        self.assertEqual(suggestion["summary"], "Change margins")
        self.assertEqual(suggestion["changes"], [])

    def test_an_inserted_paragraph_is_one_change_without_its_line_breaks(self):
        # insert_after puts "\nNew clause" before the paragraph's own newline, so the
        # suggestion's text spans the end of one paragraph and the start of the next.
        doc = (
            DocBuilder()
            .paragraph("Intro", inserted("\n", "s1"), newline=False)
            .paragraph(inserted("New clause", "s1"))
            .build()
        )
        self.serve_in_tabs(doc, suggestions=[self.thread("s1")])

        changes = docs_edit.list_suggestions(DOC_ID)["suggestions"][0]["changes"]

        self.assertEqual(changes, [{"type": "insertion", "text": "New clause", "context": "Intro"}])

    def test_separate_places_stay_separate_changes_of_one_suggestion(self):
        doc = (
            DocBuilder()
            .paragraph("The ", inserted("Supplier", "s1"), " and the ", inserted("Customer", "s1"))
            .build()
        )
        self.serve_in_tabs(doc, suggestions=[self.thread("s1")])

        changes = docs_edit.list_suggestions(DOC_ID)["suggestions"][0]["changes"]

        self.assertEqual([c["text"] for c in changes], ["Supplier", "Customer"])

    def test_formatting_suggestions_say_what_changed(self):
        doc = (
            DocBuilder()
            .paragraph("See ", restyled("Schedule 1", "s3", bold=True), " now")
            .paragraph(
                "Heading",
                suggestedParagraphStyleChanges={
                    "s4": {"paragraphStyleSuggestionState": {"namedStyleTypeSuggested": True}}
                },
                suggestedBulletChanges={"s5": {}},
            )
            .build()
        )
        self.serve_in_tabs(
            doc, suggestions=[self.thread("s3"), self.thread("s4"), self.thread("s5")]
        )

        by_id = {s["id"]: s["changes"] for s in docs_edit.list_suggestions(DOC_ID)["suggestions"]}

        self.assertEqual(
            by_id["s3"],
            [{"type": "text_style", "text": "Schedule 1", "fields": ["bold"], "context": "See Schedule 1 now"}],
        )
        self.assertEqual(
            by_id["s4"],
            [{"type": "paragraph_style", "fields": ["namedStyleType"], "context": "Heading"}],
        )
        self.assertEqual(by_id["s5"], [{"type": "bullet", "context": "Heading"}])

    def test_style_mirrored_onto_inserted_text_is_not_a_second_change(self):
        doc = (
            DocBuilder()
            .paragraph("Pay within ", inserted_and_styled("30 days", "s1"), deleted("14 days", "s1"), " of invoice.")
            .build()
        )
        self.serve_in_tabs(doc, suggestions=[self.thread("s1")])

        changes = docs_edit.list_suggestions(DOC_ID)["suggestions"][0]["changes"]

        self.assertEqual([(c["type"], c["text"]) for c in changes], [("insertion", "30 days"), ("deletion", "14 days")])

    def test_style_flags_google_puts_on_text_a_suggestion_deletes_are_not_a_change(self):
        # Seen live after a replace-all: both the new text and the replaced text carry them.
        doc = DocBuilder().paragraph(inserted_and_styled("Acme", "s1"), deleted_and_styled("Oyster", "s1"), " Ltd").build()
        self.serve_in_tabs(doc, suggestions=[self.thread("s1")])

        changes = docs_edit.list_suggestions(DOC_ID)["suggestions"][0]["changes"]

        self.assertEqual([(c["type"], c["text"]) for c in changes], [("insertion", "Acme"), ("deletion", "Oyster")])

    def test_suggestions_are_listed_in_document_order_whatever_order_google_sends(self):
        doc = (
            DocBuilder()
            .paragraph("First ", inserted("one", "s1"))
            .paragraph("Second ", inserted("two", "s2"))
            .build()
        )
        # Threads arrive in an arbitrary order; s9 touches nothing in the body.
        self.serve_in_tabs(doc, suggestions=[self.thread("s9"), self.thread("s2"), self.thread("s1")])

        result = docs_edit.list_suggestions(DOC_ID)

        self.assertEqual([s["id"] for s in result["suggestions"]], ["s1", "s2", "s9"])

    def test_paragraph_style_and_bullet_mirrored_onto_an_inserted_paragraph_are_not_changes(self):
        mirrored = {
            "suggestedParagraphStyleChanges": {
                "s1": {"paragraphStyleSuggestionState": {"namedStyleTypeSuggested": True}}
            },
            "suggestedBulletChanges": {"s1": {}},
        }
        doc = (
            DocBuilder()
            .paragraph("Intro", inserted("\n", "s1"), newline=False, **mirrored)
            .paragraph(inserted("New item", "s1"), **mirrored)
            .build()
        )
        self.serve_in_tabs(doc, suggestions=[self.thread("s1")])

        changes = docs_edit.list_suggestions(DOC_ID)["suggestions"][0]["changes"]

        self.assertEqual(changes, [{"type": "insertion", "text": "New item", "context": "Intro"}])

    def test_a_real_restyle_by_a_suggestion_that_also_inserts_is_still_listed(self):
        doc = DocBuilder().paragraph(inserted("New ", "s1"), restyled("Old", "s1", bold=True)).build()
        self.serve_in_tabs(doc, suggestions=[self.thread("s1")])

        changes = docs_edit.list_suggestions(DOC_ID)["suggestions"][0]["changes"]

        self.assertEqual(
            changes,
            [
                {"type": "insertion", "text": "New ", "context": "New Old"},
                {"type": "text_style", "text": "Old", "fields": ["bold"], "context": "New Old"},
            ],
        )

    def test_a_style_entry_that_changes_no_field_is_dropped(self):
        empty = {
            "content": "Term",
            "suggestedTextStyleChanges": {
                "s9": {"textStyle": {}, "textStyleSuggestionState": {"boldSuggested": False}}
            },
        }
        self.serve_in_tabs(DocBuilder().paragraph("See ", empty).build(), suggestions=[self.thread("s9")])

        self.assertEqual(docs_edit.list_suggestions(DOC_ID)["suggestions"][0]["changes"], [])

    def test_context_is_cut_to_120_characters(self):
        doc = DocBuilder().paragraph("word " * 60, inserted("NEW", "s1")).build()
        self.serve_in_tabs(doc, suggestions=[self.thread("s1")])

        change = docs_edit.list_suggestions(DOC_ID)["suggestions"][0]["changes"][0]

        self.assertEqual(change["text"], "NEW")
        self.assertEqual(len(change["context"]), 120)
        self.assertTrue(change["context"].startswith("word word"))

    def test_footers_and_footnotes_are_searched_too(self):
        body = DocBuilder().paragraph("Body").build()["body"]
        footer = DocBuilder().paragraph("Page ", inserted("DRAFT", "s2")).build()["body"]
        footnote = DocBuilder().paragraph(deleted("See annex", "s3")).build()["body"]
        self.serve({
            "title": "Parts",
            "tabs": [{
                "documentTab": {
                    "body": body,
                    "footers": {"kix.f1": {"content": footer["content"]}},
                    "footnotes": {"kix.fn1": {"content": footnote["content"]}},
                }
            }],
            "suggestions": [self.thread("s2"), self.thread("s3")],
        })

        by_id = {s["id"]: s["changes"] for s in docs_edit.list_suggestions(DOC_ID)["suggestions"]}

        self.assertEqual(by_id["s2"][0]["text"], "DRAFT")
        self.assertEqual(by_id["s3"][0]["text"], "See annex")

    def test_a_document_without_tabs_has_headers_footers_and_footnotes_too(self):
        parts = {
            name: {"kix.x": {"content": DocBuilder().paragraph(inserted(text, sid)).build()["body"]["content"]}}
            for name, text, sid in (("headers", "HEAD", "s1"), ("footers", "FOOT", "s2"), ("footnotes", "NOTE", "s3"))
        }
        self.serve({
            "title": "Legacy",
            "body": DocBuilder().paragraph("Body").build()["body"],
            **parts,
            "suggestions": [self.thread("s1"), self.thread("s2"), self.thread("s3")],
        })

        by_id = {s["id"]: s["changes"][0]["text"] for s in docs_edit.list_suggestions(DOC_ID)["suggestions"]}

        self.assertEqual(by_id, {"s1": "HEAD", "s2": "FOOT", "s3": "NOTE"})

    def test_suggestions_in_tables_headers_and_child_tabs_are_found(self):
        table_doc = DocBuilder().table([["Fee ", inserted("£500", "s1")]]).build()
        header = DocBuilder().paragraph(inserted("DRAFT", "s2")).build()
        child = DocBuilder().paragraph(deleted("Old", "s3"), "New").build()
        self.serve({
            "title": "Tabs",
            "tabs": [{
                "documentTab": {
                    "body": table_doc["body"],
                    "headers": {"kix.h1": {"content": header["body"]["content"]}},
                },
                "childTabs": [{"documentTab": {"body": child["body"]}}],
            }],
            "suggestions": [self.thread("s1"), self.thread("s2"), self.thread("s3")],
        })

        by_id = {s["id"]: s["changes"] for s in docs_edit.list_suggestions(DOC_ID)["suggestions"]}

        self.assertEqual(by_id["s1"][0]["text"], "£500")
        self.assertEqual(by_id["s2"][0]["text"], "DRAFT")
        self.assertEqual(by_id["s3"][0], {"type": "deletion", "text": "Old", "context": "New"})

    def test_a_document_without_tabs_is_read_from_its_body(self):
        doc = doc_with_pending_replacement()
        doc["suggestions"] = [self.thread("s1")]
        self.serve(doc)

        self.assertEqual(len(docs_edit.list_suggestions(DOC_ID)["suggestions"][0]["changes"]), 2)

    def test_replies_to_a_suggestion_are_included_without_empty_ones(self):
        replies = [
            {"postId": "r1", "content": "Why 30?", "author": {"displayName": "Sam"}, "createTime": "t"},
            {"postId": "r2", "content": "", "author": {"displayName": "Sam"}, "suggestionAction": "ACCEPT"},
            {"postId": "r3", "deleted": True},
        ]
        self.serve_in_tabs(doc_with_pending_replacement(), suggestions=[self.thread("s1", replies=replies)])

        suggestion = docs_edit.list_suggestions(DOC_ID)["suggestions"][0]

        self.assertEqual(
            suggestion["replies"],
            [{"id": "r1", "author": "Sam", "content": "Why 30?", "created": "t"}],
        )

    def test_a_pdf_is_explained(self):
        self.docs.documents().get().execute.side_effect = http_error(
            400, "Request contains an invalid argument."
        )
        self.drive_file("Terms.pdf", "application/pdf")

        with self.assertRaisesRegex(docs_edit.NotAGoogleDocError, "is a PDF, not a Google Doc"):
            docs_edit.list_suggestions(DOC_ID)


class ReviewingSuggestionsTest(DocsTestCase):
    def acknowledged(self, key: str, *ids: str) -> dict:
        return {
            "replies": [{} for _ in ids],
            "suggestionResponses": [{key: [suggestion_id]} for suggestion_id in ids],
        }

    def test_accepting_sends_one_request_per_suggestion_in_one_batch(self):
        self.reply(self.acknowledged("acceptedSuggestionIds", "s1", "s2"))

        result = docs_edit.review_suggestions(DOC_ID, "accept", ["s1", "s2"])

        self.assertEqual(
            self.requests(),
            [
                {"acceptSuggestion": {"suggestionId": "s1"}},
                {"acceptSuggestion": {"suggestionId": "s2"}},
            ],
        )
        self.assertNotIn("writeControl", self.body())
        self.assertEqual(result, {"ok": True, "action": "accept", "applied": ["s1", "s2"]})
        self.assertEqual(self.batch_update.call_count, 1)

    def test_rejecting(self):
        self.reply(self.acknowledged("rejectedSuggestionIds", "s1"))

        result = docs_edit.review_suggestions(DOC_ID, "reject", ["s1"])

        self.assertEqual(self.requests(), [{"rejectSuggestion": {"suggestionId": "s1"}}])
        self.assertEqual(result["applied"], ["s1"])

    def test_deleting(self):
        self.reply(self.acknowledged("deletedSuggestionIds", "s1"))

        result = docs_edit.review_suggestions(DOC_ID, "delete", ["s1"])

        self.assertEqual(self.requests(), [{"deleteSuggestion": {"suggestionId": "s1"}}])
        self.assertEqual(result["applied"], ["s1"])

    def test_repeated_ids_are_sent_once(self):
        self.reply(self.acknowledged("acceptedSuggestionIds", "s1"))

        docs_edit.review_suggestions(DOC_ID, "accept", ["s1", "s1"])

        self.assertEqual(len(self.requests()), 1)

    def test_ids_google_did_not_list_are_flagged_not_assumed_done(self):
        self.reply(self.acknowledged("acceptedSuggestionIds", "s1"))

        result = docs_edit.review_suggestions(DOC_ID, "accept", ["s1", "s2"])

        self.assertEqual(result["applied"], ["s1"])
        self.assertEqual(result["unconfirmed"], ["s2"])
        self.assertIn("docs_list_suggestions", result["note"])

    def test_an_unknown_action_is_refused_before_anything_is_sent(self):
        with self.assertRaisesRegex(ValueError, "accept, reject, delete"):
            docs_edit.review_suggestions(DOC_ID, "approve", ["s1"])

        self.batch_update.assert_not_called()

    def test_no_ids_is_refused_before_anything_is_sent(self):
        with self.assertRaisesRegex(ValueError, "docs_list_suggestions"):
            docs_edit.review_suggestions(DOC_ID, "accept", [])

        self.batch_update.assert_not_called()

    def test_permission_errors_reach_the_caller(self):
        # e.g. deleting a suggestion somebody else made
        self.batch_update.return_value.execute.side_effect = http_error(403, "Forbidden")

        with self.assertRaises(HttpError) as caught:
            docs_edit.review_suggestions(DOC_ID, "delete", ["s1"])

        self.assertEqual(caught.exception.resp.status, 403)
        self.drive.files.assert_not_called()

    def test_a_400_on_a_real_google_doc_keeps_the_original_error(self):
        self.batch_update.return_value.execute.side_effect = http_error(400, "Invalid suggestion")
        self.drive_file("Contract", docs_edit.GOOGLE_DOC_MIME_TYPE)

        with self.assertRaises(HttpError):
            docs_edit.review_suggestions(DOC_ID, "accept", ["nope"])

    def test_a_pdf_is_explained(self):
        self.batch_update.return_value.execute.side_effect = http_error(
            400, "Request contains an invalid argument."
        )
        self.drive_file("Terms.pdf", "application/pdf")

        with self.assertRaisesRegex(docs_edit.NotAGoogleDocError, "is a PDF, not a Google Doc"):
            docs_edit.review_suggestions(DOC_ID, "accept", ["s1"])


if __name__ == "__main__":
    unittest.main()
