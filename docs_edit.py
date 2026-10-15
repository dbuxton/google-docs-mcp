#!/usr/bin/env python3
"""
docs_edit.py — Surgical Google Docs editor
==========================================
Edits Google Docs in-place using real batchUpdate requests, preserving
document history, comments, and suggestions. LLMs never touch indices.

Every edit command takes --suggest to record the change as a suggestion (like
Docs' Suggesting mode) for someone to accept or reject, instead of editing the text.

Usage (CLI):
    docs_edit.py get <docId>
    docs_edit.py search_replace <docId> --find "old" --replace "new" [--occurrence 1] [--regex] [--suggest]
    docs_edit.py insert_after <docId> --anchor "paragraph text" --text "new text" [--plain] [--suggest]
    docs_edit.py insert_before <docId> --anchor "paragraph text" --text "new text" [--plain] [--suggest]
    docs_edit.py delete_paragraph <docId> --anchor "paragraph text" [--suggest]
    docs_edit.py append <docId> --text "text to append" [--plain] [--suggest]
    docs_edit.py batch_replace <docId> --replacements '[{"find":"a","replace":"b"}]' [--suggest]
    docs_edit.py list_suggestions <docId> [--include-closed]
    docs_edit.py review_suggestions <docId> --action accept|reject|delete --ids ID [ID ...]
    docs_edit.py convert <fileId> [--name "title"]
    docs_edit.py add_comment <docId> --anchor "text" --comment "note" [--occurrence 1] [--assignee a@b.com]
    docs_edit.py read_comments <docId> [--include-resolved]

Rich mode is ON by default and supports a small markdown-like subset:
    # / ## / ### headings
    - item / * item bullet lists
    1. item / 1) item numbered lists
    **bold**, *italic*, ***bold italic***

Auth (in priority order):
    1. GOOGLE_DOCS_MCP_TOKEN env var  → path to standalone token JSON
    2. GOOGLE_DOCS_TOKEN_FILE env var → legacy gog-exported token JSON
    3. Default standalone path        → ~/.google-docs-mcp/token.json
    4. GOG_KEYRING_PASSWORD env var   → auto-export from gog
    5. Cached gog token               → /tmp/docs_edit_token_cache.json

Legacy GOOGLE_DRIVE_MCP_* env var aliases still work.

Python API:
    from docs_edit import get, search_replace, insert_after, insert_before,
                          delete_paragraph, append, batch_replace,
                          list_suggestions, review_suggestions, convert_to_google_doc,
                          add_comment,
                          read_comments, reply_to_comment, resolve_comment,
                          delete_comment
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("docs_edit")

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

# Standalone defaults (auth_setup.py output)
STANDALONE_TOKEN_PATHS = [
    Path.home() / ".google-docs-mcp" / "token.json",
    Path.home() / ".google-drive-mcp" / "token.json",
]
TOKEN_ENV_VAR = "GOOGLE_DOCS_MCP_TOKEN"
TOKEN_ENV_ALIASES = (TOKEN_ENV_VAR, "GOOGLE_DRIVE_MCP_TOKEN")
CLIENT_ID_ENV_VAR = "GOOGLE_DOCS_MCP_CLIENT_ID"
CLIENT_SECRET_ENV_VAR = "GOOGLE_DOCS_MCP_CLIENT_SECRET"
CLIENT_ID_ENV_ALIASES = (CLIENT_ID_ENV_VAR, "GOOGLE_DRIVE_MCP_CLIENT_ID")
CLIENT_SECRET_ENV_ALIASES = (CLIENT_SECRET_ENV_VAR, "GOOGLE_DRIVE_MCP_CLIENT_SECRET")

# Gog fallback paths (backward compat)
GOG_CREDENTIALS_PATH = Path("/config/gogcli/credentials.json")
GOG_TOKEN_CACHE = Path("/tmp/docs_edit_token_cache.json")


def _export_gog_token(email: str = "david@harriethq.com") -> dict:
    """Export token from gog keyring using GOG_KEYRING_PASSWORD env var."""
    password = os.environ.get("GOG_KEYRING_PASSWORD")
    if not password:
        raise RuntimeError("GOG_KEYRING_PASSWORD not set")
    env = {**os.environ, "GOG_KEYRING_PASSWORD": password}
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    try:
        result = subprocess.run(
            ["gog", "auth", "tokens", "export", email, "--out", tmp, "--overwrite", "-y"],
            env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gog token export failed: {result.stderr}")
        return json.loads(Path(tmp).read_text())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _load_token() -> dict:
    """
    Load token data. Priority:
      1. GOOGLE_DOCS_MCP_TOKEN env var   (standalone auth_setup.py output)
      2. GOOGLE_DRIVE_MCP_TOKEN env var  (backward-compatible alias)
      3. GOOGLE_DOCS_TOKEN_FILE env var  (legacy / gog compat)
      4. ~/.google-docs-mcp/token.json   (standalone default location)
      5. ~/.google-drive-mcp/token.json  (backward-compatible fallback)
      6. GOG_KEYRING_PASSWORD auto-export (gog compat)
      7. Cached gog token
    """
    # 1. Standalone env vars
    token_file = _first_env(*TOKEN_ENV_ALIASES)
    if token_file:
        return json.loads(Path(token_file).read_text())

    # 2. Legacy env var
    token_file = os.environ.get("GOOGLE_DOCS_TOKEN_FILE")
    if token_file:
        return json.loads(Path(token_file).read_text())

    # 3. Default standalone locations
    for token_path in STANDALONE_TOKEN_PATHS:
        if token_path.exists():
            return json.loads(token_path.read_text())

    # 4. Gog auto-export
    try:
        token = _export_gog_token()
        GOG_TOKEN_CACHE.write_text(json.dumps(token))
        return token
    except RuntimeError as e:
        log.warning("Gog auto-export failed: %s", e)

    # 5. Gog cache
    if GOG_TOKEN_CACHE.exists():
        log.info("Using cached gog token")
        return json.loads(GOG_TOKEN_CACHE.read_text())

    raise RuntimeError(
        "No Google credentials found.\n"
        "Run: python3 auth_setup.py --credentials ~/credentials.json\n"
        "Or set GOOGLE_DOCS_MCP_TOKEN to your token file path "
        "(legacy GOOGLE_DRIVE_MCP_TOKEN also works)."
    )


def _load_creds():
    """Return refreshed google.oauth2.credentials.Credentials."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_data = _load_token()

    # Self-contained token — auth_setup.py embeds client_id / client_secret directly
    # (also handle legacy _client_id / _client_secret with underscore prefix)
    client_id = token_data.get("client_id") or token_data.get("_client_id")
    client_secret = token_data.get("client_secret") or token_data.get("_client_secret")
    token_uri = token_data.get("token_uri") or token_data.get("_token_uri", "https://oauth2.googleapis.com/token")

    # Environment override allows using external client credentials instead of a credentials.json file.
    client_id = client_id or _first_env(*CLIENT_ID_ENV_ALIASES)
    client_secret = client_secret or _first_env(*CLIENT_SECRET_ENV_ALIASES)

    # Gog compat: read from separate credentials.json if not embedded
    if not client_id and GOG_CREDENTIALS_PATH.exists():
        gog_creds = json.loads(GOG_CREDENTIALS_PATH.read_text())
        client_id = gog_creds.get("client_id")
        client_secret = gog_creds.get("client_secret")

    if not client_id or not client_secret:
        raise RuntimeError(
            "No OAuth client credentials found. Re-run auth_setup.py to generate a self-contained token, "
            f"or set {CLIENT_ID_ENV_VAR} and {CLIENT_SECRET_ENV_VAR} "
            "(legacy GOOGLE_DRIVE_MCP_* aliases also work)."
        )

    scopes = [s for s in token_data.get("scopes", []) if s not in ("email", "openid")]

    creds = Credentials(
        token=None,
        refresh_token=token_data["refresh_token"],
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )
    creds.refresh(Request())
    return creds


def _get_service(api: str = "docs", version: str = "v1"):
    """Build a Google API service client."""
    from googleapiclient.discovery import build
    return build(api, version, credentials=_load_creds())


SUGGESTIONS_VIEW_MODE = "SUGGESTIONS_INLINE"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
PDF_MIME_TYPE = "application/pdf"
MAX_PDF_DOWNLOAD_BYTES = 20 * 1024 * 1024
# How messages name the Drive files the Docs API will not edit; anything else is named by its MIME type.
FILE_TYPE_NAMES = {
    PDF_MIME_TYPE: "a PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "a Word document (.docx)",
    "application/msword": "a Word document (.doc)",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "an Excel spreadsheet (.xlsx)",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "a PowerPoint file (.pptx)",
}


