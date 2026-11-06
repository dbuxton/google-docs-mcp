import unittest
from unittest import mock

import docs_edit

DOC_ID = "doc123"

BULLET_LIST = "kix.bullets"
NUMBER_LIST = "kix.numbers"
LISTS = {
    BULLET_LIST: {"listProperties": {"nestingLevels": [{"glyphSymbol": "●"}, {"glyphSymbol": "○"}]}},
    NUMBER_LIST: {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}, {"glyphType": "ALPHA"}]}},
}


def _para(text, *, style="NORMAL_TEXT", list_id=None, level=0, bold=False, link=None,
          alignment=None, indent_start=None):
    return {
        "text": text,
        "style": style,
        "list_id": list_id,
        "level": level,
        "bold": bold,
        "link": link,
        "alignment": alignment,
        "indent_start": indent_start,
    }


def _doc(paragraphs):
    """Build a Docs API document body; the first paragraph starts at index 1."""
    content = [{"startIndex": 0, "endIndex": 1, "sectionBreak": {}}]
    index = 1
    for p in paragraphs:
        text = p["text"] + "\n"
        text_style = {}
        if p["bold"]:
            text_style["bold"] = True
        if p["link"]:
            text_style["link"] = {"url": p["link"]}
        paragraph_style = {"namedStyleType": p["style"]}
        if p["alignment"]:
            paragraph_style["alignment"] = p["alignment"]
        if p["indent_start"]:
            paragraph_style["indentStart"] = {"magnitude": p["indent_start"], "unit": "PT"}
        paragraph = {
            "elements": [{
                "startIndex": index,
                "endIndex": index + len(text),
                "textRun": {"content": text, "textStyle": text_style},
            }],
            "paragraphStyle": paragraph_style,
        }
        if p["list_id"]:
            paragraph["bullet"] = {"listId": p["list_id"], "nestingLevel": p["level"]}
        content.append({
            "startIndex": index,
            "endIndex": index + len(text),
            "paragraph": paragraph,
        })
        index += len(text)
    return {"title": "Doc", "body": {"content": content}, "lists": LISTS}


