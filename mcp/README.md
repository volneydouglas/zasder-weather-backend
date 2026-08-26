# Zasder Weather MCP server

Your backyard, readable by your AI assistant. This is a **read-only**
[MCP](https://modelcontextprotocol.io) server wrapping your own Zasder
Weather backend's API — ask Claude (or any MCP client) things like:

- "What did my station record during last night's storm?"
- "Compare today's high to the last week."
- "Which alerts fired while I was asleep?"
- "What does my barometer think the weather is doing?"

## Setup

```sh
pip install "mcp[cli]" httpx   # SDK 1.x and 2.x both work
export ZASDER_URL=https://your-app.fly.dev
export ZASDER_TOKEN=<your API token — a read-only share token works>
python zasder_mcp.py
```

Claude Desktop (`claude_desktop_config.json`):

```json
{ "mcpServers": { "zasder-weather": {
    "command": "python",
    "args": ["/path/to/zasder_mcp.py"],
    "env": { "ZASDER_URL": "https://your-app.fly.dev",
             "ZASDER_TOKEN": "..." } } } }
```

Claude Code: `claude mcp add zasder-weather -e ZASDER_URL=... -e
ZASDER_TOKEN=... -- python /path/to/zasder_mcp.py`

## Tools

`list_stations`, `current_conditions`, `derived_metrics`,
`history_summary`, `records`, `recent_alerts` — GET endpoints only, so
the assistant can read your weather and change nothing. Use a
**read-only share token** if you want that guarantee enforced
server-side too.

## Grafana, while you're here

The same API feeds Grafana's
[Infinity datasource](https://grafana.com/grafana/plugins/yesoreyeram-infinity-datasource/)
directly — point a JSON query at
`/api/devices/<mac>/history?hours=168` with the header
`Authorization: Bearer <token>`, parse `rows`, and chart any column.
A Prometheus-style scrape of current readings is at `/metrics`.