class NotAGoogleDocError(RuntimeError):
    """The ID belongs to a Drive file the Docs API cannot open (PDF, .docx, Sheet...)."""

    def __init__(self, doc_id: str, name: str, mime_type: str, size: int | None):
        self.doc_id = doc_id
        self.name = name
        self.mime_type = mime_type
        self.size = size
        self.kind = FILE_TYPE_NAMES.get(mime_type, f"a file of type {mime_type}")
        if mime_type == PDF_MIME_TYPE:
            message = (
                f'"{name}" is {self.kind}, not a Google Doc, so it cannot be edited with these tools. '
                "docs_get can read its text, and the comment tools can add and read Drive "
                "comments on it. docs_convert_to_google_doc can make an approximate, editable copy."
            )
        else:
            message = (
                f'"{name}" is {self.kind}, not a Google Doc, so these tools cannot '
                "edit it (and may not be able to read it). The comment tools can still add, "
                "read and reply to Drive comments on it."
                + (
                    " docs_convert_to_google_doc makes an editable Google Doc copy."
                    if mime_type in CONVERTIBLE_MIME_TYPES
                    else ""
                )
            )
        super().__init__(message)


def _non_google_doc(doc_id: str) -> NotAGoogleDocError | None:
    """Ask Drive what the file is: a NotAGoogleDocError describing it, or None for a Google Doc."""
    drive_file = _get_service("drive", "v3").files().get(
        fileId=doc_id,
        fields="name,mimeType,size",
        supportsAllDrives=True,
    ).execute()
    if drive_file["mimeType"] == GOOGLE_DOC_MIME_TYPE:
        return None
    size = drive_file.get("size")
    return NotAGoogleDocError(
        doc_id,
        drive_file["name"],
        drive_file["mimeType"],
        int(size) if size else None,
    )


def _explain_non_google_doc(doc_id: str, error) -> NotAGoogleDocError | None:
    """
    Say what a Docs API 400 means when the Drive file is not a Google Doc.

    The Docs API answers a PDF with a bare 400 "Request contains an invalid argument" on
    reads. A Word file it reads fine, but refuses every write, comments included, with
    "The document must not be an Office file". Neither error says what the file is, so
    ask Drive. Returns None when the file is a real Google Doc (the 400 is something
    else) or the error is not a 400.
    """
    if error.resp.status != 400:
        return None
    return _non_google_doc(doc_id)


def _get_document(service, doc_id: str, **params) -> dict:
    """
    Fetch a document with inline suggestions so returned indices stay usable
    for later batchUpdate calls. Extra `params` go to documents.get.
    """
    from googleapiclient.errors import HttpError

    try:
        return service.documents().get(
            documentId=doc_id,
            suggestionsViewMode=SUGGESTIONS_VIEW_MODE,
            **params,
        ).execute()
    except HttpError as e:
        not_a_doc = _explain_non_google_doc(doc_id, e)
        if not_a_doc is None:
            raise
        raise not_a_doc from None


def _read_pdf(pdf: NotAGoogleDocError) -> dict:
    """Download a PDF from Drive and return its extracted text in get()'s shape."""
    import io

    from pypdf import PdfReader

    if pdf.size and pdf.size > MAX_PDF_DOWNLOAD_BYTES:
        raise RuntimeError(
            f'"{pdf.name}" is a PDF, not a Google Doc, and is too large to read '
            f"({pdf.size // (1024 * 1024)} MB; limit {MAX_PDF_DOWNLOAD_BYTES // (1024 * 1024)} MB)."
        )
    data = _get_service("drive", "v3").files().get_media(
        fileId=pdf.doc_id,
        supportsAllDrives=True,
    ).execute()
    pages = [(page.extract_text() or "").strip() for page in PdfReader(io.BytesIO(data)).pages]
    plain_text = "\n\n".join(page for page in pages if page)
    warning = (
        "This file is a PDF, not a Google Doc. The text below was extracted from a downloaded "
        "copy and is read-only: the docs_* editing and comment tools cannot change it, and "
        "layout such as tables and columns may not survive extraction."
    )
    if not plain_text:
        warning += " No text could be extracted; the PDF is probably a scanned image."
    return {
        "title": pdf.name,
        "mime_type": PDF_MIME_TYPE,
        "warning": warning,
        "page_count": len(pages),
        "paragraphs": [],
        "plain_text": plain_text,
    }


# ---------------------------------------------------------------------------
# Document structure helpers
# ---------------------------------------------------------------------------

@dataclass
class TextRun:
    text: str
    start: int  # Document index (inclusive)
    end: int    # Document index (exclusive)
    deleted_by: list[str] = field(default_factory=list)  # Suggestions that delete this text

    @property
    def live(self) -> bool:
        """False while a suggestion deletes this text: it is not in the proposed document."""
        return not self.deleted_by


@dataclass
class Paragraph:
    text: str    # Proposed text: suggested deletions dropped, trailing newline stripped
    style: str
    start: int   # Start of paragraph including leading newline-like element
    end: int     # End of paragraph (exclusive)
    runs: list[TextRun]
    cell: Optional[tuple[int, int, int]] = None  # (table, row, column) inside a table cell
    keeps_newline: bool = False  # Docs won't delete its closing newline (see _walk_paragraphs)
    suggestion_ids: list[str] = field(default_factory=list)  # Suggestions that touch it


@dataclass
class InlineStyleSpan:
    start: int
    end: int
    bold: bool = False
    italic: bool = False


@dataclass
class RichParagraph:
    text: str
    style: str = "NORMAL_TEXT"
    bullet_preset: Optional[str] = None
    inline_styles: list[InlineStyleSpan] = field(default_factory=list)


def _walk_paragraphs(
    content: list[dict],
    tables: Iterator[int] | None = None,
    cell: tuple[int, int, int] | None = None,
) -> Iterator[tuple[dict, tuple[int, int, int] | None, bool]]:
    """
    Yield (structural element, table cell, keeps_newline) for each paragraph under
    `content`, in document order, descending into table cells.

    `cell` is (table, row, column) for a paragraph inside a table (the innermost one when
    tables are nested). `keeps_newline` is True where Docs refuses a request that deletes
    the paragraph's closing newline: when it ends the body or a table cell, or comes
    directly before a table, table of contents or section break.
    """
    if tables is None:
        tables = itertools.count()
    for i, elem in enumerate(content):
        if "paragraph" in elem:
            following = content[i + 1] if i + 1 < len(content) else None
            keeps_newline = following is None or any(
                kind in following for kind in ("table", "tableOfContents", "sectionBreak")
            )
            yield elem, cell, keeps_newline
        elif "table" in elem:
            table_number = next(tables)
            for row_number, row in enumerate(elem["table"].get("tableRows", [])):
                for column, table_cell in enumerate(row.get("tableCells", [])):
                    yield from _walk_paragraphs(
                        table_cell.get("content", []),
                        tables,
                        (table_number, row_number, column),
                    )


def _add_ids(ids: list[str], *sources) -> None:
    """Append each suggestion ID in `sources` (lists of IDs, or maps keyed by ID) once."""
    for source in sources:
        for suggestion_id in source or ():
            if suggestion_id not in ids:
                ids.append(suggestion_id)


def _extract_paragraphs(doc: dict) -> list[Paragraph]:
    """
    Extract paragraphs with full structure from a Docs API response, including the
    ones inside tables.

    Paragraph.text is the proposed text: runs that a pending suggestion deletes are
    dropped, so anchors match what the document will read once suggestions are accepted.
    Indices are those of the SUGGESTIONS_INLINE view, which is what batchUpdate expects.
    """
    paragraphs = []
    for elem, cell, keeps_newline in _walk_paragraphs(doc.get("body", {}).get("content", [])):
        para = elem["paragraph"]
        style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        runs = []
        suggestion_ids: list[str] = []
        for pe in para.get("elements", []):
            if "textRun" not in pe:
                # An image, footnote, page break...: each carries its own suggestion IDs.
                for payload in pe.values():
                    if isinstance(payload, dict):
                        _add_ids(
                            suggestion_ids,
                            payload.get("suggestedInsertionIds"),
                            payload.get("suggestedDeletionIds"),
                        )
                continue
            text_run = pe["textRun"]
            runs.append(TextRun(
                text=text_run["content"],
                start=pe["startIndex"],
                end=pe["endIndex"],
                deleted_by=list(text_run.get("suggestedDeletionIds", [])),
            ))
            _add_ids(
                suggestion_ids,
                text_run.get("suggestedInsertionIds"),
                text_run.get("suggestedDeletionIds"),
                text_run.get("suggestedTextStyleChanges"),
            )
        _add_ids(
            suggestion_ids,
            para.get("suggestedParagraphStyleChanges"),
            para.get("suggestedBulletChanges"),
        )
        # Strip trailing newline for display (Google Docs always ends paragraphs with \n)
        display_text = "".join(run.text for run in runs if run.live).rstrip("\n")
        paragraphs.append(Paragraph(
            text=display_text,
            style=style,
            start=elem["startIndex"],
            end=elem["endIndex"],
            runs=runs,
            cell=cell,
            keeps_newline=keeps_newline,
            suggestion_ids=suggestion_ids,
        ))
    return paragraphs


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units, which is what Docs counts in its indices (an emoji is two)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _build_full_text_map(paragraphs: list[Paragraph]) -> tuple[str, list[tuple[int, int, str]]]:
    """
    Returns (full_text, text_map) where:
    - full_text is all characters concatenated from all live text runs (the proposed
      text: runs a suggestion deletes are left out)
    - text_map is list of (offset_in_full_text, doc_start_index, run_text)
      allowing mapping from full_text position → document index
    Positions in full_text count characters; document indices count UTF-16 code units.
    """
    parts = []
    text_map = []
    offset = 0
    for para in paragraphs:
        for run in para.runs:
            if not run.live:
                continue
            text_map.append((offset, run.start, run.text))
            parts.append(run.text)
            offset += len(run.text)
    return "".join(parts), text_map