class FormattingTestCase(unittest.TestCase):
    def use_doc(self, paragraphs):
        self.service = mock.MagicMock()
        self.service.documents().get().execute.return_value = _doc(paragraphs)
        patcher = mock.patch.object(docs_edit, "_get_service", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    def requests(self):
        """The requests sent in the single batchUpdate call."""
        calls = self.service.documents().batchUpdate.call_args_list
        self.assertEqual(len(calls), 1, "expected exactly one batchUpdate")
        return calls[0].kwargs["body"]["requests"]

    def assert_no_write(self):
        self.service.documents().batchUpdate.assert_not_called()


class GetListInfoTest(FormattingTestCase):
    def test_get_reports_lists_alignment_indent_and_inline_formats(self):
        self.use_doc([
            _para("Intro", bold=True),
            _para("Item", list_id=BULLET_LIST, indent_start=36),
            _para("Step", list_id=NUMBER_LIST, level=1),
            _para("Centered", alignment="CENTER", link="https://example.com"),
            _para("Plain"),
        ])

        paragraphs = docs_edit.get(DOC_ID)["paragraphs"]

        self.assertEqual(paragraphs[0]["formats"], ["bold"])
        self.assertEqual(paragraphs[1]["list"], {"id": BULLET_LIST, "level": 0, "type": "bullet"})
        self.assertEqual(paragraphs[1]["indent_start"], 36)
        self.assertEqual(paragraphs[2]["list"], {"id": NUMBER_LIST, "level": 1, "type": "numbered"})
        self.assertEqual(paragraphs[3]["alignment"], "CENTER")
        self.assertEqual(paragraphs[3]["formats"], ["link"])
        for key in ("list", "alignment", "indent_start", "formats"):
            self.assertNotIn(key, paragraphs[4])


class FormatParagraphsTest(FormattingTestCase):
    def test_list_none_strips_bullets_and_indent_from_selected_range(self):
        self.use_doc([
            _para("Heading text", list_id=BULLET_LIST, indent_start=36),   # 1-14
            _para("Body text", list_id=BULLET_LIST, indent_start=36),      # 14-24
            _para("Kept item", list_id=BULLET_LIST, indent_start=36),      # 24-34
        ])

        result = docs_edit.format_paragraphs(
            DOC_ID, [{"from": "Heading text", "to": "Body text", "list": "none"}]
        )

        self.assertTrue(result["ok"])
        self.assertEqual(self.requests(), [
            {"deleteParagraphBullets": {"range": {"startIndex": 1, "endIndex": 24}}},
            {"updateParagraphStyle": {
                "range": {"startIndex": 1, "endIndex": 24},
                "paragraphStyle": {
                    "indentStart": {"magnitude": 0, "unit": "PT"},
                    "indentFirstLine": {"magnitude": 0, "unit": "PT"},
                },
                "fields": "indentStart,indentFirstLine",
            }},
        ])

    def test_list_none_skips_delete_for_paragraphs_that_are_not_bulleted(self):
        self.use_doc([_para("Plain"), _para("Item", list_id=BULLET_LIST)])

        docs_edit.format_paragraphs(DOC_ID, [{"match_regex": ".", "list": "none"}])

        deletes = [r for r in self.requests() if "deleteParagraphBullets" in r]
        self.assertEqual(deletes, [
            {"deleteParagraphBullets": {"range": {"startIndex": 7, "endIndex": 12}}},
        ])

    def test_numbered_list_groups_consecutive_paragraphs_into_one_list(self):
        self.use_doc([
            _para("One", list_id=BULLET_LIST),     # 1-5
            _para("Two", list_id=BULLET_LIST),     # 5-9
            _para("Between"),                      # 9-17
            _para("Three", list_id=BULLET_LIST),   # 17-23
        ])

        docs_edit.format_paragraphs(DOC_ID, [
            {"match_regex": "^(One|Two|Three)$", "list": "numbered"},
        ])

        creates = [r["createParagraphBullets"] for r in self.requests()
                   if "createParagraphBullets" in r]
        # Separate groups restart numbering; applied end-of-document first.
        self.assertEqual(creates, [
            {"range": {"startIndex": 17, "endIndex": 23}, "bulletPreset": "NUMBERED_DECIMAL_NESTED"},
            {"range": {"startIndex": 1, "endIndex": 9}, "bulletPreset": "NUMBERED_DECIMAL_NESTED"},
        ])

    def test_bullets_are_created_after_all_other_edits(self):
        self.use_doc([_para("Item"), _para("Title")])

        docs_edit.format_paragraphs(DOC_ID, [
            {"from": "Item", "list": "bullet"},
            {"from": "Title", "style": "heading 1"},
        ])

        kinds = [next(iter(r)) for r in self.requests()]
        self.assertEqual(kinds, ["updateParagraphStyle", "createParagraphBullets"])

    def test_style_alignment_spacing_and_clear_text_formatting(self):
        self.use_doc([_para("Title", bold=True)])

        docs_edit.format_paragraphs(DOC_ID, [{
            "from": "Title",
            "style": "H2",
            "alignment": "center",
            "space_below": 6,
            "line_spacing": 115,
            "clear_text_formatting": True,
        }])

        update, clear = self.requests()
        self.assertEqual(update["updateParagraphStyle"]["paragraphStyle"], {
            "namedStyleType": "HEADING_2",
            "alignment": "CENTER",
            "spaceBelow": {"magnitude": 6, "unit": "PT"},
            "lineSpacing": 115,
        })
        self.assertEqual(
            update["updateParagraphStyle"]["fields"],
            "namedStyleType,alignment,spaceBelow,lineSpacing",
        )
        self.assertEqual(clear["updateTextStyle"]["textStyle"], {})
        self.assertIn("bold", clear["updateTextStyle"]["fields"])
        self.assertIn("foregroundColor", clear["updateTextStyle"]["fields"])

    def test_later_operations_override_earlier_ones_for_the_same_paragraph(self):
        self.use_doc([
            _para("A", list_id=BULLET_LIST),
            _para("B", list_id=BULLET_LIST),
            _para("C", list_id=BULLET_LIST),
        ])

        docs_edit.format_paragraphs(DOC_ID, [
            {"match_regex": ".", "list": "none"},
            {"from": "B", "list": "numbered"},
        ])

        requests = self.requests()
        creates = [r["createParagraphBullets"] for r in requests if "createParagraphBullets" in r]
        self.assertEqual(creates, [
            {"range": {"startIndex": 3, "endIndex": 5}, "bulletPreset": "NUMBERED_DECIMAL_NESTED"},
        ])
        deletes = [r["deleteParagraphBullets"]["range"] for r in requests
                   if "deleteParagraphBullets" in r]
        self.assertEqual(deletes, [{"startIndex": 1, "endIndex": 3}, {"startIndex": 5, "endIndex": 7}])

    def test_regex_can_be_limited_to_a_range(self):
        self.use_doc([_para("x1"), _para("x2"), _para("x3"), _para("x4")])

        docs_edit.format_paragraphs(DOC_ID, [
            {"from": "x2", "to": "x3", "match_regex": "^x", "alignment": "right"},
        ])

        (update,) = self.requests()
        self.assertEqual(update["updateParagraphStyle"]["range"], {"startIndex": 4, "endIndex": 10})
        self.assertEqual(update["updateParagraphStyle"]["paragraphStyle"], {"alignment": "END"})

    def test_dry_run_reports_matches_without_writing(self):
        self.use_doc([_para("Alpha", list_id=BULLET_LIST), _para("Beta")])

        result = docs_edit.format_paragraphs(
            DOC_ID, [{"from": "Alpha", "to": "Beta", "list": "none"}], dry_run=True
        )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["operations"][0]["matched"], 2)
        self.assertEqual(result["operations"][0]["preview"][0]["text"], "Alpha")
        self.assertEqual(result["operations"][0]["preview"][0]["list"], "bullet")
        self.assert_no_write()

    def test_errors_do_not_write(self):
        self.use_doc([_para("Alpha")])
        bad_operations = [
            {"from": "Missing", "list": "none"},                       # anchor not found
            {"from": "Alpha"},                                          # nothing to change
            {"list": "none"},                                           # no selection
            {"from": "Alpha", "style": "gigantic"},                     # bad style
            {"from": "Alpha", "list": "bulleted-ish"},                  # bad list
            {"from": "Alpha", "alignment": "diagonal"},                 # bad alignment
            {"from": "Alpha", "bogus": 1, "list": "none"},              # typo'd key
            {"match_regex": "zzz", "list": "none"},                     # matches nothing
        ]
        for op in bad_operations:
            with self.subTest(op=op), self.assertRaises(ValueError):
                docs_edit.format_paragraphs(DOC_ID, [op])
        self.assert_no_write()

    def test_paragraphs_separated_by_a_table_are_not_merged(self):
        self.use_doc([_para("One"), _para("Two")])
        doc = self.service.documents().get().execute.return_value
        # Push "Two" 10 characters later, as if a table sat between the paragraphs.
        two = doc["body"]["content"][2]
        two["startIndex"] += 10
        two["endIndex"] += 10
        run = two["paragraph"]["elements"][0]
        run["startIndex"] += 10
        run["endIndex"] += 10

        docs_edit.format_paragraphs(DOC_ID, [{"match_regex": ".", "alignment": "center"}])

        ranges = [r["updateParagraphStyle"]["range"] for r in self.requests()]
        self.assertEqual(ranges, [
            {"startIndex": 1, "endIndex": 5},
            {"startIndex": 15, "endIndex": 19},
        ])


