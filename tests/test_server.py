"""The MCP surface: which tools exist, what they are called, and that options reach docs_edit."""

import asyncio
import json
import logging
import re
import unittest
from unittest import mock

from fastmcp import Client
from fastmcp.exceptions import ToolError

import docs_edit
import server

EDIT_TOOLS = {
    "docs_search_replace": "search_replace",
    "docs_insert_after": "insert_after",
    "docs_insert_before": "insert_before",
    "docs_delete_paragraph": "delete_paragraph",
    "docs_append": "append",
    "docs_batch_replace": "batch_replace",
}

# Minimal valid arguments for each edit tool.
EDIT_ARGUMENTS = {
    "docs_search_replace": {"doc_id": "d", "find": "a", "replace": "b"},
    "docs_insert_after": {"doc_id": "d", "anchor": "a", "text": "t"},
    "docs_insert_before": {"doc_id": "d", "anchor": "a", "text": "t"},
    "docs_delete_paragraph": {"doc_id": "d", "anchor": "a"},
    "docs_append": {"doc_id": "d", "text": "t"},
    "docs_batch_replace": {"doc_id": "d", "replacements_json": '[{"find": "a", "replace": "b"}]'},
}


def schema_of(tool) -> dict:
    # FastMCP 3+ calls it input_schema and warns about inputSchema; older versions only have the latter.
    return getattr(tool, "input_schema", None) or tool.inputSchema


def list_tools():
    async def run():
        async with Client(server.mcp) as client:
            return await client.list_tools()

    return {tool.name: tool for tool in asyncio.run(run())}


def result_json(result) -> dict:
    # FastMCP 2.10+ returns a result object with .content; older versions the content list itself.
    content = getattr(result, "content", result)
    return json.loads(content[0].text)


def call_tool(tool: str, /, **arguments):
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(run())


class ToolSurfaceTest(unittest.TestCase):
    def test_suggestion_tools_exist(self):
        tools = list_tools()

        self.assertIn("docs_list_suggestions", tools)
        self.assertIn("docs_review_suggestions", tools)

    def test_every_edit_tool_takes_suggest_defaulting_to_false(self):
        tools = list_tools()

        for name in EDIT_TOOLS:
            with self.subTest(name):
                schema = schema_of(tools[name])
                self.assertEqual(schema["properties"]["suggest"]["type"], "boolean")
                self.assertIs(schema["properties"]["suggest"]["default"], False)
                self.assertNotIn("suggest", schema["required"])

    def test_review_action_is_limited_to_the_three_actions(self):
        schema = schema_of(list_tools()["docs_review_suggestions"])

        self.assertEqual(schema["properties"]["action"]["enum"], ["accept", "reject", "delete"])
        self.assertEqual(schema["properties"]["suggestion_ids"]["type"], "array")
        self.assertEqual(schema["required"], ["doc_id", "action", "suggestion_ids"])

    def test_the_server_instructions_give_every_tool_its_exact_parameter_names(self):
        # Agents that meet a tool before its schema is loaded rely on this list, so it
        # must not drift from the real signatures.
        listed = {}
        for line in server.mcp.instructions.splitlines():
            match = re.match(r"^\s+(docs_\w+)\(([^)]*)\)", line)
            if match:
                name, params = match.groups()
                listed[name] = [p.split("=")[0].strip() for p in params.split(",") if p.strip()]

        tools = list_tools()
        self.assertEqual(sorted(listed), sorted(tools))
        for name, tool in tools.items():
            with self.subTest(name):
                self.assertEqual(listed[name], list(schema_of(tool)["properties"]))

    def test_the_instructions_show_which_parameters_have_defaults(self):
        defaults = {}
        for line in server.mcp.instructions.splitlines():
            match = re.match(r"^\s+(docs_\w+)\(([^)]*)\)", line)
            if match:
                name, params = match.groups()
                defaults[name] = {
                    p.split("=")[0].strip() for p in params.split(",") if "=" in p
                }

        for name, tool in list_tools().items():
            with self.subTest(name):
                required = set(schema_of(tool).get("required", []))
                optional = set(schema_of(tool)["properties"]) - required
                self.assertEqual(defaults[name], optional)