def _full_text_pos_to_doc_index(pos: int, text_map: list[tuple[int, int, str]]) -> int:
    """Map a position in the concatenated full_text to a document index."""
    for (ft_offset, doc_start, text) in text_map:
        if ft_offset <= pos < ft_offset + len(text):
            return doc_start + _utf16_len(text[: pos - ft_offset])
    raise ValueError(f"Position {pos} is not within any text run in the document.")


def _full_text_range_to_doc_range(
    full_text: str, text_map: list[tuple[int, int, str]], start: int, end: int
) -> tuple[int, int]:
    """Document index range (start, exclusive end) of the characters full_text[start:end]."""
    return (
        _full_text_pos_to_doc_index(start, text_map),
        _full_text_pos_to_doc_index(end - 1, text_map) + _utf16_len(full_text[end - 1]),
    )


def _parse_inline_rich_text(text: str) -> tuple[str, list[InlineStyleSpan]]:
    """
    Parse a very small markdown-like subset into plain text plus style spans.

    Supported forms:
      ***bold italic***
      **bold**
      *italic*

    This intentionally stays conservative and line-local.
    """
    out: list[str] = []
    spans: list[InlineStyleSpan] = []
    i = 0

    def emit_styled(inner: str, *, bold: bool = False, italic: bool = False) -> None:
        start = _utf16_len("".join(out))
        out.append(inner)
        end = start + _utf16_len(inner)
        spans.append(InlineStyleSpan(start=start, end=end, bold=bold, italic=italic))

    while i < len(text):
        if text.startswith("***", i):
            j = text.find("***", i + 3)
            if j != -1 and j > i + 3:
                emit_styled(text[i + 3:j], bold=True, italic=True)
                i = j + 3
                continue
        if text.startswith("**", i):
            j = text.find("**", i + 2)
            if j != -1 and j > i + 2:
                emit_styled(text[i + 2:j], bold=True)
                i = j + 2
                continue
        if text.startswith("*", i):
            j = text.find("*", i + 1)
            if j != -1 and j > i + 1:
                emit_styled(text[i + 1:j], italic=True)
                i = j + 1
                continue
        out.append(text[i])
        i += 1

    return "".join(out), spans


def _parse_rich_text(text: str) -> list[RichParagraph]:
    """
    Parse a small markdown-like subset into paragraph/list/style instructions.

    Supported block forms:
      # Heading 1
      ## Heading 2
      ### Heading 3
      - bullet item
      * bullet item
      1. numbered item
      1) numbered item
    """
    lines = text.split("\n")
    paragraphs: list[RichParagraph] = []

    for raw_line in lines:
        line = raw_line
        style = "NORMAL_TEXT"
        bullet_preset: Optional[str] = None

        if line.startswith("### "):
            style = "HEADING_3"
            line = line[4:]
        elif line.startswith("## "):
            style = "HEADING_2"
            line = line[3:]
        elif line.startswith("# "):
            style = "HEADING_1"
            line = line[2:]
        else:
            bullet_match = re.match(r"^\s*[-*]\s+(.+)$", line)
            numbered_match = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
            if bullet_match:
                bullet_preset = "BULLET_DISC_CIRCLE_SQUARE"
                line = bullet_match.group(1)
            elif numbered_match:
                bullet_preset = "NUMBERED_DECIMAL_NESTED"
                line = numbered_match.group(1)

        plain_text, inline_styles = _parse_inline_rich_text(line)
        paragraphs.append(
            RichParagraph(
                text=plain_text,
                style=style,
                bullet_preset=bullet_preset,
                inline_styles=inline_styles,
            )
        )

    return paragraphs


def _build_insert_requests(
    insert_index: int,
    text: str,
    *,
    prefix: str = "",
    suffix: str = "",
    rich: bool = False,
) -> tuple[list[dict], str]:
    """
    Build Docs batchUpdate requests for plain or rich text insertion.
    """
    if not rich:
        inserted_text = f"{prefix}{text}{suffix}"
        return [
            {
                "insertText": {
                    "location": {"index": insert_index},
                    "text": inserted_text,
                }
            }
        ], inserted_text

    paragraphs = _parse_rich_text(text)
    plain_body = "\n".join(p.text for p in paragraphs)
    inserted_text = f"{prefix}{plain_body}{suffix}"
    requests: list[dict] = [
        {
            "insertText": {
                "location": {"index": insert_index},
                "text": inserted_text,
            }
        }
    ]

    cursor = insert_index + _utf16_len(prefix)
    paragraph_positions: list[tuple[RichParagraph, int, int, int]] = []
    for idx, para in enumerate(paragraphs):
        start = cursor
        end = start + _utf16_len(para.text)
        paragraph_end = end + 1  # Paragraph terminator newline (inserted or existing body newline)
        paragraph_positions.append((para, start, end, paragraph_end))
        cursor = end
        if idx < len(paragraphs) - 1:
            cursor += 1

    for para, start, end, paragraph_end in paragraph_positions:
        if para.style != "NORMAL_TEXT":
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": max(end, start)},
                        "paragraphStyle": {"namedStyleType": para.style},
                        "fields": "namedStyleType",
                    }
                }
            )

        for span in para.inline_styles:
            if span.end <= span.start:
                continue
            text_style = {}
            fields = []
            if span.bold:
                text_style["bold"] = True
                fields.append("bold")
            if span.italic:
                text_style["italic"] = True
                fields.append("italic")
            if text_style:
                requests.append(
                    {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": start + span.start,
                                "endIndex": start + span.end,
                            },
                            "textStyle": text_style,
                            "fields": ",".join(fields),
                        }
                    }
                )

    # Group consecutive list items so numbered lists don't reset on every line.
    i = 0
    while i < len(paragraph_positions):
        para, start, end, paragraph_end = paragraph_positions[i]
        if not para.bullet_preset:
            i += 1
            continue
        bullet_preset = para.bullet_preset
        group_start = start
        group_end = paragraph_end
        j = i + 1
        while j < len(paragraph_positions):
            next_para, next_start, next_end, next_paragraph_end = paragraph_positions[j]
            if next_para.bullet_preset != bullet_preset:
                break
            group_end = next_paragraph_end
            j += 1
        requests.append(
            {
                "createParagraphBullets": {
                    "range": {"startIndex": group_start, "endIndex": group_end},
                    "bulletPreset": bullet_preset,
                }
            }
        )
        i = j

    return requests, inserted_text


# ---------------------------------------------------------------------------
# Writing: direct edits and suggestions
# ---------------------------------------------------------------------------

WRITE_MODE_SUGGEST = "SUGGEST"


class SuggestionNotSavedError(RuntimeError):
    """A suggest-mode write came back without proof it was saved as a suggestion."""


class EmbeddedCommentError(RuntimeError):
    """The comment is saved inside a Word file, which neither the Docs nor the Drive API can change."""


def _batch_update(service, doc_id: str, requests: list[dict], suggest: bool = False) -> dict:
    """
    Send one atomic batchUpdate. With suggest=True the Docs API records every request
    as a suggestion for a person to accept or reject, instead of editing the text.

    A 400 on a file that is not a Google Doc (a .docx opened in Office editing mode is
    refused at write time even when it could be read) becomes NotAGoogleDocError.
    """
    from googleapiclient.errors import HttpError

    body: dict = {"requests": requests}
    if suggest:
        body["writeControl"] = {"writeMode": WRITE_MODE_SUGGEST}
    try:
        return service.documents().batchUpdate(documentId=doc_id, body=body).execute()
    except HttpError as e:
        not_a_doc = _explain_non_google_doc(doc_id, e)
        if not_a_doc is None:
            raise
        raise not_a_doc from None


def _suggestion_ids(result: dict, *keys: str) -> list[str]:
    """Suggestion IDs named under `keys` in a batchUpdate response's suggestionResponses."""
    ids: list[str] = []
    for response in result.get("suggestionResponses", []):
        _add_ids(ids, *(response.get(key) for key in keys))
    return ids