class FormatTextTest(FormattingTestCase):
    def test_bold_every_match_of_a_phrase(self):
        self.use_doc([_para("Note: a"), _para("and Note: b")])  # 1-9, 9-21

        result = docs_edit.format_text(
            DOC_ID, [{"find": "Note:", "occurrence": 0, "bold": True, "color": "#FF0000"}]
        )

        self.assertEqual(result["operations"][0]["matched"], 2)
        self.assertEqual(self.requests(), [
            {"updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": 6},
                "textStyle": {
                    "bold": True,
                    "foregroundColor": {"color": {"rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}}},
                },
                "fields": "bold,foregroundColor",
            }},
            {"updateTextStyle": {
                "range": {"startIndex": 13, "endIndex": 18},
                "textStyle": {
                    "bold": True,
                    "foregroundColor": {"color": {"rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}}},
                },
                "fields": "bold,foregroundColor",
            }},
        ])

    def test_occurrence_selects_one_match(self):
        self.use_doc([_para("aa aa aa")])

        docs_edit.format_text(DOC_ID, [{"find": "aa", "occurrence": 2, "italic": True}])

        (update,) = self.requests()
        self.assertEqual(update["updateTextStyle"]["range"], {"startIndex": 4, "endIndex": 6})

    def test_regex_find_and_scope_limit(self):
        self.use_doc([_para("v1"), _para("v2"), _para("v3")])  # 1-4, 4-7, 7-10

        docs_edit.format_text(DOC_ID, [
            {"find": r"v\d", "regex": True, "occurrence": 0, "underline": True,
             "from": "v2", "to": "v3"},
        ])

        ranges = [r["updateTextStyle"]["range"] for r in self.requests()]
        self.assertEqual(ranges, [{"startIndex": 4, "endIndex": 6}, {"startIndex": 7, "endIndex": 9}])

    def test_whole_paragraphs_without_the_newline(self):
        self.use_doc([_para("Heading", bold=True), _para("Body", bold=True)])

        docs_edit.format_text(DOC_ID, [{"from": "Body", "bold": False}])

        (update,) = self.requests()
        self.assertEqual(update["updateTextStyle"]["range"], {"startIndex": 9, "endIndex": 13})
        self.assertEqual(update["updateTextStyle"]["textStyle"], {"bold": False})

    def test_clear_then_apply_and_link_handling(self):
        self.use_doc([_para("Click here", link="https://old.example")])

        docs_edit.format_text(DOC_ID, [
            {"from": "Click", "clear": True, "link": "https://new.example", "font_size": 11,
             "font_family": "Arial", "background": "ffff00"},
        ])

        clear, apply = self.requests()
        self.assertEqual(clear["updateTextStyle"]["textStyle"], {})
        self.assertIn("link", clear["updateTextStyle"]["fields"])
        style = apply["updateTextStyle"]["textStyle"]
        self.assertEqual(style["link"], {"url": "https://new.example"})
        self.assertEqual(style["fontSize"], {"magnitude": 11, "unit": "PT"})
        self.assertEqual(style["weightedFontFamily"], {"fontFamily": "Arial"})
        self.assertEqual(
            style["backgroundColor"],
            {"color": {"rgbColor": {"red": 1.0, "green": 1.0, "blue": 0.0}}},
        )

    def test_empty_link_removes_it(self):
        self.use_doc([_para("Click here", link="https://old.example")])

        docs_edit.format_text(DOC_ID, [{"find": "Click", "link": ""}])

        (update,) = self.requests()
        self.assertEqual(update["updateTextStyle"]["textStyle"], {})
        self.assertEqual(update["updateTextStyle"]["fields"], "link")

    def test_match_spanning_paragraphs_is_one_range(self):
        self.use_doc([_para("end of one"), _para("start of two")])

        docs_edit.format_text(
            DOC_ID, [{"find": "one\nstart", "bold": True}]
        )

        (update,) = self.requests()
        self.assertEqual(update["updateTextStyle"]["range"], {"startIndex": 8, "endIndex": 17})

    def test_indices_count_utf16_code_units(self):
        self.use_doc([_para("\U0001F600 Note: x")])  # the emoji is two code units

        docs_edit.format_text(DOC_ID, [{"find": "Note:", "bold": True}])

        (update,) = self.requests()
        self.assertEqual(update["updateTextStyle"]["range"], {"startIndex": 4, "endIndex": 9})

    def test_dry_run_and_errors_do_not_write(self):
        self.use_doc([_para("Hello")])

        result = docs_edit.format_text(DOC_ID, [{"find": "Hello", "bold": True}], dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["operations"][0]["preview"], ["Hello"])

        bad_operations = [
            {"find": "Missing", "bold": True},                 # not found
            {"find": "Hello", "occurrence": 3, "bold": True},  # occurrence out of range
            {"find": "Hello"},                                  # nothing to apply
            {"bold": True},                                     # no selection
            {"find": "Hello", "bold": "yes"},                   # not a boolean
            {"find": "Hello", "color": "red"},                  # not a hex colour
            {"find": "Hello", "boldd": True},                   # typo'd key
        ]
        for op in bad_operations:
            with self.subTest(op=op), self.assertRaises(ValueError):
                docs_edit.format_text(DOC_ID, [op])
        self.assert_no_write()


if __name__ == "__main__":
    unittest.main()