class SuggestOptionReachesDocsEditTest(unittest.TestCase):
    def setUp(self):
        # FastMCP logs a traceback for every tool call that fails, and some of these are meant to.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_suggest_is_passed_through_by_every_edit_tool(self):
        for tool, function in EDIT_TOOLS.items():
            with self.subTest(tool), mock.patch.object(
                docs_edit, function, return_value={"ok": True}
            ) as edit:
                call_tool(tool, **EDIT_ARGUMENTS[tool], suggest=True)

                self.assertIs(edit.call_args.kwargs["suggest"], True)

    def test_edits_are_direct_when_suggest_is_not_given(self):
        for tool, function in EDIT_TOOLS.items():
            with self.subTest(tool), mock.patch.object(
                docs_edit, function, return_value={"ok": True}
            ) as edit:
                call_tool(tool, **EDIT_ARGUMENTS[tool])

                self.assertIs(edit.call_args.kwargs["suggest"], False)

    def test_the_result_comes_back_as_json(self):
        reply = {"ok": True, "suggested": True, "suggestion_ids": ["suggest.a"]}
        with mock.patch.object(docs_edit, "search_replace", return_value=reply):
            result = call_tool("docs_search_replace", doc_id="d", find="a", replace="b", suggest=True)

        self.assertEqual(result_json(result), reply)

    def test_an_unconfirmed_suggestion_reaches_the_agent_as_an_error(self):
        failure = docs_edit.SuggestionNotSavedError("It may have been applied directly to the document")
        with mock.patch.object(docs_edit, "search_replace", side_effect=failure):
            with self.assertRaisesRegex(ToolError, "applied directly"):
                call_tool("docs_search_replace", doc_id="d", find="a", replace="b", suggest=True)

    def test_suggestions_can_be_listed_and_reviewed(self):
        with mock.patch.object(docs_edit, "list_suggestions", return_value={"count": 0}) as listing:
            call_tool("docs_list_suggestions", doc_id="d", include_closed=True)
        listing.assert_called_once_with("d", True)

        with mock.patch.object(docs_edit, "review_suggestions", return_value={"ok": True}) as review:
            call_tool("docs_review_suggestions", doc_id="d", action="reject", suggestion_ids=["s1", "s2"])
        review.assert_called_once_with("d", "reject", ["s1", "s2"])

    def test_an_unknown_review_action_is_rejected_before_docs_edit_runs(self):
        with mock.patch.object(docs_edit, "review_suggestions") as review:
            with self.assertRaises(ToolError):
                call_tool("docs_review_suggestions", doc_id="d", action="approve", suggestion_ids=["s1"])

        review.assert_not_called()


class ConvertToolTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_the_tool_needs_only_a_file_id(self):
        schema = schema_of(list_tools()["docs_convert_to_google_doc"])

        self.assertEqual(schema["required"], ["file_id"])
        self.assertEqual(list(schema["properties"]), ["file_id", "name"])

    def test_arguments_reach_docs_edit_and_the_result_comes_back_as_json(self):
        reply = {"ok": True, "converted": True, "id": "new"}
        with mock.patch.object(docs_edit, "convert_to_google_doc", return_value=reply) as convert:
            result = call_tool("docs_convert_to_google_doc", file_id="f1", name="Working copy")
            call_tool("docs_convert_to_google_doc", file_id="f2")

        self.assertEqual(convert.call_args_list, [mock.call("f1", "Working copy"), mock.call("f2", "")])
        self.assertEqual(result_json(result), reply)

    def test_a_file_that_cannot_be_converted_reaches_the_agent_as_an_error(self):
        failure = ValueError('"Budget" is a Google Sheet, which cannot be converted to a Google Doc.')
        with mock.patch.object(docs_edit, "convert_to_google_doc", side_effect=failure):
            with self.assertRaisesRegex(ToolError, "cannot be converted"):
                call_tool("docs_convert_to_google_doc", file_id="sheet")


if __name__ == "__main__":
    unittest.main()