def _suggestion_fields(
    result: dict, suggest: bool, requests: list[dict], expect_changes: bool = True
) -> dict:
    """
    Extra fields for an edit's result: none for a direct edit; for a suggest-mode write,
    the suggestions it created, after checking that Google really saved them.

    A suggest-mode batch can commit its text changes while the suggestion threads fail to
    save, which leaves the edit live in the document with nothing for a person to review.
    So a write is only reported as a suggestion when Google names a suggestion it created
    or changed. `expect_changes` is False when the request may legitimately change nothing
    (a replace-all that matched no text).

    The fields are kept apart because an edit can also fold into an existing suggestion
    (updated_suggestion_ids, which may be someone else's) or, when it only deletes text a
    pending suggestion inserted, shrink or remove one (removed_suggestion_ids).
    """
    if not suggest:
        return {}
    created = _suggestion_ids(result, "createdSuggestionIds")
    updated = [
        suggestion_id
        for suggestion_id in _suggestion_ids(result, "updatedSummarySuggestionIds")
        if suggestion_id not in created
    ]
    removed = _suggestion_ids(result, "deletedSuggestionIds")
    # An edit that inserts text always creates or updates a suggestion; one that only
    # deletes may instead take text out of a pending suggestion, leaving nothing new.
    only_deletes = all("deleteContentRange" in request for request in requests)
    saved = created or updated or (removed if only_deletes else [])
    state = result.get("commentUpdateState", "")
    if state == "ALL_FAILED_UNKNOWN_REASON" or (expect_changes and not saved):
        raise SuggestionNotSavedError(
            "Google did not confirm that this edit was saved as a suggestion "
            f"(commentUpdateState={state or 'missing'}, suggestions created: {len(created)}). "
            "It may have been applied directly to the document: check it with docs_get and "
            "the document's version history before relying on it."
        )
    fields: dict = {"suggested": True, "suggestion_ids": created}
    if updated:
        fields["updated_suggestion_ids"] = updated
    if removed:
        fields["removed_suggestion_ids"] = removed
    return fields


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def get(doc_id: str) -> dict:
    """
    Fetch a Google Doc and return structured representation.

    Paragraphs inside tables are included, in document order, with a "table_cell"
    giving their table, row and column. Where a person or tool has made pending
    suggestions, the text is the document as it will read once they are all accepted
    (suggested deletions are hidden), paragraphs they touch carry a "suggestions" list
    of IDs, and "suggestion_ids" lists every one.

    Returns:
        {
          "title": "Document Title",
          "paragraphs": [
            {"text": "...", "style": "HEADING_1", "start": 0, "end": 45}
          ],
          "plain_text": "full document text..."
        }

    A PDF stored in Drive is returned as extracted, read-only text in the same
    shape, with no paragraphs plus "mime_type", "page_count" and a "warning".
    """
    service = _get_service("docs", "v1")
    try:
        doc = _get_document(service, doc_id)
    except NotAGoogleDocError as e:
        if e.mime_type != PDF_MIME_TYPE:
            raise
        return _read_pdf(e)
    paragraphs = _extract_paragraphs(doc)
    items = []
    suggestion_ids: list[str] = []
    for p in paragraphs:
        item: dict = {"text": p.text, "style": p.style, "start": p.start, "end": p.end}
        if p.cell:
            table, row, column = p.cell
            item["table_cell"] = {"table": table, "row": row, "column": column}
        if p.suggestion_ids:
            item["suggestions"] = p.suggestion_ids
            _add_ids(suggestion_ids, p.suggestion_ids)
        items.append(item)
    result = {
        "title": doc.get("title", ""),
        "paragraphs": items,
        "plain_text": "\n".join(p.text for p in paragraphs),
    }
    if suggestion_ids:
        result["suggestion_ids"] = suggestion_ids
        result["note"] = (
            "This text is the document as it will read once every pending suggestion is "
            "accepted (suggested deletions are hidden). docs_list_suggestions shows what "
            "each suggestion changes."
        )
    return result


def search_replace(
    doc_id: str,
    find: str,
    replace: str,
    occurrence: int = 1,
    regex: bool = False,
    suggest: bool = False,
) -> dict:
    """
    Find text in a document and replace a specific occurrence.

    Args:
        doc_id:     Google Doc ID
        find:       Text to find (or regex pattern if regex=True)
        replace:    Replacement text
        occurrence: Which occurrence to replace (1-based). 0 = replace all.
        regex:      Treat `find` as a regular expression
        suggest:    Record the change as a suggestion instead of editing the text

    Returns:
        {"ok": True, "replaced": "old text", "at_index": 45, "occurrences_found": 3}
        plus "suggested" and "suggestion_ids" (the suggestions created) when suggest=True
    """
    service = _get_service("docs", "v1")

    # Replace-all: use the native replaceAllText API (fast, atomic)
    if occurrence == 0 and not regex:
        requests = [{
            "replaceAllText": {
                "containsText": {"text": find, "matchCase": True},
                "replaceText": replace,
            }
        }]
        result = _batch_update(service, doc_id, requests, suggest)
        count = (
            result.get("replies", [{}])[0]
            .get("replaceAllText", {})
            .get("occurrencesChanged", 0)
        )
        return {
            "ok": True,
            "replaced": find,
            "occurrences_changed": count,
            **_suggestion_fields(result, suggest, requests, expect_changes=count > 0),
        }

    # Targeted occurrence: find index manually
    doc = _get_document(service, doc_id)
    paragraphs = _extract_paragraphs(doc)
    full_text, text_map = _build_full_text_map(paragraphs)

    # Build list of (ft_start, ft_end) tuples
    if regex:
        matches = [(m.start(), m.end()) for m in re.finditer(find, full_text)]
    else:
        matches = []
        search_from = 0
        while True:
            pos = full_text.find(find, search_from)
            if pos == -1:
                break
            matches.append((pos, pos + len(find)))
            search_from = pos + 1

    if not matches:
        raise ValueError(f"Text not found in document: {find!r}")

    target_idx = occurrence - 1
    if target_idx >= len(matches):
        raise ValueError(
            f"Occurrence {occurrence} not found. Document has {len(matches)} occurrence(s) of {find!r}"
        )

    ft_start, ft_end = matches[target_idx]

    doc_start, doc_end = _full_text_range_to_doc_range(full_text, text_map, ft_start, ft_end)

    old_text = full_text[ft_start:ft_end]

    # Apply: delete old text, then insert replacement at same position
    requests = []
    if replace:
        requests.append({
            "insertText": {
                "location": {"index": doc_start},
                "text": replace,
            }
        })
        requests.append({
            "deleteContentRange": {
                "range": {
                    "startIndex": doc_start + _utf16_len(replace),
                    "endIndex": doc_end + _utf16_len(replace),
                }
            }
        })
    else:
        # Replacing with empty string = pure delete
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": doc_start, "endIndex": doc_end}
            }
        })

    result = _batch_update(service, doc_id, requests, suggest)

    return {
        "ok": True,
        "replaced": old_text,
        "at_index": doc_start,
        "occurrences_found": len(matches),
        **_suggestion_fields(result, suggest, requests),
    }


def insert_after(
    doc_id: str, anchor: str, text: str, rich: bool = True, suggest: bool = False
) -> dict:
    """
    Insert text as a new paragraph after the paragraph containing `anchor`.

    The inserted text becomes a separate paragraph (newline appended automatically).
    With suggest=True the insertion is recorded as a suggestion instead of an edit.
    """
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)
    paragraphs = _extract_paragraphs(doc)

    target = None
    for p in paragraphs:
        if anchor.lower() in p.text.lower():
            target = p
            break

    if target is None:
        raise ValueError(f"No paragraph containing anchor: {anchor!r}")

    # Insert after the end of the paragraph (doc end index includes the \n)
    insert_index = target.end - 1  # position of the terminating \n

    requests, inserted_text = _build_insert_requests(
        insert_index,
        text,
        prefix="\n",
        rich=rich,
    )
    result = _batch_update(service, doc_id, requests, suggest)

    return {
        "ok": True,
        "inserted_after": target.text[:80],
        "at_index": insert_index,
        "rich": rich,
        "inserted_text": inserted_text[:200],
        **_suggestion_fields(result, suggest, requests),
    }


def insert_before(
    doc_id: str, anchor: str, text: str, rich: bool = True, suggest: bool = False
) -> dict:
    """
    Insert text as a new paragraph before the paragraph containing `anchor`.
    With suggest=True the insertion is recorded as a suggestion instead of an edit.
    """
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)
    paragraphs = _extract_paragraphs(doc)

    target = None
    for p in paragraphs:
        if anchor.lower() in p.text.lower():
            target = p
            break

    if target is None:
        raise ValueError(f"No paragraph containing anchor: {anchor!r}")

    # Insert at the start of the paragraph
    insert_index = target.start

    requests, inserted_text = _build_insert_requests(
        insert_index,
        text,
        suffix="\n",
        rich=rich,
    )
    result = _batch_update(service, doc_id, requests, suggest)

    return {
        "ok": True,
        "inserted_before": target.text[:80],
        "at_index": insert_index,
        "rich": rich,
        "inserted_text": inserted_text[:200],
        **_suggestion_fields(result, suggest, requests),
    }


