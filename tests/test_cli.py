"""The docs_edit.py command line mirrors the tools, including --suggest."""

import contextlib
import io
import json
import sys
import unittest
from unittest import mock

import docs_edit

EDIT_COMMANDS = {
    "search_replace": (["--find", "a", "--replace", "b"], "search_replace"),
    "insert_after": (["--anchor", "a", "--text", "t"], "insert_after"),
    "insert_before": (["--anchor", "a", "--text", "t"], "insert_before"),
    "delete_paragraph": (["--anchor", "a"], "delete_paragraph"),
    "append": (["--text", "t"], "append"),
    "batch_replace": (["--replacements", '[{"find": "a", "replace": "b"}]'], "batch_replace"),
}


def run_cli(*argv: str) -> dict:
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["docs_edit.py", *argv]), contextlib.redirect_stdout(out):
        docs_edit.main()
    return json.loads(out.getvalue())


class CliTest(unittest.TestCase):
    def test_every_edit_command_takes_suggest(self):
        for command, (arguments, function) in EDIT_COMMANDS.items():
            with self.subTest(command), mock.patch.object(
                docs_edit, function, return_value={"ok": True}
            ) as edit:
                run_cli(command, "DOC", *arguments, "--suggest")

                self.assertIs(edit.call_args.kwargs["suggest"], True)

    def test_edits_are_direct_without_the_flag(self):
        for command, (arguments, function) in EDIT_COMMANDS.items():
            with self.subTest(command), mock.patch.object(
                docs_edit, function, return_value={"ok": True}
            ) as edit:
                run_cli(command, "DOC", *arguments)

                self.assertIs(edit.call_args.kwargs["suggest"], False)

    def test_list_suggestions(self):
        with mock.patch.object(docs_edit, "list_suggestions", return_value={"count": 0}) as listing:
            run_cli("list_suggestions", "DOC", "--include-closed")

        listing.assert_called_once_with("DOC", include_closed=True)

    def test_review_suggestions(self):
        with mock.patch.object(docs_edit, "review_suggestions", return_value={"ok": True}) as review:
            run_cli("review_suggestions", "DOC", "--action", "accept", "--ids", "s1", "s2")

        review.assert_called_once_with("DOC", "accept", ["s1", "s2"])

    def test_convert(self):
        with mock.patch.object(docs_edit, "convert_to_google_doc", return_value={"ok": True}) as convert:
            run_cli("convert", "FILE", "--name", "Working copy")
            run_cli("convert", "FILE")

        self.assertEqual(convert.call_args_list, [mock.call("FILE", "Working copy"), mock.call("FILE", "")])

    def test_review_needs_a_known_action(self):
        with mock.patch.object(sys, "argv", ["docs_edit.py", "review_suggestions", "DOC", "--action", "approve", "--ids", "s1"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                docs_edit.main()


if __name__ == "__main__":
    unittest.main()
