# google-drive-mcp

> Surgical Google Docs editing for AI agents — search/replace, comments, and full Drive access without character indices.

An MCP server that makes Google Docs actually usable for LLMs. Preserves document history, never touches character indices, and supports proper inline comments via Apps Script.

## Why

The Google Docs API locates text by **character index**. Every insert or delete shifts all subsequent indices. LLMs can't reliably count characters. The result: every integration ends up doing "delete everything, rewrite" — destroying version history, comments, and collaborator attribution.

This server uses the same abstraction as Claude Code for file editing: **search by text, not position**. You describe what you want to change. The server finds where it is.

## Tools

| Tool | Description |
|------|-------------|
| `docs_get` | Read doc as structured paragraphs + plain text |
| `docs_search_replace` | Find and replace (targeted occurrence or replace-all) |
| `docs_insert_after` | Insert paragraph after anchor text |
| `docs_insert_before` | Insert paragraph before anchor text |
| `docs_delete_paragraph` | Delete paragraphs matching anchor text |
| `docs_append` | Append paragraph at end |
| `docs_batch_replace` | Multiple replacements in one atomic call |
| `docs_add_comment` | Add comment anchored to specific text |
| `docs_read_comments` | List all comments with anchor/resolved status |
| `docs_reply_to_comment` | Reply to an existing comment |
| `docs_resolve_comment` | Resolve a comment (optionally with a reply) |
| `docs_delete_comment` | Delete a comment |
| `docs_list` | List recent docs (optional search query) |
| `docs_create` | Create a new document |

## Setup

### 1. Install

```bash
pip install google-drive-mcp
```

Or from source:

```bash
git clone https://github.com/dbuxton/google-drive-mcp
cd google-drive-mcp
pip install -e .
```

### 2. Create Google Cloud credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Create a project (or use an existing one)
3. Enable these APIs:
   - **Google Docs API**
   - **Google Drive API**
   - **Google Apps Script API** (required for inline-anchored comments)
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
5. Application type: **Desktop App**
6. Download the JSON file

### 3. Authenticate

**Normal (browser opens automatically):**
```bash
google-drive-mcp-auth --credentials ~/credentials.json
```

**Headless / remote server (no browser on this machine):**
```bash
google-drive-mcp-auth --credentials ~/credentials.json --headless
# Prints a URL → open on any device → paste back the redirect URL
```

**Already have an auth code:**
```bash
google-drive-mcp-auth --credentials ~/credentials.json --code "4/0Afr..."
```

Token is saved to `~/.google-drive-mcp/token.json` by default.

### 4. Configure Claude Desktop (or any MCP client)

Add to your MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json` on Mac):

```json
{
  "mcpServers": {
    "google-drive": {
      "command": "google-drive-mcp",
      "env": {
        "GOOGLE_DRIVE_MCP_TOKEN": "/Users/you/.google-drive-mcp/token.json"
      }
    }
  }
}
```

Or with `uvx` (no install needed):

```json
{
  "mcpServers": {
    "google-drive": {
      "command": "uvx",
      "args": ["google-drive-mcp"],
      "env": {
        "GOOGLE_DRIVE_MCP_TOKEN": "/Users/you/.google-drive-mcp/token.json"
      }
    }
  }
}
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `GOOGLE_DRIVE_MCP_TOKEN` | Path to token file (from `auth_setup.py`) |
| `GOOGLE_DOCS_TOKEN_FILE` | Legacy alias for `GOOGLE_DRIVE_MCP_TOKEN` |
| `GOG_KEYRING_PASSWORD` | If using [gog CLI](https://gogcli.sh) for auth (personal/dev use) |

## Comment anchoring

Comments created via `docs_add_comment` are stored in the Drive API and readable via `docs_read_comments`. However, due to a Drive API limitation, they show as "Original content deleted" in the Docs UI rather than as inline highlights.

**For true inline highlights**, the server uses Apps Script (`DocumentApp.addComment()`). This requires:
1. The Google Apps Script API is enabled in your Cloud project
2. The `script.projects` scope is included in your token (it is, if you used `auth_setup.py`)

The server automatically uses Apps Script when the scope is available.

## Development

```bash
git clone https://github.com/dbuxton/google-drive-mcp
cd google-drive-mcp
pip install -e ".[dev]"

# Run the server directly
python server.py

# Test auth
python auth_setup.py --credentials ~/credentials.json --headless
```

## License

MIT