def delete_paragraph(doc_id: str, anchor: str, suggest: bool = False) -> dict:
    """
    Delete the paragraph(s) containing `anchor` text.

    Deletes ALL paragraphs matching the anchor (case-insensitive substring), including
    those in tables. Returns count of deleted paragraphs. With suggest=True the deletion
    is recorded as a suggestion instead of an edit.

    Docs won't delete the closing newline of the last paragraph of the body or of a table
    cell, or of one directly before a table or section break. For those only the text is
    removed and an empty paragraph remains.
    """
    if not anchor.strip():
        raise ValueError("anchor must not be empty: it would match every paragraph.")
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)
    paragraphs = _extract_paragraphs(doc)

    targets = [p for p in paragraphs if anchor.lower() in p.text.lower()]
    if not targets:
        raise ValueError(f"No paragraph containing anchor: {anchor!r}")

    # Sort by start index descending so deleting earlier content doesn't shift later indices
    targets.sort(key=lambda p: p.start, reverse=True)

    requests = []
    for t in targets:
        requests.append({
            "deleteContentRange": {
                "range": {
                    "startIndex": t.start,
                    "endIndex": t.end - 1 if t.keeps_newline else t.end,
                }
            }
        })

    result = _batch_update(service, doc_id, requests, suggest)

    return {
        "ok": True,
        "deleted_count": len(targets),
        "deleted": [t.text[:80] for t in targets],
        **_suggestion_fields(result, suggest, requests),
    }


def append(doc_id: str, text: str, rich: bool = True, suggest: bool = False) -> dict:
    """
    Append text as a new paragraph at the end of the document.
    With suggest=True the insertion is recorded as a suggestion instead of an edit.
    """
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)

    # Find the last content index (end of the body, before the body's closing)
    body_content = doc.get("body", {}).get("content", [])
    if not body_content:
        raise ValueError("Document body is empty.")

    # The last element's endIndex is the body end (exclusive).
    # We insert just before that.
    last_elem = body_content[-1]
    insert_index = last_elem["endIndex"] - 1

    requests, inserted_text = _build_insert_requests(
        insert_index,
        text,
        prefix="\n",
        rich=rich,
    )
    result = _batch_update(service, doc_id, requests, suggest)

    return {
        "ok": True,
        "appended": text[:80],
        "at_index": insert_index,
        "rich": rich,
        "inserted_text": inserted_text[:200],
        **_suggestion_fields(result, suggest, requests),
    }


def batch_replace(doc_id: str, replacements: list[dict], suggest: bool = False) -> dict:
    """
    Apply multiple find→replace operations atomically (all or nothing).

    Replacements are sorted by position (end→start) automatically, so earlier
    indices remain valid throughout the batch.

    Args:
        doc_id:       Google Doc ID
        replacements: List of {"find": "...", "replace": "...", "occurrence": 1}
                      `occurrence` defaults to 1. Use 0 for replace-all.
        suggest:      Record the changes as suggestions instead of editing the text

    Returns:
        {"ok": True, "applied": N, "changes": [...]}
        plus "suggested" and "suggestion_ids" (the suggestions created) when suggest=True
    """
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)
    paragraphs = _extract_paragraphs(doc)
    full_text, text_map = _build_full_text_map(paragraphs)

    # Collect all changes with their document indices
    changes = []  # (doc_start, doc_end, replace_text, find_text)
    for rep in replacements:
        find = rep["find"]
        replace_text = rep.get("replace", "")
        occurrence = rep.get("occurrence", 1)
        is_regex = rep.get("regex", False)

        # Build list of (ft_start, ft_end) tuples
        if is_regex:
            matches = [(m.start(), m.end()) for m in re.finditer(find, full_text)]
        else:
            matches = []
            search_from = 0
            while True:
                pos = full_text.find(find, search_from)
                if pos == -1:
                    break
                matches.append((pos, pos + len(find)))
                search_from = pos + 1

        if not matches:
            raise ValueError(f"Text not found: {find!r}")

        if occurrence == 0:
            # Replace all
            for ft_start, ft_end in matches:
                ds, de = _full_text_range_to_doc_range(full_text, text_map, ft_start, ft_end)
                changes.append((ds, de, replace_text, full_text[ft_start:ft_end]))
        else:
            idx = occurrence - 1
            if idx >= len(matches):
                raise ValueError(f"Occurrence {occurrence} not found for {find!r}")
            ft_start, ft_end = matches[idx]
            ds, de = _full_text_range_to_doc_range(full_text, text_map, ft_start, ft_end)
            changes.append((ds, de, replace_text, full_text[ft_start:ft_end]))

    # Sort by doc_start DESCENDING (apply end-of-doc first so indices stay valid)
    changes.sort(key=lambda c: c[0], reverse=True)

    # Build batchUpdate requests
    requests = []
    for (ds, de, replace_text, old_text) in changes:
        if replace_text:
            requests.append({
                "insertText": {
                    "location": {"index": ds},
                    "text": replace_text,
                }
            })
            requests.append({
                "deleteContentRange": {
                    "range": {
                        "startIndex": ds + _utf16_len(replace_text),
                        "endIndex": de + _utf16_len(replace_text),
                    }
                }
            })
        else:
            requests.append({
                "deleteContentRange": {
                    "range": {"startIndex": ds, "endIndex": de}
                }
            })

    result = _batch_update(service, doc_id, requests, suggest)

    return {
        "ok": True,
        "applied": len(changes),
        "changes": [
            {"replaced": old, "with": new, "at_index": ds}
            for (ds, de, new, old) in sorted(changes, key=lambda c: c[0])
        ],
        **_suggestion_fields(result, suggest, requests),
    }


def _docs_or_drive(
    doc_id: str,
    via_docs: Callable[[], dict],
    via_drive: Callable[[NotAGoogleDocError], dict],
) -> dict:
    """
    Run a comment operation through the Docs API, or through Drive when the file is not
    a Google Doc.

    On a real Google Doc the Docs API is the right route: it is the only one Docs shows
    as anchored to text (a comment made through Drive reads "Original content deleted").
    A Word file opened in Drive's Office editing mode, a PDF and the like are refused by
    the Docs API, but take Drive comments, which are the only kind they have.
    """
    from googleapiclient.errors import HttpError

    try:
        return via_docs()
    except NotAGoogleDocError as e:
        not_a_doc = e
    except HttpError as e:
        not_a_doc = _explain_non_google_doc(doc_id, e)
        if not_a_doc is None:
            raise
    return via_drive(not_a_doc)


def add_comment(
    doc_id: str,
    comment: str,
    anchor_text: str,
    occurrence: int = 1,
    assignee_email: str | None = None,
) -> dict:
    """
    Add a comment anchored to specific text in a Google Doc.

    The anchor is found by text, never by index: the server maps the matched
    text to its document range and sends a Docs API insertComment request, so
    the comment shows as a normal inline highlight in the Docs UI. Text inside
    tables can be anchored too.

    For a file that is not a Google Doc (a .docx opened in Office editing mode, a
    PDF) the comment is posted on the file through Drive instead. Drive cannot
    anchor it, so it starts with a quote of `anchor_text` and the result has
    "anchored": False.

    Args:
        doc_id:         Google Doc ID
        comment:        Comment text to post (max 2048 UTF-8 bytes)
        anchor_text:    Text in the document to attach the comment to
        occurrence:     Which occurrence to anchor to (default 1 = first)
        assignee_email: Optional user to assign the comment to as an action item

    Returns:
        {"ok": True, "comment_id": "...", "anchored_to": "...", "at_index": N}
    """
    return _docs_or_drive(
        doc_id,
        lambda: _add_docs_comment(doc_id, comment, anchor_text, occurrence, assignee_email),
        lambda file: _add_drive_comment(
            doc_id, file, comment, anchor_text, occurrence, assignee_email
        ),
    )


def _add_docs_comment(
    doc_id: str,
    comment: str,
    anchor_text: str,
    occurrence: int,
    assignee_email: str | None,
) -> dict:
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)
    paragraphs = _extract_paragraphs(doc)
    full_text, text_map = _build_full_text_map(paragraphs)

    # Find all occurrences of anchor_text
    matches = []
    search_from = 0
    while True:
        pos = full_text.find(anchor_text, search_from)
        if pos == -1:
            break
        matches.append((pos, pos + len(anchor_text)))
        search_from = pos + 1

    if not matches:
        raise ValueError(f"Anchor text not found in document: {anchor_text!r}")

    target_idx = occurrence - 1
    if target_idx >= len(matches):
        raise ValueError(
            f"Occurrence {occurrence} not found — document has {len(matches)} "
            f"occurrence(s) of {anchor_text!r}"
        )

    ft_start, ft_end = matches[target_idx]
    doc_start, doc_end = _full_text_range_to_doc_range(full_text, text_map, ft_start, ft_end)

    insert = {
        "range": {"startIndex": doc_start, "endIndex": doc_end},
        "content": comment,
    }
    if assignee_email:
        insert["assigneeEmailAddress"] = assignee_email
    result = service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertComment": insert}]},
    ).execute()
    thread = result["replies"][0]["insertComment"]["commentThread"]

    return {
        "ok": True,
        "comment_id": thread["commentId"],
        "anchored_to": full_text[ft_start:ft_end],
        "at_index": doc_start,
    }


def _post_summary(post: dict) -> dict:
    return {
        "id": post.get("postId"),
        "author": post.get("author", {}).get("displayName", "?"),
        "content": post.get("content", ""),
        "created": post.get("createTime", ""),
    }


