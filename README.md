# google-docs-mcp

> Surgical Google Docs editing for AI agents — preserves history, never touches character indices.

An MCP server that makes Google Docs actually usable for LLMs. **Standalone** — no other tools required beyond a Google Cloud OAuth app.

Compatibility note: the repo, package, CLI, env vars, and default token path now use `google-docs-mcp`. Backward-compatible `google-drive-mcp` command and env-var aliases are still accepted so existing setups do not break immediately.

Indexing note: when the MCP reads document content before planning index-based edits, it requests `suggestionsViewMode=SUGGESTIONS_INLINE`. That keeps returned indices aligned for later `documents.batchUpdate` calls when the doc contains suggestions. Anchor text is matched against the document as it will read once pending suggestions are accepted (suggested deletions are ignored), so an edit can follow a suggestion made earlier.

Tables note: text inside tables is read by `docs_get` and can be matched, edited and commented on like any other text.

Non-Docs note: the Docs API treats a Drive file that is not a Google Doc in two ways. A PDF is refused with a bare 400 `Request contains an invalid argument`, and `docs_get` then downloads it (up to 20 MB) and returns its extracted text with a read-only `warning`. A `.docx` is read without complaint (Docs converts it on the fly) but every write, edits and comments alike, is refused with `The document must not be an Office file`. Either way the MCP asks Drive what the file is: the edit tools fail with a message naming the file and its type, and the comment tools fall back to Drive comments on the file (see [Comments](#comments)).

## Why

The Google Docs API uses **character indices** for every edit. LLMs are bad at counting characters. Everyone ends up deleting and rewriting entire documents, which destroys version history, comments, and collaborator attribution.

This server uses the same abstraction as code editors: **search by text, not by position**. You describe *what* to change; the server finds *where* it is and handles the index arithmetic.

---

## Quick start

### 1. Run directly from GitHub

```bash
uvx --from git+https://github.com/dbuxton/google-docs-mcp google-docs-mcp --help
```

That command downloads the package, creates an isolated environment, installs dependencies, and runs the `google-docs-mcp` entry point.

No PyPI release is required. `uvx` can execute the tool straight from the GitHub repository.

To pin to a branch, tag, or commit, add a ref to the URL:

```bash
uvx --from git+https://github.com/dbuxton/google-docs-mcp@main google-docs-mcp --help
```

If you prefer a persistent local install instead of `uvx`, use:

```bash
uv tool install --from git+https://github.com/dbuxton/google-docs-mcp google-docs-mcp
uv tool install --from git+https://github.com/dbuxton/google-docs-mcp google-docs-mcp-auth
```

### 2. Create a Google Cloud OAuth app

1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Create a project (or select an existing one)
3. Enable these APIs (**APIs & Services → Library**):
   - **Google Docs API**
   - **Google Drive API**
4. Go to **APIs & Services → Credentials**
5. Click **Create Credentials → OAuth 2.0 Client ID**
6. Application type: **Desktop App**
7. Download the JSON file

### 3. Authenticate

**Normal — browser opens automatically:**
```bash
uvx --from git+https://github.com/dbuxton/google-docs-mcp \
  google-docs-mcp-auth --credentials ~/credentials.json
```

**Or use env vars instead of a credentials file:**
```bash
export GOOGLE_DOCS_MCP_CLIENT_ID="your-google-client-id"
export GOOGLE_DOCS_MCP_CLIENT_SECRET="your-google-client-secret"

uvx --from git+https://github.com/dbuxton/google-docs-mcp \
  google-docs-mcp-auth
```

**Headless / remote server — no browser on device:**
```bash
uvx --from git+https://github.com/dbuxton/google-docs-mcp \
  google-docs-mcp-auth --credentials ~/credentials.json --headless
# Prints a URL → open on any device (phone, laptop, etc.)
# Paste the full redirect URL back into the terminal
```

**Already have an auth code:**
```bash
uvx --from git+https://github.com/dbuxton/google-docs-mcp \
  google-docs-mcp-auth --credentials ~/credentials.json --code "4/0Afr..."
```

`google-docs-mcp-auth` resolves OAuth client credentials in this order:

1. `--credentials /path/to/credentials.json`
2. `--client-id` and `--client-secret`
3. `GOOGLE_DOCS_MCP_CLIENT_ID` and `GOOGLE_DOCS_MCP_CLIENT_SECRET`

Token is saved to `~/.google-docs-mcp/token.json` by default. Override with `--out /path/to/token.json`.

### 4. Configure your MCP client

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "google-docs": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/dbuxton/google-docs-mcp", "google-docs-mcp"],
      "env": {
        "GOOGLE_DOCS_MCP_TOKEN": "/Users/you/.google-docs-mcp/token.json"
      }
    }
  }
}
```

**OpenClaw** (gateway config):
```json
{
  "mcp": {
    "servers": {
      "google-docs": {
        "command": "uvx",
        "args": ["--from", "git+https://github.com/dbuxton/google-docs-mcp", "google-docs-mcp"],
        "env": {
          "GOOGLE_DOCS_MCP_TOKEN": "~/.google-docs-mcp/token.json"
        }
      }
    }
  }
}
```

**Local checkout during development:**
```json
{
  "mcpServers": {
    "google-docs": {
      "command": "uvx",
      "args": ["--from", "/absolute/path/to/google-docs-mcp", "google-docs-mcp"],
      "env": {
        "GOOGLE_DOCS_MCP_TOKEN": "/Users/you/.google-docs-mcp/token.json"
      }
    }
  }
}
```

---

## Tools reference

### Document reading

#### `docs_get(doc_id)`
Read a Google Doc and return its full structure.

Returns the document title, a list of paragraphs (with text, heading style, and character indices), and the full plain text. Use this first to understand the document before making edits.

```
docs_get(doc_id="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms")
```

Returns:
```json
{
  "title": "My Document",
  "paragraphs": [
    {"text": "Introduction", "style": "HEADING_1", "start": 1, "end": 14},
    {"text": "This is the body.", "style": "NORMAL_TEXT", "start": 14, "end": 32},
    {"text": "12 months", "style": "NORMAL_TEXT", "start": 36, "end": 46,
     "table_cell": {"table": 0, "row": 0, "column": 1}}
  ],
  "plain_text": "Introduction\nThis is the body.\n12 months"
}
```

- **Tables.** Paragraphs inside tables are included in document order; each carries a `table_cell` with its table, row and column (counted from 0).
- **Pending suggestions.** The text is the document as it will read once every suggestion is accepted (suggested deletions are hidden). Paragraphs a suggestion touches carry `suggestions` (IDs) and the result has `suggestion_ids` and a `note`. `docs_list_suggestions` shows what each one changes.

---

#### `docs_list(query, limit)`
List Google Docs from Drive, optionally filtered by a search query. Word files and PDFs are not listed; pass their Drive file IDs to the other tools.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | `""` | Search terms (searches title and content) |
| `limit` | int | `20` | Maximum results |

```
docs_list(query="board deck 2026", limit=5)
```

---

### Document editing

All editing tools use **text anchors**, never character indices. The server finds the text and handles the indices internally. Text inside tables is found like any other text.

Every editing tool also takes **`suggest`** (default `false`). With `suggest=true` the change is recorded as a suggestion for someone to accept or reject, instead of editing the text. See [Suggestions](#suggestions).

#### `docs_search_replace(doc_id, find, replace, occurrence, regex, suggest)`
Find text in a document and replace a specific occurrence.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `doc_id` | string | required | Google Doc ID |
| `find` | string | required | Text to find |
| `replace` | string | required | Replacement text |
| `occurrence` | int | `1` | Which occurrence: `1` = first, `2` = second, `0` = **all** |
| `regex` | bool | `false` | Treat `find` as a Python regex |
| `suggest` | bool | `false` | Record the change as a suggestion instead of editing the text |

```
# Replace first occurrence
docs_search_replace(doc_id="...", find="Q1 2024", replace="Q2 2024")

