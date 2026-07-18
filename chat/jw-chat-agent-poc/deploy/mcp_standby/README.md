# MCP standby stack

This directory is the canonical, secret-free definition of the four independent MCP standby services used by chat.

The renderer reads the current `code-serving-112`, `127`, `190`, and `196` Deployment specs at execution time. It preserves their image, resources, environment, and secret references without serializing literal runtime values into Git. It then removes server metadata and ownership, forces one replica, replaces selectors with `mcp-*-standby` labels, and mounts the tracked UTF-8 relay.

## Render and apply

Run from an ops host with the `llmops` kubectl context:

```bash
uv run render_standby.py verify-live
uv run render_standby.py dry-run-server
uv run render_standby.py apply
uv run render_standby.py wire-chat
```

`wire-chat` changes only the four MCP endpoint variables and keeps `CHAT_EXTERNAL_TOOL_AGENT_ENABLED=true`. It never changes the chat image.

There is intentionally no manifest-output mode. Source Deployments can contain literal runtime configuration, so `apply` keeps the rendered objects in memory and passes them directly to `kubectl apply -f -` without writing or printing them.

The direct standby route is `/json`. The gateway resource route `/mcp/{resource}/mcp` and the direct code-serving route are different contracts.

## Safety boundary

- Original `code-serving-*` Deployments, Temporal resources, and scaler resources are read-only inputs.
- Standby names and labels do not contain `code-serving`, so the platform scaler does not own them.
- Literal runtime values are never written by this renderer; they move only through the in-memory kubectl pipeline.
- The renderer refuses to run when credential-shaped content is found in this tracked directory.