def read_comments(doc_id: str, include_resolved: bool = False) -> dict:
    """
    List comment threads on a Google Doc with their quoted text and replies.

    For a file that is not a Google Doc (a .docx, a PDF) the file's Drive comments are
    listed instead, with "source": "drive", plus the comments saved inside a Word file,
    marked "origin": "embedded" (readable, but they cannot be replied to, resolved or
    deleted).

    Returns:
        {"count": N, "comments": [{"id", "content", "author", "quoted_text",
          "assignee", "resolved", "created", "replies": [...]}]}
    """
    # Only Drive can say whether this is a real Google Doc, and the answer decides where the
    # comments live: the Docs API reads a Word file without complaint, yet shows just the
    # comments saved inside it and none of the Drive comments added since.
    not_a_doc = _non_google_doc(doc_id)
    if not_a_doc is None:
        return _read_docs_comments(doc_id, include_resolved)
    return _read_other_file_comments(doc_id, not_a_doc, include_resolved)


def _read_docs_comments(doc_id: str, include_resolved: bool) -> dict:
    # The Docs API only returns comments when tabs content is requested and
    # suggestions are shown inline.
    doc = _get_document(
        _get_service("docs", "v1"),
        doc_id,
        commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED",
        includeTabsContent=True,
        fields="comments",
    )

    comments = []
    for thread in doc.get("comments", []):
        resolved = thread.get("status") == "RESOLVED"
        if resolved and not include_resolved:
            continue
        head = thread.get("headPost", {})
        comments.append({
            "id": thread["commentId"],
            "content": head.get("content", ""),
            "author": head.get("author", {}).get("displayName", "?"),
            "quoted_text": thread.get("plainTextQuote", ""),
            "assignee": head.get("assigneeEmail"),
            "resolved": resolved,
            "created": head.get("createTime", ""),
            "replies": [
                _post_summary(post) for post in thread.get("replies", []) if not post.get("deleted")
            ],
        })
    return {"count": len(comments), "comments": comments}


def _read_other_file_comments(
    doc_id: str, file: NotAGoogleDocError, include_resolved: bool
) -> dict:
    """
    Comments on a Word file, PDF and the like: its Drive comments, plus the ones saved
    inside a Word file, which the Docs API shows when it converts the file on the fly (and
    Drive does not list). Those cannot be replied to, resolved or deleted.
    """
    result = _read_drive_comments(doc_id, file, include_resolved)
    for comment in result["comments"]:
        comment["origin"] = "drive"
    embedded: list[dict] = []
    if file.mime_type != PDF_MIME_TYPE:
        try:
            embedded = _read_docs_comments(doc_id, include_resolved)["comments"]
        except NotAGoogleDocError:
            pass  # the Docs API cannot read this kind of file at all
    for comment in embedded:
        comment["origin"] = "embedded"
    result["comments"] = embedded + result["comments"]
    result["count"] = len(result["comments"])
    if embedded:
        result["note"] += (
            ' Comments with origin "embedded" are saved inside the file: they can be read but '
            "not replied to, resolved or deleted here. Convert the file to a Google Doc "
            "(docs_convert_to_google_doc) to work with them."
        )
    return result


def _add_comment_post(doc_id: str, comment_id: str, post: dict) -> dict:
    result = _get_service("docs", "v1").documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"addCommentReply": {"commentId": comment_id, "post": post}}]},
    ).execute()
    return result["replies"][0]["addCommentReply"]["post"]


