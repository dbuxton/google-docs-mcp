"""Text inside tables must be readable, matchable and editable like any other text."""

import unittest

import docs_edit
from tests.helpers import DOC_ID, DocBuilder, DocsTestCase


def order_form() -> dict:
    # Indices: "Order Form" 1-11, table 12-62 ("Initial Term" 15-27, "12 months" 29-38,
    # "Customer" 41-49, "Oyster Ltd" 51-61), closing paragraph from 62.
    return (
        DocBuilder()
        .paragraph("Order Form", style="HEADING_1")
        .table([["Initial Term", "12 months"], ["Customer", "Oyster Ltd"]])
        .paragraph("Closing paragraph.")
        .build()
    )


class ReadingTablesTest(DocsTestCase):
    def test_get_lists_table_paragraphs_in_document_order(self):
        self.serve(order_form())

        result = docs_edit.get(DOC_ID)

        self.assertEqual(
            [p["text"] for p in result["paragraphs"]],
            ["Order Form", "Initial Term", "12 months", "Customer", "Oyster Ltd", "Closing paragraph."],
        )
        self.assertEqual(
            result["plain_text"],
            "Order Form\nInitial Term\n12 months\nCustomer\nOyster Ltd\nClosing paragraph.",
        )

    def test_get_says_which_table_cell_a_paragraph_is_in(self):
        self.serve(order_form())

        paragraphs = docs_edit.get(DOC_ID)["paragraphs"]

        self.assertNotIn("table_cell", paragraphs[0])
        self.assertEqual(paragraphs[1]["table_cell"], {"table": 0, "row": 0, "column": 0})
        self.assertEqual(paragraphs[2]["table_cell"], {"table": 0, "row": 0, "column": 1})
        self.assertEqual(paragraphs[4]["table_cell"], {"table": 0, "row": 1, "column": 1})
        self.assertNotIn("table_cell", paragraphs[5])
        self.assertEqual((paragraphs[1]["start"], paragraphs[1]["end"]), (15, 28))

    def test_a_cell_can_hold_several_paragraphs(self):
        self.serve(DocBuilder().table([[["Alpha", "Beta"]]]).build())

        paragraphs = docs_edit.get(DOC_ID)["paragraphs"]

        self.assertEqual([p["text"] for p in paragraphs], ["Alpha", "Beta"])
        self.assertEqual(paragraphs[0]["table_cell"], paragraphs[1]["table_cell"])

    def test_tables_inside_tables_are_numbered_in_document_order(self):
        def text_paragraph(start, text):
            return {
                "startIndex": start,
                "endIndex": start + len(text),
                "paragraph": {
                    "elements": [
                        {"startIndex": start, "endIndex": start + len(text), "textRun": {"content": text}}
                    ]
                },
            }

        inner = {"table": {"tableRows": [{"tableCells": [{"content": [text_paragraph(10, "Inner\n")]}]}]}}
        outer = {
            "table": {
                "tableRows": [{"tableCells": [{"content": [text_paragraph(3, "Outer\n"), inner]}]}]
            }
        }
        self.serve({"title": "Nested", "body": {"content": [outer]}})

        paragraphs = docs_edit.get(DOC_ID)["paragraphs"]

        self.assertEqual([p["text"] for p in paragraphs], ["Outer", "Inner"])
        self.assertEqual(paragraphs[0]["table_cell"], {"table": 0, "row": 0, "column": 0})
        self.assertEqual(paragraphs[1]["table_cell"], {"table": 1, "row": 0, "column": 0})


