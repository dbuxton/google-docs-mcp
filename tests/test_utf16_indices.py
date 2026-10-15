"""Docs counts indices in UTF-16 code units, so an emoji takes two; the tools must count the same way."""

import unittest

import docs_edit
from tests.helpers import DOC_ID, DocBuilder, DocsTestCase

GRIN = "\U0001F600"  # one character, two UTF-16 code units


class IndicesAfterAnEmojiTest(DocsTestCase):
    def setUp(self):
        super().setUp()
        # Indices: emoji 1-3, " " 3-4, "Pay" 4-7, " " 7-8, "14 days" 8-15, " later." 15-22, newline 22-23.
        self.serve(DocBuilder().paragraph(f"{GRIN} Pay 14 days later.").build())
        self.reply({"replies": [{}, {}, {}, {}]})

    def test_search_replace_lands_on_the_matched_text(self):
        docs_edit.search_replace(DOC_ID, "14 days", "30 days")

        self.assertEqual(
            self.requests(),
            [
                {"insertText": {"location": {"index": 8}, "text": "30 days"}},
                {"deleteContentRange": {"range": {"startIndex": 15, "endIndex": 22}}},
            ],
        )

    def test_a_replacement_with_an_emoji_shifts_the_delete_by_its_utf16_width(self):
        docs_edit.search_replace(DOC_ID, "14 days", f"{GRIN} 30 days")  # 2 + 8 = 10 units, 9 characters

        self.assertEqual(
            self.requests()[1], {"deleteContentRange": {"range": {"startIndex": 18, "endIndex": 25}}}
        )

    def test_a_match_that_ends_on_an_emoji_includes_both_of_its_units(self):
        self.serve(DocBuilder().paragraph(f"Smile {GRIN}").build())  # "Smile " 1-7, emoji 7-9

        docs_edit.search_replace(DOC_ID, f"Smile {GRIN}", "Frown")

        self.assertEqual(
            self.requests(),
            [
                {"insertText": {"location": {"index": 1}, "text": "Frown"}},
                {"deleteContentRange": {"range": {"startIndex": 6, "endIndex": 14}}},
            ],
        )

    def test_batch_replace_handles_emoji_before_and_in_the_replacement(self):
        docs_edit.batch_replace(
            DOC_ID,
            [{"find": "Pay", "replace": f"{GRIN}Pay"}, {"find": "later", "replace": "sooner"}],
        )

        # End of the paragraph first; each delete starts after the text just inserted.
        self.assertEqual(
            self.requests(),
            [
                {"insertText": {"location": {"index": 16}, "text": "sooner"}},
                {"deleteContentRange": {"range": {"startIndex": 22, "endIndex": 27}}},
                {"insertText": {"location": {"index": 4}, "text": f"{GRIN}Pay"}},
                {"deleteContentRange": {"range": {"startIndex": 9, "endIndex": 12}}},
            ],
        )

    def test_comments_anchor_after_and_around_an_emoji(self):
        self.reply({"replies": [{"insertComment": {"commentThread": {"commentId": "c1"}}}]})

        docs_edit.add_comment(DOC_ID, "Which days?", "14 days")
        self.assertEqual(self.requests()[0]["insertComment"]["range"], {"startIndex": 8, "endIndex": 15})

        docs_edit.add_comment(DOC_ID, "Nice", f"{GRIN} Pay")
        self.assertEqual(self.requests()[0]["insertComment"]["range"], {"startIndex": 1, "endIndex": 7})

    def test_rich_text_styles_land_on_the_right_characters(self):
        # Inserted after the paragraph's text: "\n" (1 unit), then "<emoji> bold text" where
        # "bold" starts 3 units in (the emoji is 2, the space 1).
        docs_edit.insert_after(DOC_ID, "Pay", f"{GRIN} **bold** text")

        insert_at = 22  # just before the paragraph's closing newline
        bold = next(r for r in self.requests() if "updateTextStyle" in r)["updateTextStyle"]["range"]
        self.assertEqual(self.requests()[0]["insertText"]["location"]["index"], insert_at)
        self.assertEqual(bold, {"startIndex": insert_at + 1 + 3, "endIndex": insert_at + 1 + 3 + 4})


if __name__ == "__main__":
    unittest.main()