def reply_to_comment(doc_id: str, comment_id: str, reply: str) -> dict:
    """Post a reply to a comment thread (a Drive reply when the file is not a Google Doc)."""

    def via_docs() -> dict:
        post = _add_comment_post(doc_id, comment_id, {"content": reply})
        return {
            "ok": True,
            "reply_id": post.get("postId"),
            "comment_id": comment_id,
            "content": post.get("content", reply),
        }

    def via_drive(file: NotAGoogleDocError) -> dict:
        from googleapiclient.errors import HttpError

        try:
            posted = _get_service("drive", "v3").replies().create(
                fileId=doc_id,
                commentId=comment_id,
                body={"content": reply},
                fields="id,content",
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                raise _embedded_comment_error(file, comment_id) from None
            raise
        return {
            "ok": True,
            "reply_id": posted.get("id"),
            "comment_id": comment_id,
            "content": posted.get("content", reply),
            "source": "drive",
        }

    return _docs_or_drive(doc_id, via_docs, via_drive)


def resolve_comment(doc_id: str, comment_id: str, reply: str = "") -> dict:
    """
    Resolve a comment thread, optionally with a final reply in the same post
    (a resolving Drive reply when the file is not a Google Doc).
    """

    def via_docs() -> dict:
        post: dict = {"commentAction": "RESOLVE"}
        if reply:
            post["content"] = reply
        _add_comment_post(doc_id, comment_id, post)
        return {"ok": True, "comment_id": comment_id, "resolved": True}

    def via_drive(file: NotAGoogleDocError) -> dict:
        from googleapiclient.errors import HttpError

        body = {"action": "resolve"}
        if reply:
            body["content"] = reply
        try:
            _get_service("drive", "v3").replies().create(
                fileId=doc_id,
                commentId=comment_id,
                body=body,
                fields="id,action",
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                raise _embedded_comment_error(file, comment_id) from None
            raise
        return {"ok": True, "comment_id": comment_id, "resolved": True, "source": "drive"}

    return _docs_or_drive(doc_id, via_docs, via_drive)


def delete_comment(doc_id: str, comment_id: str) -> dict:
    """
    Delete a comment thread. Only the thread's author can delete it
    (also true of Drive comments, which are used when the file is not a Google Doc).
    """

    def via_docs() -> dict:
        _get_service("docs", "v1").documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"deleteComment": {"commentId": comment_id}}]},
        ).execute()
        return {"ok": True, "comment_id": comment_id}

    def via_drive(file: NotAGoogleDocError) -> dict:
        from googleapiclient.errors import HttpError

        try:
            _get_service("drive", "v3").comments().delete(
                fileId=doc_id, commentId=comment_id
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                raise _embedded_comment_error(file, comment_id) from None
            raise
        return {"ok": True, "comment_id": comment_id, "source": "drive"}

    return _docs_or_drive(doc_id, via_docs, via_drive)


# --- Drive comments: the route for files the Docs API refuses ---------------

def _embedded_comment_error(file: NotAGoogleDocError, comment_id: str) -> EmbeddedCommentError:
    return EmbeddedCommentError(
        f'Comment {comment_id} is not a Drive comment on "{file.name}". If it is one saved inside '
        'the Word file (docs_read_comments marks those "origin": "embedded"), it can be read but '
        "not replied to, resolved or deleted here. Convert the file to a Google Doc with "
        "docs_convert_to_google_doc to work with it."
    )


def _drive_note(file: NotAGoogleDocError) -> str:
    return (
        f'"{file.name}" is {file.kind}, not a Google Doc, so the Docs API cannot comment '
        "on it; its comments are Drive comments on the file."
    )


def _add_drive_comment(
    doc_id: str,
    file: NotAGoogleDocError,
    comment: str,
    anchor_text: str,
    occurrence: int,
    assignee_email: str | None,
) -> dict:
    """
    Post a Drive comment on a file the Docs API cannot open.

    Drive cannot anchor a comment in such a file and the text cannot be searched, so
    the quote of `anchor_text` goes at the start of the comment to say what it is about.
    """
    quote = " ".join(anchor_text.split())
    if len(quote) > 200:
        quote = quote[:200] + "..."
    heading = f'Re: "{quote}"' + (f" (occurrence {occurrence})" if occurrence > 1 else "")
    # Docs reads "+address" in a comment as "assign to" where it can; otherwise it is just text.
    body = f"+{assignee_email} {comment}" if assignee_email else comment
    created = _get_service("drive", "v3").comments().create(
        fileId=doc_id,
        body={"content": f"{heading}\n\n{body}"},
        fields="id,content",
    ).execute()
    result = {
        "ok": True,
        "comment_id": created["id"],
        "anchored": False,
        "anchored_to": None,
        "at_index": None,
        "source": "drive",
        "note": _drive_note(file) + " The comment starts with a quote of the text it is about.",
    }
    if assignee_email:
        result["assignee_note"] = (
            "Drive comments cannot be assigned through the API, so the address is "
            "written at the start of the comment text."
        )
    return result


DRIVE_COMMENT_FIELDS = (
    "nextPageToken,comments(id,content,createdTime,resolved,deleted,"
    "author(displayName),quotedFileContent(value),"
    "replies(id,content,createdTime,action,deleted,author(displayName)))"
)


def _read_drive_comments(
    doc_id: str, file: NotAGoogleDocError, include_resolved: bool
) -> dict:
    """List the Drive comments on a file the Docs API cannot open, in read_comments' shape."""
    drive = _get_service("drive", "v3")
    comments = []
    page_token = None
    while True:
        page = drive.comments().list(
            fileId=doc_id,
            fields=DRIVE_COMMENT_FIELDS,
            includeDeleted=False,
            pageSize=100,
            pageToken=page_token,
        ).execute()
        for thread in page.get("comments", []):
            resolved = bool(thread.get("resolved"))
            if thread.get("deleted") or (resolved and not include_resolved):
                continue
            replies = []
            for reply in thread.get("replies", []):
                if reply.get("deleted"):
                    continue
                item = {
                    "id": reply.get("id"),
                    "author": reply.get("author", {}).get("displayName", "?"),
                    "content": reply.get("content", ""),
                    "created": reply.get("createdTime", ""),
                }
                if reply.get("action"):
                    item["action"] = reply["action"]
                replies.append(item)
            comments.append({
                "id": thread["id"],
                "content": thread.get("content", ""),
                "author": thread.get("author", {}).get("displayName", "?"),
                "quoted_text": (thread.get("quotedFileContent") or {}).get("value", ""),
                "assignee": None,
                "resolved": resolved,
                "created": thread.get("createdTime", ""),
                "replies": replies,
            })
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    return {
        "count": len(comments),
        "comments": comments,
        "source": "drive",
        "note": _drive_note(file),
    }


# ---------------------------------------------------------------------------
# Suggestions: see what is pending, then accept, reject or delete it
# ---------------------------------------------------------------------------

def _tab_contents(doc: dict) -> list[list[dict]]:
    """
    Every list of structural elements in the document: the body, headers, footers and
    footnotes of each tab (and child tab), or of the document itself when it has no tabs.
    """
    contents: list[list[dict]] = []

    def collect(container: dict) -> None:
        contents.append(container.get("body", {}).get("content", []))
        for part in ("headers", "footers", "footnotes"):
            for section in container.get(part, {}).values():
                contents.append(section.get("content", []))

    def visit(tab: dict) -> None:
        collect(tab.get("documentTab", {}))
        for child in tab.get("childTabs", []):
            visit(child)

    if doc.get("tabs"):
        for tab in doc["tabs"]:
            visit(tab)
    else:
        collect(doc)
    return contents


def _changed_fields(state: dict | None) -> list[str]:
    """Names of the fields a suggestion-state mask marks as changed ("boldSuggested" -> "bold")."""
    return [
        name.removesuffix("Suggested")
        for name, changed in (state or {}).items()
        if changed is True
    ]


def _suggested_changes(doc: dict) -> dict[str, list[dict]]:
    """
    What each suggestion changes, read from the markers on the inline document.

    Returns suggestion ID -> changes in document order. A change is {"type", "text",
    "context"}: type is insertion, deletion or text_style (with "fields"), plus
    paragraph_style (with "fields") and bullet, which have no text. "context" is the
    paragraph the change starts in. Adjacent runs of one suggestion are joined.

    Google also marks text a suggestion inserts or deletes with that suggestion's own
    style (every field flagged), and a paragraph it inserts with its paragraph style and
    bullet, as though they were further changes. They are part of the insertion or
    deletion, so they are not listed again.
    """
    found: dict[str, list[dict]] = {}
    for content in _tab_contents(doc):
        open_changes: dict[tuple[str, str], dict] = {}
        for elem, _cell, _keeps_newline in _walk_paragraphs(content):
            para = elem["paragraph"]
            runs = [pe["textRun"] for pe in para.get("elements", []) if "textRun" in pe]
            context = "".join(
                run["content"] for run in runs if not run.get("suggestedDeletionIds")
            ).strip()
            inserted_here = {sid for run in runs for sid in run.get("suggestedInsertionIds", [])}
            for suggestion_id, change in (para.get("suggestedParagraphStyleChanges") or {}).items():
                if suggestion_id not in inserted_here:
                    found.setdefault(suggestion_id, []).append({
                        "type": "paragraph_style",
                        "fields": _changed_fields(change.get("paragraphStyleSuggestionState")),
                        "context": context,
                    })
            for suggestion_id in para.get("suggestedBulletChanges") or {}:
                if suggestion_id not in inserted_here:
                    found.setdefault(suggestion_id, []).append(
                        {"type": "bullet", "context": context}
                    )
            for run in runs:
                style_changes = run.get("suggestedTextStyleChanges") or {}
                inserted = run.get("suggestedInsertionIds", [])
                deleted = run.get("suggestedDeletionIds", [])
                keys = (
                    [("insertion", sid) for sid in inserted]
                    + [("deletion", sid) for sid in deleted]
                    + [
                        ("text_style", sid)
                        for sid in style_changes
                        if sid not in inserted and sid not in deleted
                    ]
                )
                still_open: dict[tuple[str, str], dict] = {}
                for kind, suggestion_id in keys:
                    entry = open_changes.get((kind, suggestion_id))
                    if entry is None:
                        entry = {"type": kind, "text": "", "context": context}
                        if kind == "text_style":
                            entry["fields"] = []
                        found.setdefault(suggestion_id, []).append(entry)
                    entry["text"] += run["content"]
                    if kind == "text_style":
                        state = style_changes[suggestion_id].get("textStyleSuggestionState")
                        for name in _changed_fields(state):
                            if name not in entry["fields"]:
                                entry["fields"].append(name)
                    still_open[(kind, suggestion_id)] = entry
                open_changes = still_open

    for entries in found.values():
        # A style entry that changes no field says nothing.
        entries[:] = [e for e in entries if e["type"] != "text_style" or e["fields"]]
        for entry in entries:
            if "text" in entry:
                raw = entry["text"]
                entry["text"] = raw.strip("\n") or ("(paragraph break)" if raw else "")
            entry["context"] = entry["context"][:120]
            if not entry["context"] or entry["context"] == entry.get("text"):
                del entry["context"]
    return found


def list_suggestions(doc_id: str, include_closed: bool = False) -> dict:
    """
    List the suggestions in a Google Doc: who made each, Google's summary of it, and
    exactly which text it inserts, deletes or restyles.

    Includes suggestions made by people in the Docs UI as well as ones made with
    suggest=True. Accepted and rejected suggestions are left out unless include_closed.

    Returns:
        {"count": N, "suggestions": [{"id", "status", "author", "created", "summary",
          "changes": [{"type", "text", "context"}], "replies": [...]}]}
    """
    doc = _get_document(
        _get_service("docs", "v1"),
        doc_id,
        commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED",
        includeTabsContent=True,
    )
    changes = _suggested_changes(doc)
    threads = {thread["suggestionId"]: thread for thread in doc.get("suggestions", [])}

    suggestions = []
    # In document order (Google lists the threads in no useful order). A suggestion marked
    # in the text but missing from the thread list is still listed, and a thread with no
    # marks in the body (a header or document-style change) comes last.
    for suggestion_id in dict.fromkeys([*changes, *threads]):
        thread = threads.get(suggestion_id, {})
        status = thread.get("status", "OPEN")
        if status in ("ACCEPTED", "REJECTED") and not include_closed:
            continue
        head = thread.get("headPost", {})
        suggestions.append({
            "id": suggestion_id,
            "status": status,
            "author": head.get("author", {}).get("displayName"),
            "created": head.get("createTime", ""),
            "summary": thread.get("summaryText", ""),
            "changes": changes.get(suggestion_id, []),
            "replies": [
                _post_summary(post)
                for post in thread.get("replies", [])
                if not post.get("deleted") and post.get("content")
            ],
        })
    return {"count": len(suggestions), "suggestions": suggestions}


SUGGESTION_ACTIONS = {
    "accept": ("acceptSuggestion", "acceptedSuggestionIds"),
    "reject": ("rejectSuggestion", "rejectedSuggestionIds"),
    "delete": ("deleteSuggestion", "deletedSuggestionIds"),
}


def review_suggestions(doc_id: str, action: str, suggestion_ids: list[str]) -> dict:
    """
    Accept, reject or delete suggestions, all in one atomic request.

    accept applies the change to the document (needs edit access); reject throws it
    away (edit access, or being its author); delete removes the suggestion from the
    document altogether (its author only).

    Args:
        doc_id:         Google Doc ID
        action:         "accept", "reject" or "delete"
        suggestion_ids: IDs from list_suggestions

    Returns:
        {"ok": True, "action": "accept", "applied": ["suggest.abc", ...]}
        plus "unconfirmed" for any ID Google did not list as processed
    """
    if action not in SUGGESTION_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(SUGGESTION_ACTIONS)}, not {action!r}")
    ids = list(dict.fromkeys(suggestion_ids))
    if not ids:
        raise ValueError("No suggestion IDs given: pass the IDs from docs_list_suggestions.")
    request_name, response_key = SUGGESTION_ACTIONS[action]

    result = _batch_update(
        _get_service("docs", "v1"),
        doc_id,
        [{request_name: {"suggestionId": suggestion_id}} for suggestion_id in ids],
    )

    applied = _suggestion_ids(result, response_key)
    out: dict = {"ok": True, "action": action, "applied": applied}
    unconfirmed = [suggestion_id for suggestion_id in ids if suggestion_id not in applied]
    if unconfirmed:
        out["unconfirmed"] = unconfirmed
        out["note"] = (
            "Google accepted the request but did not list these suggestions as processed; "
            "check docs_list_suggestions."
        )
    return out


# ---------------------------------------------------------------------------
# Converting other files to Google Docs
# ---------------------------------------------------------------------------

