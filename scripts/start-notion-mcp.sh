#!/bin/sh
# Sources .env from the repo root (if present) before launching the Notion MCP server.
# This makes NOTION_TOKEN available without requiring it to be pre-exported in the shell.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi
exec npx -y @notionhq/notion-mcp-server
