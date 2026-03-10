# google-drive-mcp

> Surgical Google Docs editing for AI agents — preserves history, never touches character indices.

An MCP server that makes Google Docs actually usable for LLMs. Standalone, no other tools required.

## Why

The Google Docs API uses **character indices** for every edit. LLMs are bad at counting characters. Everyone ends up deleting and rewriting entire documents, which destroys version history, comments, and collaborator attribution.

This server uses the same abstraction as code editors: **search by text, not by position**. You describe *what* to change, the server finds *where* it is and handles the index arithmetic.

## Setup

### 1. Install

```bash
pip install google-drive-mcp
```

Or from source:
```bash
git clone https://github.com/dbuxton/google-drive-mcp
cd google-drive-mcp
pip install -r requirements.txt
```

### 2. Google Cloud credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Create a project (or use an existing one)
3. Enable these APIs:
   - **Google Docs API**
   - **Google Drive API**
   - **Google Apps Script API** *(required for inline-anchored comments)*
4. Go to **APIs & Services → Credentials**
5. **Create credentials → OAuth 2.0 Client ID → Desktop App**
6. Download the JSON file

### 3. Authenticate

**Normal (browser on same machine):**
```bash
google-drive-mcp-auth --credentials ~/credentials.json
```

**Headless / remote server (no browser on device):**
```bash
google-drive-mcp-auth --credentials ~/credentials.json --headless
# Prints a URL → open on any device → paste back the redirect URL
```

**Already have an auth code:**
```bash
google-drive-mcp-auth --credentials ~/credentials.json --code "4/0Afr..."
```

Token is saved to `~/.google-drive-mcp/token.json` by default.

### 4. Add to your MCP config

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "google-drive": {
      "command": "python3",
      "args": ["/path/to/google-drive-mcp/server.py"],
      "env": {
        "GOOGLE_DRIVE_MCP_TOKEN": "/Users/you/.google-drive-mcp/token.json"
      }
    }
  }
}
```

**OpenClaw** (in gateway config):
```json
{
  "mcp": {
    "servers": {
      "google-drive": {
        "command": "python3",
        "args": ["/path/to/server.py"],
        "env": {
          "GOOGLE_DRIVE_MCP_TOKEN": "~/.google-drive-mcp/token.json"
        }
      }
    }
  }
}
```

---

## Tools

### Document editing

| Tool | Description |
|------|-------------|
| `docs_get` | Read doc structure as paragraphs + plain text |
| `docs_search_replace` | Find and replace (first, nth, or all occurrences) |
| `docs_insert_after` | Insert paragraph after anchor text |
| `docs_insert_before` | Insert paragraph before anchor text |
| `docs_delete_paragraph` | Delete paragraphs matching anchor text |
| `docs_append` | Append paragraph at end |
| `docs_batch_replace` | Multiple replacements in one atomic call |

### Comments

| Tool | Description |
|------|-------------|
| `docs_add_comment` | Add a comment anchored to specific text |
| `docs_read_comments` | List all comments with anchor/resolved status |
| `docs_reply_to_comment` | Reply to an existing comment |
| `docs_resolve_comment` | Resolve a comment (optionally with reply) |
| `docs_delete_comment` | Delete a comment |

### Document management

| Tool | Description |
|------|-------------|
| `docs_list` | List recent docs (optional search query) |
| `docs_create` | Create a new document |

---

## Examples

```python
# Replace text
docs_search_replace(doc_id="...", find="Q1 2024", replace="Q2 2024")

# Insert paragraph after a heading
docs_insert_after(doc_id="...", anchor="Executive Summary", text="Updated March 2026.")

# Replace all occurrences
docs_search_replace(doc_id="...", find="ACME Corp", replace="Initech", occurrence=0)

# Atomic multi-replace
docs_batch_replace(doc_id="...", replacements_json='[
  {"find": "draft", "replace": "final"},
  {"find": "[DATE]", "replace": "10 March 2026"}
]')

# Add a review comment
docs_add_comment(doc_id="...", anchor_text="clause 14.2", comment="Legal review needed here.")

# Resolve it
docs_resolve_comment(doc_id="...", comment_id="AAAB1...", reply="Fixed in v2.")
```

---

## Auth environment variables

| Variable | Description |
|----------|-------------|
| `GOOGLE_DRIVE_MCP_TOKEN` | Path to token file (preferred) |
| `GOOGLE_DOCS_TOKEN_FILE` | Legacy alias |
| `GOG_KEYRING_PASSWORD` | Auto-export from gog CLI (personal use) |

---

## Note on inline comment anchoring

`docs_add_comment` creates comments that appear in the **💬 comments panel** but currently show as *"Original content deleted"* rather than inline highlights. This is a Drive API limitation — the internal anchor link can only be established by the Docs UI or Apps Script.

To enable true inline comments, the Apps Script scope (`script.projects`) must be included during auth — which it is. Once the Apps Script execution path is implemented in the server, `docs_add_comment` will be upgraded to use `DocumentApp.addComment()` automatically.

---

## License

MIT
