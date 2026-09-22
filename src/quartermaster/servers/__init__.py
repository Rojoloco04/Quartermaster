"""MCP servers the assistant reaches its integrations through.

Each runs as ``qm mcp <name>`` over stdio, so the Discord bot (via the Agent
SDK) and ``claude`` in a terminal (via the vault's .mcp.json) share one
implementation and the same tokens.
"""
