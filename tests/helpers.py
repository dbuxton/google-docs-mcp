"""Shared fixtures: fake Docs/Drive services and a builder for Docs API document JSON."""

import unittest
from unittest import mock

import httplib2
from googleapiclient.errors import HttpError

import docs_edit

DOC_ID = "1gFJPPFPv2tuw11jAzzH4tjE-bQjSWQa6"


def http_error(status: int, message: str) -> HttpError:
    return HttpError(
        httplib2.Response({"status": status}),
        ('{"error": {"code": %d, "message": "%s"}}' % (status, message)).encode(),
    )


# --- Document JSON ----------------------------------------------------------

def inserted(text: str, *suggestion_ids: str) -> dict:
    """A text run a suggestion inserts."""
    return {"content": text, "suggestedInsertionIds": list(suggestion_ids)}


def deleted(text: str, *suggestion_ids: str) -> dict:
    """A text run a suggestion deletes."""
    return {"content": text, "suggestedDeletionIds": list(suggestion_ids)}


def restyled(text: str, suggestion_id: str, **fields: bool) -> dict:
    """A text run a suggestion restyles, e.g. restyled("x", "s1", bold=True)."""
    return {
        "content": text,
        "suggestedTextStyleChanges": {
            suggestion_id: {
                "textStyle": {name: value for name, value in fields.items()},
                "textStyleSuggestionState": {f"{name}Suggested": True for name in fields},
            }
        },
    }


class DocBuilder:
    """
    Builds a Docs API document with correct indices, the way SUGGESTIONS_INLINE returns it.

    A run is a string or a dict from inserted() / deleted() / restyled(). Paragraphs get
    their closing newline automatically unless newline=False (use that when the newline
    is itself part of a suggestion). Tables follow the real index accounting: one index
    each for the table, every row and every cell.
    """

    def __init__(self):
        self.index = 1
        self.content = [{"startIndex": 0, "endIndex": 1, "sectionBreak": {}}]

    def _paragraph(self, runs, style="NORMAL_TEXT", newline=True, extras=None) -> dict:
        runs = list(runs) + (["\n"] if newline else [])
        start = self.index
        elements = []
        for run in runs:
            text_run = {"content": run} if isinstance(run, str) else dict(run)
            end = self.index + len(text_run["content"].encode("utf-16-le")) // 2  # Docs counts UTF-16 units
            elements.append({"startIndex": self.index, "endIndex": end, "textRun": text_run})
            self.index = end
        return {
            "startIndex": start,
            "endIndex": self.index,
            "paragraph": {
                "elements": elements,
                "paragraphStyle": {"namedStyleType": style},
                **(extras or {}),
            },
        }

    def paragraph(self, *runs, style="NORMAL_TEXT", newline=True, **extras) -> "DocBuilder":
        self.content.append(self._paragraph(runs, style, newline, extras))
        return self

    def table(self, rows: list[list]) -> "DocBuilder":
        """rows -> cells -> text, or a list of texts for a cell holding several paragraphs."""
        start = self.index
        self.index += 1
        table_rows = []
        for row in rows:
            row_start = self.index
            self.index += 1
            cells = []
            for cell in row:
                cell_start = self.index
                self.index += 1
                texts = [cell] if isinstance(cell, (str, dict)) else cell
                content = [self._paragraph([text]) for text in texts]
                cells.append({"startIndex": cell_start, "endIndex": self.index, "content": content})
            table_rows.append({"startIndex": row_start, "endIndex": self.index, "tableCells": cells})
        self.content.append({
            "startIndex": start,
            "endIndex": self.index,
            "table": {"rows": len(rows), "columns": len(rows[0]), "tableRows": table_rows},
        })
        return self

    def build(self, title: str = "Contract", **extra) -> dict:
        return {"title": title, "body": {"content": self.content}, **extra}


# --- Fake services ----------------------------------------------------------

class DocsTestCase(unittest.TestCase):
    """Replaces docs_edit's Docs and Drive services with mocks."""

    def setUp(self):
        self.docs = mock.MagicMock()
        self.drive = mock.MagicMock()
        services = {"docs": self.docs, "drive": self.drive}
        patcher = mock.patch.object(
            docs_edit, "_get_service", side_effect=lambda api, version: services[api]
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.batch_update = self.docs.documents().batchUpdate

    def serve(self, doc: dict) -> None:
        """Make documents.get return `doc`."""
        # .return_value rather than calling, so setting up leaves no calls on record.
        self.docs.documents.return_value.get.return_value.execute.return_value = doc

    def reply(self, response: dict) -> None:
        """Make documents.batchUpdate return `response`."""
        self.batch_update.return_value.execute.return_value = response

    def body(self) -> dict:
        """The body of the last batchUpdate call."""
        return self.batch_update.call_args.kwargs["body"]

    def requests(self) -> list:
        return self.body()["requests"]

    def drive_file(self, name: str, mime_type: str, size: int = 1024) -> None:
        """Make Drive say the file is `name` of `mime_type`."""
        self.drive.files.return_value.get.return_value.execute.return_value = {
            "name": name,
            "mimeType": mime_type,
            "size": str(size),
        }


def saved_as_suggestion(*created_per_request: list[str]) -> dict:
    """A batchUpdate response for a suggest-mode write that Google saved."""
    return {
        "replies": [{} for _ in created_per_request],
        "commentUpdateState": "ALL_SAVED",
        "suggestionResponses": [
            {"createdSuggestionIds": list(ids)} for ids in created_per_request
        ],
    }