# Replace all occurrences
docs_search_replace(doc_id="...", find="ACME Corp", replace="Initech", occurrence=0)

# Regex replace
docs_search_replace(doc_id="...", find=r"\bDraft\b", replace="Final", regex=true)
```

---

#### `docs_insert_after(doc_id, anchor, text)`
Insert a new paragraph immediately after the paragraph containing `anchor`.

```
docs_insert_after(
  doc_id="...",
  anchor="Executive Summary",
  text="Updated as of March 2026 following board review."
)
```

---

#### `docs_insert_before(doc_id, anchor, text)`
Insert a new paragraph immediately before the paragraph containing `anchor`.

```
docs_insert_before(
  doc_id="...",
  anchor="Appendix A",
  text="See the following appendix for supporting data."
)
```

---

#### `docs_delete_paragraph(doc_id, anchor, suggest)`
Delete all paragraphs containing `anchor` text (case-insensitive), including ones in tables. Docs won't delete the closing newline of the last paragraph of the document or of a table cell, or of one directly before a table or section break; those paragraphs are emptied rather than removed.

```
docs_delete_paragraph(doc_id="...", anchor="[PLACEHOLDER — DELETE ME]")
```

---

#### `docs_append(doc_id, text)`
Append a new paragraph at the end of the document.

```
docs_append(doc_id="...", text="Document last updated: March 2026.")
```

---

#### `docs_batch_replace(doc_id, replacements_json)`
Apply multiple find→replace operations **atomically** in a single API call. Either all changes succeed or none do.

```
docs_batch_replace(
  doc_id="...",
  replacements_json='[
    {"find": "[CLIENT]", "replace": "Acme Corp", "occurrence": 0},
    {"find": "[DATE]", "replace": "10 March 2026", "occurrence": 0},
    {"find": "DRAFT", "replace": "FINAL"}
  ]'
)
```

Each item in the array:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `find` | string | required | Text to find |
| `replace` | string | required | Replacement text |
| `occurrence` | int | `1` | `1` = first, `0` = all |
| `regex` | bool | `false` | Regex mode |

---

#### `docs_create(title, initial_text)`
Create a new Google Doc.

```
docs_create(title="Q2 Board Deck", initial_text="Confidential — not for distribution.")
```

Returns `{id, title, webViewLink}`.

---

### Suggestions

Suggestions are Docs' tracked changes: the text stays as it is, and the proposed change is shown for the owner to accept or reject. Use them when an agent should **propose** edits rather than make them (contract redlining, editing someone else's draft).

Pass `suggest=true` to any editing tool. The arguments and anchors are the same as for a direct edit; the result gains `suggested` and the `suggestion_ids` created:

```
docs_search_replace(doc_id="...", find="14 days", replace="30 days", suggest=true)
```
```json
{"ok": true, "replaced": "14 days", "at_index": 23, "occurrences_found": 1,
 "suggested": true, "suggestion_ids": ["suggest.abc123"]}
