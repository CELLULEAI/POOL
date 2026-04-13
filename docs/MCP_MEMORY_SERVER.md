# Cellule.ai MCP Memory Server — Configuration

## Claude Code (claude_code_config.json)

Add to `~/.claude/claude_code_config.json`:

```json
{
  "mcpServers": {
    "cellule-memory": {
      "command": "python",
      "args": ["-m", "iamine", "mcp-server", "--pool-url", "https://iamine.org", "--token", "YOUR_TOKEN"]
    }
  }
}
```

## Claude Desktop (claude_desktop_config.json)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "cellule-memory": {
      "command": "python",
      "args": ["-m", "iamine", "mcp-server", "--pool-url", "https://iamine.org", "--token", "YOUR_TOKEN"]
    }
  }
}
```

## Cursor (.cursor/mcp.json)

Add to `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "cellule-memory": {
      "command": "python",
      "args": ["-m", "iamine", "mcp-server", "--pool-url", "https://iamine.org", "--token", "YOUR_TOKEN"]
    }
  }
}
```

## Environment variables (alternative)

```bash
export IAMINE_POOL_URL=https://iamine.org
export IAMINE_TOKEN=acc_xxxxx
python -m iamine mcp-server
```

## Available MCP Tools

| Tool | Description |
|------|-------------|
| `memory_status` | Memory stats per tier |
| `memory_search` | Hybrid search across all tiers |
| `memory_observe` | Record an observation |
| `memory_episodes` | List session episodes |
| `memory_procedures` | List workflow patterns |
| `memory_graph` | Explore memory relationships |
| `memory_consolidate` | Force observation consolidation |
| `memory_forget_all` | RGPD purge all memory |

## Available MCP Resources

| URI | Description |
|-----|-------------|
| `memory://status` | Current statistics |
| `memory://episodes` | Recent episodes |
| `memory://procedures` | Active procedures |
