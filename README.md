# google-docs-mcp

> Surgical Google Docs editing for AI agents — preserves history, never touches indices.

An MCP server that makes Google Docs editing actually usable for LLMs.

## The problem

The Google Docs API uses **character indices** to locate text. Every insert or delete shifts all subsequent indices. LLMs are terrible at counting characters. The result: everyone ends up using "delete everything, rewrite" — which destroys:
- Version history
- Comments
- Suggestions and review threads
- Collaborator attribution

## The solution

Same abstraction Claude Code uses for file editing: **search by text, not by position**.

You describe *what* you want to change. The server finds *where* it is, figures out the exact indices, applies the change with a real `batchUpdate`, and returns confirmation. Index arithmetic lives in the server, not the model's context window.

```
"Replace 'Q1' with 'Q2' in the board deck"
→ docs_search_replace({doc_id, find: "Q1", replace: "Q2"})
→ Finds all instances, replaces the first one, preserves history ✓
```

## Tools

| Tool | Description |
|------|-------------|
| `docs_get` | Read doc as structured paragraphs + plain text |
| `docs_search_replace` | Find and replace (occurrence-targeted or replace-all) |
| `docs_insert_after` | Insert new paragraph after anchor text |
| `docs_insert_before` | Insert new paragraph before anchor text |
| `docs_delete_paragraph` | Delete paragraphs matching anchor text |
| `docs_append` | Append paragraph at end of document |
| `docs_batch_replace` | Multiple replacements in one atomic call |
| `docs_list` | List recent docs (optional search query) |
| `docs_create` | Create a new document |

## Setup

### 1. Install

```bash
git clone https://github.com/dbuxton/google-docs-mcp
cd google-docs-mcp
pip install -r requirements.txt
```

### 2. Auth

**Option A: Via gog CLI** (recommended for personal use)

```bash
# Install gog: https://github.com/itchyny/gog (or your system's gog package)
gog login your@email.com

# Export your token (one-time)
export GOG_KEYRING_PASSWORD=your_keyring_password  # if using file keyring
gog auth tokens export your@email.com --out ~/.google_docs_token.json

# Point the server at it
export GOOGLE_DOCS_TOKEN_FILE=~/.google_docs_token.json
```

**Option B: Direct OAuth credentials**

Get credentials from [Google Cloud Console](https://console.cloud.google.com/):
1. Create a project, enable the Google Docs API and Google Drive API
2. Create OAuth 2.0 credentials (Desktop app type)
3. Download the credentials JSON

```bash
# Run the auth flow (opens browser)
python3 auth_setup.py --credentials ~/credentials.json --out ~/.google_docs_token.json
```

Then set:
```bash
export GOOGLE_DOCS_TOKEN_FILE=~/.google_docs_token.json
# Also set GOG_CREDENTIALS_PATH or the server uses /config/gogcli/credentials.json by default
export GOOGLE_DOCS_CREDENTIALS_PATH=~/credentials.json
```

### 3. Configure Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "google-docs": {
      "command": "python3",
      "args": ["/path/to/google-docs-mcp/server.py"],
      "env": {
        "GOOGLE_DOCS_TOKEN_FILE": "/path/to/google_docs_token.json"
      }
    }
  }
}
```

### 4. Configure OpenClaw

Add to your OpenClaw config:

```json
{
  "mcp": {
    "servers": {
      "google-docs": {
        "command": "python3",
        "args": ["/path/to/google-docs-mcp/server.py"],
        "env": {
          "GOG_KEYRING_PASSWORD": "${GOG_KEYRING_PASSWORD}"
        }
      }
    }
  }
}
```

## Usage examples

```
# Read a document
docs_get({doc_id: "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"})

# Fix a typo
docs_search_replace({
  doc_id: "...",
  find: "recieve",
  replace: "receive"
})

# Update all Q1 references
docs_search_replace({
  doc_id: "...",
  find: "Q1 2025",
  replace: "Q2 2025",
  occurrence: 0  // replace all
})

# Add a section after the introduction
docs_insert_after({
  doc_id: "...",
  anchor: "Introduction",
  text: "This section covers the background and motivation."
})

# Update multiple things at once (atomic)
docs_batch_replace({
  doc_id: "...",
  replacements_json: '[
    {"find": "DRAFT", "replace": "FINAL", "occurrence": 0},
    {"find": "TBD", "replace": "March 15, 2026"},
    {"find": "John Smith", "replace": "Jane Doe"}
  ]'
})
```

## Why not just use the existing Google Docs MCP servers?

Most existing implementations only expose `list`, `get`, and `create`. The few that support editing use `replaceAllText` (which only does replace-all) or write Python code that the model must generate with exact character positions — which is fragile and error-prone.

This server:
- Supports **targeted occurrence replacement** (1st, 2nd, 3rd...)
- Supports **regex search and replace**
- Supports **anchor-based paragraph insertion** (before/after)
- Supports **atomic batch operations** (multiple edits in one API call)
- Always uses real `batchUpdate` — **never deletes and rewrites**

## Architecture

```
Claude/Agent
     │
     │ MCP (stdio)
     ▼
server.py          ← FastMCP tool definitions
     │
     ▼
docs_edit.py       ← Core logic: auth, index math, batchUpdate calls
     │
     ▼
Google Docs API
```

The `docs_edit.py` module is also usable as a standalone CLI or Python library (no MCP required). See the [docs-edit OpenClaw skill](https://clawhub.com) for the skill version.

## Requirements

- Python 3.10+
- `google-api-python-client`
- `google-auth`
- `fastmcp`

See `requirements.txt` for pinned versions.

## License

MIT
