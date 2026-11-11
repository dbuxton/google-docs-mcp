#!/usr/bin/env python3
"""
google-docs-mcp — Surgical Google Docs editing for AI agents
=============================================================
Exposes Google Docs and Drive operations as MCP tools.

Core abstraction: search-by-text, not by character index.
LLMs describe WHAT they want to change; the server figures out WHERE.
All edits use real batchUpdate — version history, comments, and
suggestions are preserved. Every edit tool can instead record its change
as a suggestion (suggest=True) for a person to accept or reject.

Tools:
  Editing:  docs_get, docs_search_replace, docs_insert_after,
            docs_insert_before, docs_delete_paragraph, docs_append,
            docs_batch_replace
  Suggestions: docs_list_suggestions, docs_review_suggestions
  Format:   docs_format_paragraphs, docs_format_text
  Comments: docs_add_comment, docs_read_comments, docs_reply_to_comment,
            docs_resolve_comment, docs_delete_comment
  Manage:   docs_list, docs_create, docs_copy

Auth (in priority order):
  1. GOOGLE_DOCS_MCP_TOKEN env var   — standalone token (auth_setup.py)
  2. GOOGLE_DRIVE_MCP_TOKEN env var  — backward-compatible alias
  3. GOOGLE_DOCS_TOKEN_FILE env var  — legacy gog alias
  4. ~/.google-docs-mcp/token.json   — default standalone location
  5. ~/.google-drive-mcp/token.json  — backward-compatible fallback
  6. GOG_KEYRING_PASSWORD env var    — auto-export from gog CLI

Setup:
  python3 auth_setup.py --credentials ~/credentials.json
  python3 auth_setup.py --credentials ~/credentials.json --headless  # no browser
  GOOGLE_DOCS_MCP_CLIENT_ID=... GOOGLE_DOCS_MCP_CLIENT_SECRET=... python3 auth_setup.py

Transport: stdio (Claude Desktop / OpenClaw MCP config)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Literal, Optional

# Add the docs-edit script to path (if running from the repo alongside that skill)
# Or copy docs_edit.py here — we bundle a copy for standalone use.
sys.path.insert(0, os.path.dirname(__file__))

from fastmcp import FastMCP
import docs_edit

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

mcp = FastMCP(
    name="google-docs-mcp",
    instructions="""
Surgical Google Docs editing — search by text, never by character index.
Text inside tables is read, matched and edited like any other text.

TOOLS, with their exact parameter names (name=value is the default):
  Reading
    docs_get(doc_id)                     Document structure: paragraphs + plain text
    docs_list(query="", limit=20)        Recent docs, optionally filtered
  Editing — each takes suggest=True to propose the change instead of making it
    docs_search_replace(doc_id, find, replace, occurrence=1, regex=False, suggest=False)
    docs_insert_after(doc_id, anchor, text, rich=True, suggest=False)
    docs_insert_before(doc_id, anchor, text, rich=True, suggest=False)
    docs_delete_paragraph(doc_id, anchor, suggest=False)
    docs_append(doc_id, text, rich=True, suggest=False)
    docs_batch_replace(doc_id, replacements_json, suggest=False)   atomic, one API call
  Suggestions — tracked changes that a person accepts or rejects in Docs
    docs_list_suggestions(doc_id, include_closed=False)
    docs_review_suggestions(doc_id, action, suggestion_ids)   action: accept | reject | delete
  Comments
    docs_add_comment(doc_id, comment, anchor_text, occurrence=1, assignee_email="")
    docs_read_comments(doc_id, include_resolved=False)
    docs_reply_to_comment(doc_id, comment_id, reply)
    docs_resolve_comment(doc_id, comment_id, reply="")
    docs_delete_comment(doc_id, comment_id)
  Formatting — fix structure that has gone wrong; JSON list of operations, atomic, dry_run to preview
    docs_format_paragraphs(doc_id, operations_json, dry_run=False)   lists, heading styles, alignment, spacing
    docs_format_text(doc_id, operations_json, dry_run=False)         bold/italic/underline, font, colour, links
  Documents
    docs_create(title, initial_text="")
    docs_copy(doc_id, name="", folder_id="")   Duplicate a doc (or any Drive file): fill in a template
    docs_convert_to_google_doc(file_id, name="")   Google Doc copy of a Word/RTF/text/HTML/PDF file