class EditingTablesTest(DocsTestCase):
    def setUp(self):
        super().setUp()
        self.serve(order_form())
        self.reply({"replies": [{"insertComment": {"commentThread": {"commentId": "c1"}}}]})

    def test_comment_anchors_to_text_inside_a_table(self):
        result = docs_edit.add_comment(DOC_ID, "Check the term", "Initial Term")

        self.assertEqual(
            self.requests(),
            [{"insertComment": {"range": {"startIndex": 15, "endIndex": 27}, "content": "Check the term"}}],
        )
        self.assertEqual(result["anchored_to"], "Initial Term")

    def test_comment_anchors_to_part_of_a_cell(self):
        docs_edit.add_comment(DOC_ID, "Who is this?", "Oyster")

        self.assertEqual(
            self.requests()[0]["insertComment"]["range"], {"startIndex": 51, "endIndex": 57}
        )

    def test_search_replace_edits_text_inside_a_cell(self):
        result = docs_edit.search_replace(DOC_ID, "12 months", "24 months")

        self.assertEqual(
            self.requests(),
            [
                {"insertText": {"location": {"index": 29}, "text": "24 months"}},
                {"deleteContentRange": {"range": {"startIndex": 38, "endIndex": 47}}},
            ],
        )
        self.assertEqual(result["replaced"], "12 months")

    def test_batch_replace_reaches_cells_and_body_together(self):
        docs_edit.batch_replace(
            DOC_ID,
            [
                {"find": "Oyster", "replace": "Acme"},
                {"find": "Closing", "replace": "Final"},
            ],
        )

        # End of document first, so the earlier indices stay valid.
        self.assertEqual(
            [r["insertText"]["location"]["index"] for r in self.requests() if "insertText" in r],
            [62, 51],
        )

    def test_insert_after_adds_a_paragraph_inside_the_cell(self):
        docs_edit.insert_after(DOC_ID, "Customer", "Preferred name", rich=False)

        self.assertEqual(
            self.requests(),
            [{"insertText": {"location": {"index": 49}, "text": "\nPreferred name"}}],
        )

    def test_insert_before_adds_a_paragraph_inside_the_cell(self):
        docs_edit.insert_before(DOC_ID, "Customer", "Preferred name", rich=False)

        self.assertEqual(
            self.requests(),
            [{"insertText": {"location": {"index": 41}, "text": "Preferred name\n"}}],
        )

    def test_deleting_a_cells_only_paragraph_keeps_the_cells_closing_newline(self):
        # Docs rejects a delete that includes the last newline of a cell.
        result = docs_edit.delete_paragraph(DOC_ID, "Oyster")

        self.assertEqual(
            self.requests(), [{"deleteContentRange": {"range": {"startIndex": 51, "endIndex": 61}}}]
        )
        self.assertEqual(result["deleted"], ["Oyster Ltd"])

    def test_deleting_one_of_several_paragraphs_in_a_cell(self):
        self.serve(DocBuilder().table([[["Alpha", "Beta"]]]).build())

        docs_edit.delete_paragraph(DOC_ID, "Alpha")
        self.assertEqual(
            self.requests(), [{"deleteContentRange": {"range": {"startIndex": 4, "endIndex": 10}}}]
        )

        docs_edit.delete_paragraph(DOC_ID, "Beta")
        self.assertEqual(
            self.requests(), [{"deleteContentRange": {"range": {"startIndex": 10, "endIndex": 14}}}]
        )

    def test_deleting_the_last_paragraph_of_the_body_keeps_its_newline(self):
        self.serve(DocBuilder().paragraph("First").paragraph("Last").build())

        docs_edit.delete_paragraph(DOC_ID, "Last")

        self.assertEqual(
            self.requests(), [{"deleteContentRange": {"range": {"startIndex": 7, "endIndex": 11}}}]
        )

    def test_a_paragraph_before_a_table_keeps_its_newline(self):
        # Docs rejects a delete that takes the newline just before a table.
        docs_edit.delete_paragraph(DOC_ID, "Order Form")

        self.assertEqual(
            self.requests(), [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 11}}}]
        )

    def test_a_paragraph_before_a_section_break_keeps_its_newline(self):
        builder = DocBuilder().paragraph("Chapter one")  # indices 1-13
        builder.content.append({"startIndex": 13, "endIndex": 14, "sectionBreak": {}})
        builder.index = 14
        self.serve(builder.paragraph("Chapter two").build())  # indices 14-26

        docs_edit.delete_paragraph(DOC_ID, "Chapter one")
        self.assertEqual(
            self.requests(), [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 12}}}]
        )

        # The last paragraph of the body keeps its newline for the usual reason.
        docs_edit.delete_paragraph(DOC_ID, "Chapter two")
        self.assertEqual(
            self.requests(), [{"deleteContentRange": {"range": {"startIndex": 14, "endIndex": 25}}}]
        )

    def test_an_empty_anchor_cannot_delete_everything(self):
        # It matches every paragraph, and the last paragraph is now deletable text-only,
        # so an empty anchor would otherwise empty the whole document.
        for anchor in ("", "   "):
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                docs_edit.delete_paragraph(DOC_ID, anchor)

        self.batch_update.assert_not_called()

    def test_deleting_an_ordinary_paragraph_removes_it_whole(self):
        self.serve(DocBuilder().paragraph("First").paragraph("Last").build())

        docs_edit.delete_paragraph(DOC_ID, "First")

        self.assertEqual(
            self.requests(), [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 7}}}]
        )


if __name__ == "__main__":
    unittest.main()