```

Under the hood this sends the same `batchUpdate` requests as a direct edit with `writeControl.writeMode = "SUGGEST"`. The Docs API supports that since 30 September 2026 (developer preview from 7 July), with no new scope or dependency.

- **Confirmation.** A suggest-mode batch can commit its text changes yet fail to save the suggestion, which leaves the edit live with nothing to review. So an edit is only reported as a suggestion when Google names a suggestion it created or changed; otherwise the tool fails with `SuggestionNotSavedError`, saying the change may have been applied directly.
- **Which IDs.** `suggestion_ids` are the suggestions the edit created. If it folded into an existing suggestion (which may be someone else's) that is `updated_suggestion_ids`, and if it only deleted text a pending suggestion had inserted, a suggestion it shrank away is `removed_suggestion_ids`. Check `docs_list_suggestions` for the author before accepting an ID you did not create. Google may also split one edit into several suggestions: an insert with a bullet list comes back as the text and, separately, its list formatting, so review all the IDs returned.
- **Attribution.** Suggestions are authored by the Google account you authenticated with, as if typed in Suggesting mode.
- **Reading.** `docs_get` shows the document as it will read once all suggestions are accepted, so later anchors quote what a reader sees and one edit can build on another.
- **Limits.** The Docs API refuses some requests in suggest mode: tab changes, named ranges, header/footer deletion, table column properties and a few document-style fields. The editing tools here use none of them.
- **Not itemised.** Pending changes to tables, images, footnotes, page breaks and the like are listed by `docs_list_suggestions` with Google's summary, and `docs_get` flags the paragraphs that hold them, but their `changes` stay empty.

#### `docs_list_suggestions(doc_id, include_closed)`
List the suggestions in a document: who made each, Google's one-line summary, and exactly which text it inserts, deletes or restyles. Covers suggestions people made in the Docs UI as well as ones made with `suggest=true`. Accepted and rejected ones are left out unless `include_closed=true`.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `doc_id` | string | required | Google Doc ID |
| `include_closed` | bool | `false` | Also list accepted and rejected suggestions |

```json
{
  "count": 1,
  "suggestions": [{
    "id": "suggest.abc123",
    "status": "OPEN",
    "author": "Dana Smith",
    "created": "2026-10-14T09:00:00Z",
    "summary": "Replace 14 days with 30 days",
    "changes": [
      {"type": "insertion", "text": "30 days", "context": "Payment is due within 30 days of invoice."},
      {"type": "deletion", "text": "14 days", "context": "Payment is due within 30 days of invoice."}
    ],
    "replies": []
  }]
}
```

`type` is `insertion`, `deletion`, `text_style` or `paragraph_style` (both with the `fields` changed) or `bullet`. `context` is the paragraph the change starts in.

#### `docs_review_suggestions(doc_id, action, suggestion_ids)`
Accept, reject or delete suggestions, all in one atomic request.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `doc_id` | string | required | Google Doc ID |
| `action` | `accept` \| `reject` \| `delete` | required | `accept` applies the change (edit access); `reject` throws it away (edit access, or its author); `delete` withdraws your own suggestion (its author only): the text stays as it was, as with reject, but a rejected suggestion stays listed under `include_closed` and a deleted one does not |
| `suggestion_ids` | list of strings | required | IDs from `docs_list_suggestions` or from an edit's `suggestion_ids` |

```
docs_review_suggestions(doc_id="...", action="accept", suggestion_ids=["suggest.abc123"])
```

Returns `ok`, `action` and `applied` (the IDs Google confirms it processed), plus `unconfirmed` for any it did not list. If one ID is wrong, none are processed.

---

### Comments

On a Google Doc, comments go through the Docs API: it is the only route Docs shows as anchored to the text (a comment made through Drive reads "Original content deleted").

**Word files and PDFs.** A `.docx` uploaded to Drive and opened in Office editing mode is not a Google Doc, and the Docs API refuses comment requests on it (`The document must not be an Office file`). For any file that is not a Google Doc, all five comment tools fall back to **Drive comments** on the file, and say so with `"source": "drive"`:

- `docs_add_comment` cannot anchor the comment, so it starts the comment with a quote of `anchor_text` and returns `"anchored": false`. An `assignee_email` is written at the start of the comment text, because Drive comments cannot be assigned through the API.
- `docs_read_comments` lists the file's Drive comments in the same shape, each marked `"origin": "drive"`, plus any comments saved inside a Word file, marked `"origin": "embedded"`. The Docs API shows those when it converts the file on the fly; Drive does not list them. Embedded comments are **read-only**: replying to, resolving or deleting one fails with a message saying so, because neither API can change them. (Convert the file to a Google Doc with `docs_convert_to_google_doc` to work with them; see below.)
- `docs_reply_to_comment`, `docs_resolve_comment` and `docs_delete_comment` work as usual on Drive comments.

### Word files (.docx)

A `.docx` in Drive can be worked with in part. Converting a copy to a Google Doc gives the full set; the conversion keeps tables, turns Word's tracked changes into suggestions (author and date kept) and turns its comments into ordinary comments.

| | the `.docx` as it is | a copy converted to a Google Doc |
|---|---|---|
| Read text, headings and tables (`docs_get`) | yes | yes |
| See Word's tracked changes (`docs_list_suggestions`) | yes, read-only | yes, as real suggestions |
| Accept or reject them (`docs_review_suggestions`) | no | yes |
| Edit the text, or propose changes (`suggest=true`) | no | yes |
| Read comments saved in the file | yes (`origin: embedded`) | yes, as ordinary comments |
| Add a comment | yes, as an unanchored Drive comment | yes, anchored to the text |
| Reply to, resolve or delete a comment | Drive comments only | yes |

`docs_convert_to_google_doc` makes that copy in one call.

#### `docs_convert_to_google_doc(file_id, name)`
Make a Google Doc from a Word, RTF, OpenDocument, plain-text, HTML or PDF file in Drive, then use the returned `id` with every other tool.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `file_id` | string | required | Drive file ID of the file to convert (from its URL) |
| `name` | string | `""` | Title for the copy (default: the original's name without its extension) |

```
docs_convert_to_google_doc(file_id="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms")
```

Returns `ok`, `converted`, `id` (the Google Doc to use from now on), `name`, `webViewLink`, `converted_from` (`id`, `name`, `type`) and a `note`.

- The original is left alone, and each call makes a **new** copy beside it.
- A file that is already a Google Doc comes back unchanged (`converted: false`). A Sheet, image, folder or other type that cannot be a Doc is refused before anything is copied.
- A PDF is converted by text recognition, so expect lost layout and some errors; check it with `docs_get`.
- It needs the `drive` scope, which the auth setup already requests.

Drive's own **File → Save as Google Docs** does the same by hand, and **File → Download → Microsoft Word (.docx)** converts back. `docs_list` only lists Google Docs, so pass a Word file's own Drive ID.

#### `docs_add_comment(doc_id, comment, anchor_text, occurrence, assignee_email)`
Add a comment anchored to specific text in the document. It shows in the Docs UI as a normal inline highlight on that text. Text inside tables can be anchored too.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `doc_id` | string | required | Google Doc ID |
| `comment` | string | required | Comment text (max 2048 UTF-8 bytes) |
| `anchor_text` | string | required | Text in the document to attach the comment to |
| `occurrence` | int | `1` | Which occurrence of `anchor_text` to use |
| `assignee_email` | string | `""` | Assign the comment to this user as an action item |

Use a short, distinctive phrase for `anchor_text` — a few words that are unique enough to match exactly one location. The server maps the matched text to its document range and sends a Docs API `insertComment` request; callers never deal with character indices.

```
docs_add_comment(
  doc_id="...",
  anchor_text="unable to perform the Employee's duties",
  comment="Legal risk: 3-month absence threshold may not satisfy Equality Act 2010 duty to make reasonable adjustments before terminating."
)
```

---

#### `docs_read_comments(doc_id, include_resolved)`
List all comments on a document.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `doc_id` | string | required | Google Doc ID |
| `include_resolved` | bool | `false` | Include resolved comments |

Returns `count` and `comments`, each with `id`, `content`, `author`, `quoted_text` (the document text it is anchored to), `assignee`, `resolved`, `created` and `replies`.

```
docs_read_comments(doc_id="...")
```

---

#### `docs_reply_to_comment(doc_id, comment_id, reply)`
Post a reply to an existing comment.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `doc_id` | string | required | Google Doc ID |
| `comment_id` | string | required | Comment ID (from `docs_read_comments`) |
| `reply` | string | required | Reply text |

```
docs_reply_to_comment(
  doc_id="...",
  comment_id="AAAB1iPyaUY",
  reply="Agreed — adding Carer's Leave clause before we sign."
)
```

---

#### `docs_resolve_comment(doc_id, comment_id, reply)`
Resolve (close) a comment, optionally with a final reply.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `doc_id` | string | required | Google Doc ID |
| `comment_id` | string | required | Comment ID |
| `reply` | string | `""` | Optional reply posted with the resolution |

```
docs_resolve_comment(
  doc_id="...",
  comment_id="AAAB1iPyaUY",
  reply="Fixed in v2 — carer's leave clause added at 15.1."
)
```

---

#### `docs_delete_comment(doc_id, comment_id)`
Permanently delete a comment. Only the comment's author can delete it.

```
docs_delete_comment(doc_id="...", comment_id="AAAB1iPyaUY")
```

---

## Typical workflows

### Contract review
```
1. docs_get          — read the document
2. docs_add_comment  — flag issues with anchor_text pointing to specific clauses
3. docs_read_comments — audit what's been flagged
4. docs_search_replace — fix straightforward issues directly
5. docs_resolve_comment — close comments as they're addressed
```

### Bulk document update
```
1. docs_list         — find all relevant documents
2. docs_batch_replace — apply changes atomically (e.g. rebrand, date update)
3. docs_get          — verify the result
```

### Reviewing a Word contract
```
1. docs_convert_to_google_doc — a Google Doc copy, with their tracked changes as suggestions and their comments as comments
2. docs_list_suggestions      — see their changes; docs_review_suggestions accepts or rejects each
3. docs_read_comments         — docs_reply_to_comment / docs_resolve_comment to answer them
4. docs_search_replace (suggest=true) — propose your own changes
```

### Redlining (propose changes, don't make them)
```
1. docs_get              — read the document
2. docs_search_replace / docs_insert_after / ... with suggest=true
3. docs_list_suggestions — check what was proposed
4. (the owner accepts or rejects in Docs — or docs_review_suggestions does it)
```

### Collaborative review
```
1. docs_add_comment  — add review notes
2. docs_reply_to_comment — respond to collaborator comments
3. docs_resolve_comment  — close resolved threads
```

---

## Auth environment variables

| Variable | Description |
|----------|-------------|
| `GOOGLE_DOCS_MCP_TOKEN` | Path to token file (preferred for standalone use) |
| `GOOGLE_DOCS_MCP_CLIENT_ID` | Optional OAuth client ID override |
| `GOOGLE_DOCS_MCP_CLIENT_SECRET` | Optional OAuth client secret override |
| `GOOGLE_DOCS_TOKEN_FILE` | Legacy alias |
| `GOG_KEYRING_PASSWORD` | Auto-export from gog CLI (for personal/OpenClaw use) |

Legacy `GOOGLE_DRIVE_MCP_*` env var aliases still work for backward compatibility.

---

## Scopes

The auth setup requests these scopes:

| Scope | Purpose |
|-------|---------|
| `https://www.googleapis.com/auth/documents` | Read and write Google Docs |
| `https://www.googleapis.com/auth/drive` | Access Drive files (PDF reading, listing docs) |
| `https://www.googleapis.com/auth/drive.readonly` | Read Drive file metadata |
| `https://www.googleapis.com/auth/drive.file` | Per-file Drive access |
| `openid`, `email`, `profile` | Identity |

---

## License

MIT