SUGGESTIONS vs EDITS:
  Use suggest=True when the user asks you to propose, suggest, redline or "track"
  changes, or says not to edit the document directly. The text is left alone and
  Docs shows the change for the owner to accept or reject. Otherwise edits are
  applied directly.
  After suggest=True, docs_get shows the text as it will read once every suggestion
  is accepted, so later anchors should quote that text. docs_list_suggestions shows
  what each suggestion changes; docs_review_suggestions accepts/rejects/deletes them.
  Suggestions appear as made by the signed-in Google account.

FIXING FORMATTING (lists, headings, alignment, bold/links) — for structure that has gone wrong:
  docs_get shows each paragraph's list (id, level, bullet/numbered), alignment, indent_start and
  inline formats. Then repair it with docs_format_paragraphs / docs_format_text, which take a JSON
  list of operations, apply them atomically, select by anchor text or regex, and support
  dry_run=True to preview. They edit directly (they cannot be suggestions).

FILLING IN A TEMPLATE: docs_copy the template, then edit the copy by its returned id (never the
original). Edits apply to the copy only, so one template can serve any number of documents.

TYPICAL WORKFLOW:
  1. docs_get — read the document
  2. docs_search_replace / docs_insert_after / etc. — make targeted edits
  3. docs_get — verify changes
  For redlining: the same edits with suggest=True → docs_list_suggestions
  To fix messy formatting: docs_get → docs_format_paragraphs / docs_format_text
  (dry_run first for large changes) → docs_get.
  For review: docs_add_comment → docs_read_comments → docs_resolve_comment

WORD FILES AND PDFS (not Google Docs): only real Google Docs can be edited or given suggestions.
docs_get still reads a .docx (text, tables, and Word's tracked changes shown as suggestions,
which docs_list_suggestions lists read-only) and a PDF (text). The comment tools fall back to
Drive comments on the whole file ("source": "drive"); comments saved inside a .docx are
listed with "origin": "embedded" and are read-only. To edit a .docx or accept its tracked
changes, make a Google Doc copy first with docs_convert_to_google_doc: it keeps tables and turns
tracked changes into suggestions and Word comments into ordinary comments.
docs_list does not list Word files or PDFs: use their Drive file IDs.

