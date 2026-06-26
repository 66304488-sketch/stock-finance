"""Helpers for local MCP settings and Anthropic MCP connector requests."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

MCP_TOKEN_MASK = "••••••••"
MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LOCAL_MCP_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_local_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return host in LOCAL_MCP_HOSTS


def validate_mcp_server(server: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a user-supplied MCP server config."""
    name = str(server.get("name") or "").strip()
    url = str(server.get("url") or "").strip()
    token = str(server.get("authorization_token") or "").strip()

    if not MCP_NAME_RE.match(name):
        raise ValueError("MCP server name must be 1-64 letters, numbers, underscores, or hyphens")

    parsed = urlparse(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and _is_local_url(url)):
        raise ValueError("MCP server URL must be https://, or http://localhost for local clients")
    if not parsed.netloc:
        raise ValueError("MCP server URL is invalid")

    return {"name": name, "url": url, "authorization_token": token}


def merge_mcp_servers(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate incoming servers and preserve masked tokens from existing config."""
    existing_by_key = {
        (str(s.get("name") or ""), str(s.get("url") or "")): str(s.get("authorization_token") or "")
        for s in existing
    }
    result = []
    seen = set()
    for raw in incoming:
        server = validate_mcp_server(raw)
        key = (server["name"], server["url"])
        if key in seen:
            raise ValueError(f"Duplicate MCP server: {server['name']}")
        seen.add(key)
        if server["authorization_token"] == MCP_TOKEN_MASK:
            server["authorization_token"] = existing_by_key.get(key, "")
        result.append(server)
    return result


def sanitize_mcp_servers(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return MCP server settings safe for frontend display."""
    sanitized = []
    for server in servers:
        name = str(server.get("name") or "").strip()
        url = str(server.get("url") or "").strip()
        if not name or not url:
            continue
        sanitized.append({
            "name": name,
            "url": url,
            "authorization_configured": bool(server.get("authorization_token")),
            "is_local": _is_local_url(url),
        })
    return sanitized


def build_anthropic_mcp_parts(cfg: dict[str, Any]) -> dict[str, Any]:
    """Build Anthropic Messages API MCP connector params from saved config.

    Localhost servers are intentionally skipped: Anthropic's remote MCP connector
    cannot reach the user's machine. Local heatmap MCP is exposed for local MCP
    clients via mcp_heatmap_server.py instead.
    """
    if not cfg.get("mcp_enabled"):
        return {"betas": [], "mcp_servers": [], "tools": []}

    mcp_servers = []
    tools = []
    for server in cfg.get("mcp_servers") or []:
        try:
            normalized = validate_mcp_server(server)
        except ValueError:
            continue
        if _is_local_url(normalized["url"]):
            continue
        entry = {"type": "url", "name": normalized["name"], "url": normalized["url"]}
        if normalized.get("authorization_token"):
            entry["authorization_token"] = normalized["authorization_token"]
        mcp_servers.append(entry)
        tools.append({"type": "mcp_toolset", "mcp_server_name": normalized["name"]})

    if not mcp_servers:
        return {"betas": [], "mcp_servers": [], "tools": []}
    return {"betas": ["mcp-client-2025-11-20"], "mcp_servers": mcp_servers, "tools": tools}