# Drive can make a Google Doc from these. Anything else (a Sheet, an image, a folder) cannot be one.
CONVERTIBLE_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "application/msword",
    "application/rtf",
    "text/rtf",
    "application/vnd.oasis.opendocument.text",
    "text/plain",
    "text/html",
    PDF_MIME_TYPE,
}
# Dropped from the copy's default name, as Drive's own "Save as Google Docs" does.
CONVERTED_EXTENSIONS = (".docx", ".dotx", ".doc", ".rtf", ".odt", ".txt", ".html", ".htm", ".pdf")


def convert_to_google_doc(file_id: str, name: str = "") -> dict:
    """
    Make a Google Doc from a Word, RTF, OpenDocument, text, HTML or PDF file in Drive.

    The Docs API cannot edit those, but a converted copy supports every tool here. For a
    Word file the conversion keeps tables, turns tracked changes into suggestions (author
    and date kept) and comments into ordinary comments. The original is left alone and
    each call makes a new copy beside it. A file that is already a Google Doc is returned
    as it is.

    Args:
        file_id: Drive file ID of the file to convert
        name:    Title for the copy (default: the original's name without its extension)

    Returns:
        {"ok": True, "converted": True, "id": "<new doc id>", "name": "...",
         "webViewLink": "...", "converted_from": {"id", "name", "type"}, "note": "..."}
    """
    drive = _get_service("drive", "v3")
    source = drive.files().get(
        fileId=file_id, fields="id,name,mimeType", supportsAllDrives=True
    ).execute()
    mime_type = source["mimeType"]
    kind = FILE_TYPE_NAMES.get(mime_type, f"a file of type {mime_type}")

    if mime_type == GOOGLE_DOC_MIME_TYPE:
        return {
            "ok": True,
            "converted": False,
            "id": file_id,
            "name": source["name"],
            "webViewLink": f"https://docs.google.com/document/d/{file_id}/edit",
            "note": "Already a Google Doc: nothing to convert.",
        }
    if mime_type not in CONVERTIBLE_MIME_TYPES:
        raise ValueError(
            f'"{source["name"]}" is {kind}, which cannot be converted to a Google Doc. '
            "Word, RTF, OpenDocument, plain-text, HTML and PDF files can be."
        )

    title = name
    if not title:
        title = source["name"]
        for extension in CONVERTED_EXTENSIONS:
            if title.lower().endswith(extension) and len(title) > len(extension):
                title = title[: -len(extension)]
                break
    copy = drive.files().copy(
        fileId=file_id,
        body={"name": title, "mimeType": GOOGLE_DOC_MIME_TYPE},
        supportsAllDrives=True,
        fields="id,name,webViewLink",
    ).execute()

    if mime_type == PDF_MIME_TYPE:
        note = (
            "A PDF is converted by text recognition, so expect lost layout and some errors: "
            "check the result with docs_get before relying on it."
        )
    else:
        note = (
            "Tracked changes in the original are now suggestions (docs_list_suggestions) and its "
            "comments are ordinary comments (docs_read_comments). The original is unchanged."
        )
    return {
        "ok": True,
        "converted": True,
        "id": copy["id"],
        "name": copy["name"],
        "webViewLink": copy.get("webViewLink") or f"https://docs.google.com/document/d/{copy['id']}/edit",
        "converted_from": {"id": file_id, "name": source["name"], "type": kind},
        "note": note,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Surgical Google Docs editor — LLMs never touch indices.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", help="Output JSON (default for scripting)")

    sub = p.add_subparsers(dest="command", required=True)

    # get
    g = sub.add_parser("get", help="Get document structure")
    g.add_argument("doc_id")

    suggest_help = "Record the change as a suggestion instead of editing the text"

    # search_replace
    sr = sub.add_parser("search_replace", help="Search and replace text")
    sr.add_argument("doc_id")
    sr.add_argument("--find", required=True)
    sr.add_argument("--replace", required=True)
    sr.add_argument("--occurrence", type=int, default=1, help="Which occurrence (1=first, 0=all)")
    sr.add_argument("--regex", action="store_true")
    sr.add_argument("--suggest", action="store_true", help=suggest_help)

    # insert_after
    ia = sub.add_parser("insert_after", help="Insert paragraph after anchor")
    ia.add_argument("doc_id")
    ia.add_argument("--anchor", required=True)
    ia.add_argument("--text", required=True)
    ia.set_defaults(rich=True)
    ia.add_argument("--plain", dest="rich", action="store_false", help="Insert literal text without rich formatting")
    ia.add_argument("--suggest", action="store_true", help=suggest_help)

    # insert_before
    ib = sub.add_parser("insert_before", help="Insert paragraph before anchor")
    ib.add_argument("doc_id")
    ib.add_argument("--anchor", required=True)
    ib.add_argument("--text", required=True)
    ib.set_defaults(rich=True)
    ib.add_argument("--plain", dest="rich", action="store_false", help="Insert literal text without rich formatting")
    ib.add_argument("--suggest", action="store_true", help=suggest_help)

    # delete_paragraph
    dp = sub.add_parser("delete_paragraph", help="Delete paragraph(s) matching anchor")
    dp.add_argument("doc_id")
    dp.add_argument("--anchor", required=True)
    dp.add_argument("--suggest", action="store_true", help=suggest_help)

    # append
    ap = sub.add_parser("append", help="Append text to end of document")
    ap.add_argument("doc_id")
    ap.add_argument("--text", required=True)
    ap.set_defaults(rich=True)
    ap.add_argument("--plain", dest="rich", action="store_false", help="Insert literal text without rich formatting")
    ap.add_argument("--suggest", action="store_true", help=suggest_help)

    # batch_replace
    br = sub.add_parser("batch_replace", help="Multiple replacements (atomic)")
    br.add_argument("doc_id")
    br.add_argument(
        "--replacements",
        required=True,
        help='JSON array: [{"find":"...","replace":"...","occurrence":1}]',
    )
    br.add_argument("--suggest", action="store_true", help=suggest_help)

    # list_suggestions
    ls = sub.add_parser("list_suggestions", help="List suggestions and what they change")
    ls.add_argument("doc_id")
    ls.add_argument("--include-closed", action="store_true", help="Include accepted and rejected ones")

    # review_suggestions
    rs = sub.add_parser("review_suggestions", help="Accept, reject or delete suggestions")
    rs.add_argument("doc_id")
    rs.add_argument("--action", required=True, choices=sorted(SUGGESTION_ACTIONS))
    rs.add_argument("--ids", required=True, nargs="+", help="Suggestion IDs from list_suggestions")

    # convert
    cv = sub.add_parser("convert", help="Make a Google Doc from a Word, RTF, text, HTML or PDF file")
    cv.add_argument("file_id")
    cv.add_argument("--name", default="", help="Title for the copy (default: the original's name)")

    # add_comment
    ac = sub.add_parser("add_comment", help="Add a comment anchored to specific text")
    ac.add_argument("doc_id")
    ac.add_argument("--anchor", required=True, help="Text in the document to anchor the comment to")
    ac.add_argument("--comment", required=True, help="Comment text to post")
    ac.add_argument("--occurrence", type=int, default=1, help="Which occurrence to anchor to (default 1)")
    ac.add_argument("--assignee", help="Email of the user to assign the comment to")

    # read_comments
    rc = sub.add_parser("read_comments", help="List comment threads")
    rc.add_argument("doc_id")
    rc.add_argument("--include-resolved", action="store_true")

    return p


def main():
    logging.basicConfig(level=logging.WARNING)
    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.command == "get":
            result = get(args.doc_id)
        elif args.command == "search_replace":
            result = search_replace(
                args.doc_id, args.find, args.replace, args.occurrence, args.regex,
                suggest=args.suggest,
            )
        elif args.command == "insert_after":
            result = insert_after(
                args.doc_id, args.anchor, args.text, rich=args.rich, suggest=args.suggest
            )
        elif args.command == "insert_before":
            result = insert_before(
                args.doc_id, args.anchor, args.text, rich=args.rich, suggest=args.suggest
            )
        elif args.command == "delete_paragraph":
            result = delete_paragraph(args.doc_id, args.anchor, suggest=args.suggest)
        elif args.command == "append":
            result = append(args.doc_id, args.text, rich=args.rich, suggest=args.suggest)
        elif args.command == "batch_replace":
            replacements = json.loads(args.replacements)
            result = batch_replace(args.doc_id, replacements, suggest=args.suggest)
        elif args.command == "list_suggestions":
            result = list_suggestions(args.doc_id, include_closed=args.include_closed)
        elif args.command == "review_suggestions":
            result = review_suggestions(args.doc_id, args.action, args.ids)
        elif args.command == "convert":
            result = convert_to_google_doc(args.file_id, args.name)
        elif args.command == "add_comment":
            result = add_comment(
                args.doc_id,
                args.comment,
                args.anchor,
                args.occurrence,
                assignee_email=args.assignee,
            )
        elif args.command == "read_comments":
            result = read_comments(args.doc_id, include_resolved=args.include_resolved)
        else:
            parser.print_help()
            sys.exit(1)

        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