All edits preserve version history. Use short, distinctive anchor text
(a few unique words) for reliable text matching.
""".strip(),
)


@mcp.tool
def docs_get(doc_id: str) -> str:
    """
    Read a Google Doc and return its structure as JSON.

    Returns title, list of paragraphs (with text, style, start/end indices),
    and the full plain text. Use this before editing to understand the document.

    Paragraphs inside tables are included in document order, each with a
    "table_cell" giving its table, row and column.

    If the document has pending suggestions, the text is the document as it will
    read once they are all accepted (suggested deletions are hidden). Paragraphs a
    suggestion touches carry "suggestions" (IDs), and "suggestion_ids" lists them
    all; docs_list_suggestions shows what each one changes.

    A .docx stored in Drive reads the same way (but cannot be edited). A PDF is
    readable too: its extracted text comes back in plain_text with a "warning"
    that it is a read-only PDF, not a Google Doc.

    Args:
        doc_id: Google Doc ID (from the URL: /document/d/{DOC_ID}/edit) or a
                Drive file ID of a PDF
    """
    result = docs_edit.get(doc_id)
    return json.dumps(result, indent=2)


@mcp.tool
def docs_search_replace(
    doc_id: str,
    find: str,
    replace: str,
    occurrence: int = 1,
    regex: bool = False,
    suggest: bool = False,
) -> str:
    """
    Find text in a Google Doc and replace a specific occurrence.

    Preserves document history — uses real batchUpdate, not delete-and-rewrite.
    Text inside tables is found too.

    Args:
        doc_id:     Google Doc ID
        find:       Text to search for (or regex pattern if regex=True)
        replace:    Text to replace it with
        occurrence: Which occurrence to replace. 1 = first (default), 2 = second,
                    0 = replace ALL occurrences.
        regex:      If True, treat `find` as a Python regular expression
        suggest:    If True, record the change as a suggestion for the owner to
                    accept or reject instead of editing the text

    Returns:
        JSON with: ok, replaced (original text), at_index, occurrences_found
        (and, when suggest=True, suggested and suggestion_ids, the suggestions created)
    """
    result = docs_edit.search_replace(doc_id, find, replace, occurrence, regex, suggest=suggest)
    return json.dumps(result, indent=2)


@mcp.tool
def docs_insert_after(
    doc_id: str, anchor: str, text: str, rich: bool = True, suggest: bool = False
) -> str:
    """
    Insert a new paragraph immediately after the paragraph containing `anchor`.

    The anchor is matched case-insensitively as a substring of the paragraph.

    Rich formatting is ON by default. Set rich=False to insert literal text.

    Supported rich subset:
      - # / ## / ### headings
      - - item / * item bullet lists
      - 1. item / 1) item numbered lists
      - **bold**, *italic*, ***bold italic***

    Args:
        doc_id:  Google Doc ID
        anchor:  Text to search for to find the target paragraph
        text:    Text to insert as the new paragraph
        rich:    If True (default), interpret simple markdown-like formatting natively
        suggest: If True, record the insertion as a suggestion instead of editing the text

    Returns:
        JSON with: ok, inserted_after (matched paragraph preview), at_index
        (and, when suggest=True, suggested and suggestion_ids, the suggestions created)
    """
    result = docs_edit.insert_after(doc_id, anchor, text, rich=rich, suggest=suggest)
    return json.dumps(result, indent=2)


@mcp.tool
def docs_insert_before(
    doc_id: str, anchor: str, text: str, rich: bool = True, suggest: bool = False
) -> str:
    """
    Insert a new paragraph immediately before the paragraph containing `anchor`.

    The anchor is matched case-insensitively as a substring of the paragraph.

    Rich formatting is ON by default. Set rich=False to insert literal text.

    Supported rich subset:
      - # / ## / ### headings
      - - item / * item bullet lists
      - 1. item / 1) item numbered lists
      - **bold**, *italic*, ***bold italic***

    Args:
        doc_id:  Google Doc ID
        anchor:  Text to search for to find the target paragraph
        text:    Text to insert as the new paragraph
        rich:    If True (default), interpret simple markdown-like formatting natively
        suggest: If True, record the insertion as a suggestion instead of editing the text

    Returns:
        JSON with: ok, inserted_before (matched paragraph preview), at_index
        (and, when suggest=True, suggested and suggestion_ids, the suggestions created)
    """
    result = docs_edit.insert_before(doc_id, anchor, text, rich=rich, suggest=suggest)
    return json.dumps(result, indent=2)


@mcp.tool
def docs_delete_paragraph(doc_id: str, anchor: str, suggest: bool = False) -> str:
    """
    Delete the paragraph(s) containing `anchor` text.

    Deletes ALL paragraphs that contain the anchor string (case-insensitive).
    If the anchor matches only one paragraph, only that paragraph is deleted.
    Paragraphs inside tables count. Docs won't delete the closing newline of the last
    paragraph of the document or of a table cell, or of one directly before a table
    or section break, so those are emptied rather than removed.

    Args:
        doc_id:  Google Doc ID
        anchor:  Text to search for in paragraphs to delete
        suggest: If True, record the deletion as a suggestion instead of editing the text

    Returns:
        JSON with: ok, deleted_count, deleted (list of deleted paragraph previews)
        (and, when suggest=True, suggested and suggestion_ids, the suggestions created)
    """
    result = docs_edit.delete_paragraph(doc_id, anchor, suggest=suggest)
    return json.dumps(result, indent=2)


@mcp.tool
def docs_append(doc_id: str, text: str, rich: bool = True, suggest: bool = False) -> str:
    """
    Append a new paragraph at the end of a Google Doc.

    Rich formatting is ON by default. Set rich=False to append literal text.

    Supported rich subset:
      - # / ## / ### headings
      - - item / * item bullet lists
      - 1. item / 1) item numbered lists
      - **bold**, *italic*, ***bold italic***

    Args:
        doc_id:  Google Doc ID
        text:    Text to append as the final paragraph
        rich:    If True (default), interpret simple markdown-like formatting natively
        suggest: If True, record the addition as a suggestion instead of editing the text

    Returns:
        JSON with: ok, appended (text preview), at_index
        (and, when suggest=True, suggested and suggestion_ids, the suggestions created)
    """
    result = docs_edit.append(doc_id, text, rich=rich, suggest=suggest)
    return json.dumps(result, indent=2)


@mcp.tool
def docs_batch_replace(doc_id: str, replacements_json: str, suggest: bool = False) -> str:
    """
    Apply multiple find→replace operations atomically in a single batchUpdate.

    All replacements are applied in one API call (end-of-document first to
    preserve index validity). Either ALL changes succeed, or none do.

    Args:
        doc_id:           Google Doc ID
        replacements_json: JSON array of replacements, e.g.:
                          '[{"find": "Q1", "replace": "Q2"},
                            {"find": "draft", "replace": "final", "occurrence": 0}]'

                          Each item: {"find": str, "replace": str,
                                      "occurrence": int (default 1, 0=all),
                                      "regex": bool (default false)}
        suggest:          If True, record the changes as suggestions instead of
                          editing the text

    Returns:
        JSON with: ok, applied (count), changes (list of what changed)
        (and, when suggest=True, suggested and suggestion_ids, the suggestions created)
    """
    replacements = json.loads(replacements_json)
    result = docs_edit.batch_replace(doc_id, replacements, suggest=suggest)
    return json.dumps(result, indent=2)


def _load_operations(operations_json: str) -> list:
    operations = json.loads(operations_json)
    return [operations] if isinstance(operations, dict) else operations


@mcp.tool
def docs_format_paragraphs(
    doc_id: str,
    operations_json: str,
    dry_run: bool = False,
) -> str:
    """
    Repair or set paragraph formatting (lists, heading styles, alignment, spacing)
    for many paragraphs at once, atomically in one API call.

    Use this when a document's structure is wrong: bullets applied to every
    paragraph, numbered lists that came out as bullets, headings that are plain
    text, stray indents. Call docs_get first — each paragraph reports its
    "list" (id, level, bullet/numbered), "alignment", "indent_start" and inline
    "formats" — then fix it here. Use dry_run=true to preview the matches.

    Each operation selects paragraphs, then says what to change:

      Selection (anchors are case-insensitive substrings; never character indices):
        from          first paragraph containing this text
        to            last paragraph of the range (inclusive), first match at/after `from`
        match_regex   only paragraphs whose text matches this regex. With it,
                      from/to merely limit the scope (default: whole document).
                      Empty paragraphs never match "." — use "^" to include them.
        A `from` alone (no match_regex) selects just that one paragraph.

      Changes (at least one):
        style                   NORMAL_TEXT, TITLE, SUBTITLE, HEADING_1..HEADING_6
                                (also "H2", "heading 2")
        list                    "none" strips bullets/numbering AND the leftover indent;
                                "bullet"; "numbered"; or a Docs preset such as
                                NUMBERED_UPPERALPHA_ALPHA_ROMAN
        alignment               left | center | right | justified
        reset_indent            true to zero the paragraph indents
        space_above / space_below   points
        line_spacing            percent (100 = single)
        clear_text_formatting   true to reset bold/italic/underline/colour/size/font/link

    Operations apply in order against one snapshot; where several select the same
    paragraph, the later one wins. So "clear everything, then re-apply" works:
      [{"match_regex": ".", "list": "none", "style": "NORMAL_TEXT"},
       {"from": "Introduction", "style": "HEADING_1"},
       {"from": "First step", "to": "Last step", "list": "numbered"}]
    Consecutive paragraphs given the same `list` value become ONE list (numbering
    continues); separate groups restart at 1.

    Args:
        doc_id:          Google Doc ID
        operations_json: JSON array of operations (a single object is also accepted)
        dry_run:         If true, report what would change without editing

    Returns:
        JSON with: ok, dry_run, paragraphs_changed, requests, operations
        (per operation: matched count and a preview of the first matches)
    """
    result = docs_edit.format_paragraphs(doc_id, _load_operations(operations_json), dry_run)
    return json.dumps(result, indent=2)


@mcp.tool
def docs_format_text(
    doc_id: str,
    operations_json: str,
    dry_run: bool = False,
) -> str:
    """
    Apply character-level formatting (bold, italic, underline, strikethrough, font,
    size, colour, highlight, links) to matched text, atomically in one API call.

    Use this to fix or add inline styling that docs_search_replace and the
    markdown-style rich insert cannot reach: un-bold a whole section, restyle
    every "Note:" label, remove stray links, reset text that picked up odd
    formatting. Use dry_run=true to preview the matches.

    Each operation selects text, then says what to change:

      Selection — either by content:
        find          literal text (or a regex with "regex": true)
        occurrence    1 = first match (default), 2 = second, 0 = all matches
        from / to     optionally limit `find` to a paragraph range (anchor text,
                      inclusive); `from` alone means "from there to the end"
      — or by whole paragraphs (omit `find`):
        from / to     paragraphs from `from` through `to` (or just `from`)
        match_regex   only paragraphs whose text matches this regex

      Changes (at least one):
        bold, italic, underline, strikethrough   true or false
        font_family   e.g. "Arial"
        font_size     points
        color         text colour as "#RRGGBB"
        background    highlight colour as "#RRGGBB"
        link          a URL to set, or "" to remove an existing link
        clear         true to first reset all of the above to the style's defaults

    Operations apply in order, so later ones win where they overlap. Example:
      [{"from": "Summary", "to": "Next steps", "clear": true},
       {"find": "Note:", "occurrence": 0, "bold": true, "color": "#B00020"}]

    Args:
        doc_id:          Google Doc ID
        operations_json: JSON array of operations (a single object is also accepted)
        dry_run:         If true, report what would change without editing

    Returns:
        JSON with: ok, dry_run, requests, operations
        (per operation: matched count and a preview of the first matches)
    """
    result = docs_edit.format_text(doc_id, _load_operations(operations_json), dry_run)
    return json.dumps(result, indent=2)


@mcp.tool
def docs_list_suggestions(doc_id: str, include_closed: bool = False) -> str:
    """
    List the suggestions in a Google Doc and exactly what each one changes.

    Covers suggestions made by people in the Docs UI as well as ones made with
    suggest=True. Use the returned ids with docs_review_suggestions.

    Args:
        doc_id:         Google Doc ID
        include_closed: Also list suggestions already accepted or rejected
                        (default False)

    Returns:
        JSON with count and suggestions: id, status, author, created, summary
        (Google's one-line description), changes (each: type insertion |
        deletion | text_style | paragraph_style | bullet, text, context — the
        paragraph it sits in) and replies
    """
    return json.dumps(docs_edit.list_suggestions(doc_id, include_closed), indent=2)


@mcp.tool
def docs_review_suggestions(
    doc_id: str,
    action: Literal["accept", "reject", "delete"],
    suggestion_ids: list[str],
) -> str:
    """
    Accept, reject or delete suggestions in a Google Doc, all in one request.

    accept   Apply the change to the text (needs edit access).
    reject   Throw the change away (edit access, or being its author).
    delete   Withdraw your own suggestion: the text stays as it was, as with reject, but
             the suggestion leaves no record (its author only).

    All the suggestions are processed together: if any ID is wrong, none are.

    Args:
        doc_id:         Google Doc ID
        action:         "accept", "reject" or "delete"
        suggestion_ids: IDs from docs_list_suggestions (or from the suggestion_ids
                        an edit made with suggest=True returned)

    Returns:
        JSON with: ok, action, applied (the IDs Google confirms it processed),
        and unconfirmed for any it did not list
    """
    return json.dumps(docs_edit.review_suggestions(doc_id, action, suggestion_ids), indent=2)


@mcp.tool
def docs_add_comment(
    doc_id: str,
    comment: str,
    anchor_text: str,
    occurrence: int = 1,
    assignee_email: str = "",
) -> str:
    """
    Add a comment anchored to specific text in a Google Doc.

    The comment appears in the Docs UI as a normal inline highlight on the
    matched text, exactly like a comment a person added. Text inside tables can
    be anchored too.

    For a file that is not a Google Doc (a .docx opened in Office editing mode,
    a PDF) the comment is posted on the file through Drive instead. Drive cannot
    anchor it, so it starts with a quote of `anchor_text`, and the result has
    anchored=false and source="drive".

    Args:
        doc_id:      Google Doc ID (from the URL: /document/d/{DOC_ID}/edit)
        comment:     The comment text to post
        anchor_text: Exact text in the document to attach the comment to.
                     Use a short, unique phrase (a few words) for reliable matching.
        occurrence:  Which occurrence of anchor_text to use (default 1 = first).
                     Use 2, 3, etc. if the text appears multiple times.
        assignee_email: Optional email of a user to assign the comment to as an
                     action item (they are notified by Google Docs).

    Returns:
        JSON with: ok, comment_id, anchored_to (matched text), at_index
    """
    result = docs_edit.add_comment(
        doc_id,
        comment,
        anchor_text,
        occurrence,
        assignee_email=assignee_email or None,
    )
    return json.dumps(result, indent=2)


@mcp.tool
def docs_reply_to_comment(doc_id: str, comment_id: str, reply: str) -> str:
    """
    Reply to an existing comment on a Google Doc.

    Args:
        doc_id:     Google Doc ID
        comment_id: ID of the comment to reply to (from docs_read_comments)
        reply:      Reply text to post

    Returns:
        JSON with: ok, reply_id, comment_id, content
    """
    return json.dumps(docs_edit.reply_to_comment(doc_id, comment_id, reply), indent=2)


@mcp.tool
def docs_resolve_comment(doc_id: str, comment_id: str, reply: str = "") -> str:
    """
    Resolve (close) a comment on a Google Doc, optionally with a final reply.

    Args:
        doc_id:     Google Doc ID
        comment_id: ID of the comment to resolve (from docs_read_comments)
        reply:      Optional reply text to post with the resolution

    Returns:
        JSON with: ok, comment_id, resolved
    """
    return json.dumps(docs_edit.resolve_comment(doc_id, comment_id, reply), indent=2)


@mcp.tool
def docs_delete_comment(doc_id: str, comment_id: str) -> str:
    """
    Delete a comment from a Google Doc. Only the comment's author can delete it.

    Args:
        doc_id:     Google Doc ID
        comment_id: ID of the comment to delete

    Returns:
        JSON with: ok, comment_id
    """
    return json.dumps(docs_edit.delete_comment(doc_id, comment_id), indent=2)


@mcp.tool
def docs_read_comments(doc_id: str, include_resolved: bool = False) -> str:
    """
    Read all comments on a Google Doc.

    Returns each comment thread with its content, author, the document text it
    is anchored to, any assignee, its replies, and whether it's resolved.

    For a file that is not a Google Doc (a .docx opened in Office editing mode,
    a PDF) these are the file's Drive comments, and the result has source="drive".

    Args:
        doc_id:           Google Doc ID
        include_resolved: Include resolved comments (default False)

    Returns:
        JSON with count and comments: id, content, author, quoted_text, assignee,
        resolved, created, replies (id, author, content, created)
    """
    return json.dumps(docs_edit.read_comments(doc_id, include_resolved), indent=2)


@mcp.tool
def docs_list(query: str = "", limit: int = 20) -> str:
    """
    List Google Docs from Drive, optionally filtered by a search query.

    Args:
        query: Optional search terms (searches title and content)
        limit: Maximum number of results (default 20)

    Returns:
        JSON array of {id, name, modifiedTime, webViewLink}
    """
    from googleapiclient.discovery import build
    creds = docs_edit._load_creds()
    drive = build("drive", "v3", credentials=creds)

    q = 'mimeType="application/vnd.google-apps.document" and trashed=false'
    if query:
        q += f' and fullText contains "{query}"'

    results = drive.files().list(
        q=q,
        pageSize=min(limit, 100),
        fields="files(id, name, modifiedTime, webViewLink)",
        orderBy="modifiedTime desc",
    ).execute()

    files = results.get("files", [])
    return json.dumps(files, indent=2)


@mcp.tool
def docs_create(title: str, initial_text: str = "") -> str:
    """
    Create a new Google Doc with an optional initial paragraph of text.

    Args:
        title:        Document title
        initial_text: Optional first paragraph content

    Returns:
        JSON with: id, title, webViewLink
    """
    creds = docs_edit._load_creds()
    from googleapiclient.discovery import build

    service = build("docs", "v1", credentials=creds)
    doc = service.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]

    if initial_text:
        service.documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [{
                    "insertText": {
                        "location": {"index": 1},
                        "text": initial_text,
                    }
                }]
            },
        ).execute()

    return json.dumps({
        "id": doc_id,
        "title": title,
        "webViewLink": f"https://docs.google.com/document/d/{doc_id}/edit",
    }, indent=2)


@mcp.tool
def docs_copy(doc_id: str, name: str = "", folder_id: str = "") -> str:
    """
    Duplicate a document: the same as File > Make a copy in Google Docs.

    Use this to start from a template or a previous version without touching it: copy it,
    then edit the copy with the other docs_* tools using the returned id. The copy keeps
    all content and formatting; comments may not come across (check with docs_read_comments).
    It works on any Drive file (a
    Word file or PDF is copied as it is, not converted: see docs_convert_to_google_doc).

    Args:
        doc_id:    Google Doc ID (or other Drive file ID) to copy
        name:      Title for the copy (default: "Copy of <original's title>")
        folder_id: Drive folder ID to put the copy in (default: beside the original)

    Returns:
        JSON with: ok, id (the copy: use it from now on), name, webViewLink,
        copied_from (id, name, type) and a note
    """
    return json.dumps(docs_edit.copy_doc(doc_id, name, folder_id), indent=2)


@mcp.tool
def docs_convert_to_google_doc(file_id: str, name: str = "") -> str:
    """
    Make a Google Doc from a Word, RTF, OpenDocument, plain-text, HTML or PDF file in Drive.

    Only real Google Docs can be edited or given suggestions. Use this on a .docx first:
    the copy keeps tables, turns Word's tracked changes into suggestions (so they can be
    listed, accepted or rejected) and Word's comments into ordinary comments (so they can
    be replied to and resolved). Then use the returned id with the other docs_* tools.

    The original is left alone and each call makes a new copy next to it. A file that is
    already a Google Doc comes back unchanged (converted=false). A PDF is converted by text
    recognition, so check the result with docs_get.

    Args:
        file_id: Drive file ID of the file to convert (from its URL)
        name:    Title for the copy (default: the original's name without its extension)

    Returns:
        JSON with: ok, converted, id (the Google Doc to use from now on), name,
        webViewLink, converted_from (id, name, type) and a note
    """
    return json.dumps(docs_edit.convert_to_google_doc(file_id, name), indent=2)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
