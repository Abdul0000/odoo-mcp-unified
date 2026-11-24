"""Read-only SDK checks through the real HTTP/proxy path. Never prints tokens."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from urllib.parse import urlsplit

import httpx2
from dotenv import dotenv_values
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError


async def check(url, token, expected_commit=None, expected_ttl_ms=None):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use a plain MCP endpoint URL; credentials belong in headers")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Remote MCP checks require HTTPS")
    report = {"endpoint_host": parsed.hostname, "modes": {}}
    for mode in ("auto", "legacy"):
        seen_sse = []
        progress = []
        probe_times = {}

        async def response_hook(response):
            if "text/event-stream" in response.headers.get("content-type", ""):
                seen_sse.append(True)

        async def on_progress(value, total, message):
            progress.append(value)
            if message in {"transport.probe.started", "transport.probe.ready"}:
                probe_times[message] = time.monotonic()

        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token},
                                      event_hooks={"response": [response_hook]}) as http:
            started = time.monotonic()
            # Reconnect through a fresh SDK client: no transport session state
            # is assumed reusable after a close/redeploy.
            for iteration in range(2):
                async with Client(streamable_http_client(url, http_client=http), mode=mode, read_timeout_seconds=30) as client:
                    tools = await client.list_tools(cache_mode="refresh")
                    if not tools.tools:
                        raise RuntimeError("MCP catalog/cache contract failed")
                    if client.protocol_version >= "2026-07-28" and (
                        tools.cache_scope != "private" or tools.ttl_ms < 0
                        or (expected_ttl_ms is not None and tools.ttl_ms != expected_ttl_ms)
                    ):
                        raise RuntimeError("MCP cache hints differ from configuration")
                    health = await client.call_tool(
                        "connector_health", {}, progress_callback=on_progress,
                        meta={"io.odoo-mcp/progressProbe": True} if iteration == 0 else None,
                    )
                    body = health.structured_content or {}
                    if health.is_error or body.get("status") != "ok":
                        raise RuntimeError("MCP health tool failed")
                    actual_commit = (body.get("runtime") or {}).get("commit_sha")
                    if expected_commit and actual_commit != expected_commit:
                        raise RuntimeError("MCP runtime commit does not match deployed release")
                    if iteration == 0:
                        templates = await client.list_resource_templates()
                        if templates.resource_templates:
                            raise RuntimeError("Unexpected dynamic resource template catalog")
                        resource = await client.read_resource("odoo-mcp://instructions/client-check")
                        if not resource.contents or not resource.contents[0].text:
                            raise RuntimeError("MCP resource read failed")
                        try:
                            await client.read_resource("odoo-mcp://missing")
                        except MCPError as exc:
                            expected_error = -32602 if client.protocol_version >= "2026-07-28" else -32002
                            if exc.code != expected_error:
                                raise RuntimeError("Resource error code differs from negotiated protocol") from None
                        else:
                            raise RuntimeError("Unknown resource unexpectedly succeeded")
                    await asyncio.sleep(.05)
                if iteration == 0:
                    first = probe_times.get("transport.probe.started")
                    last = probe_times.get("transport.probe.ready")
                    if first is None or last is None or last - first < .2:
                        raise RuntimeError("Intermediate progress missing or buffered by proxy")
            if mode == "legacy" and not seen_sse:
                raise RuntimeError("Proxy did not deliver legacy SSE responses")
            report["modes"][mode] = {"reconnected": True, "sse_responses": len(seen_sse),
                                     "progress_events": len(progress), "progress_streaming": True, "elapsed_ms": round((time.monotonic() - started) * 1000)}
    async with httpx2.AsyncClient(headers={"X-API-Key": token}) as http:
        async with Client(streamable_http_client(url, http_client=http), read_timeout_seconds=30) as client:
            result = await client.call_tool("connector_health", {})
            if result.is_error:
                raise RuntimeError("API-key compatibility failed")
    report["api_key"] = "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--env-file")
    parser.add_argument("--expected-commit")
    args = parser.parse_args()
    config = {**(dotenv_values(args.env_file, interpolate=False) if args.env_file else {}), **os.environ}
    token = config.get("ODOO_AGENT_API_TOKEN")
    base = config.get("ODOO_AGENT_PUBLIC_BASE_URL") or config.get("ODOO_AGENT_OAUTH_ISSUER")
    url = args.url or (base.rstrip("/") + "/mcp" if base else None)
    if not token or not url:
        raise SystemExit("Configure ODOO_AGENT_API_TOKEN and an MCP endpoint URL")
    try:
        ttl_ms = int(config.get("ODOO_AGENT_MCP_CACHE_TTL_SECONDS", "60")) * 1000
        result = asyncio.run(check(url, token, args.expected_commit, ttl_ms))
    except Exception as exc:
        # Transport exceptions may contain URLs/headers: never echo their text.
        raise SystemExit("MCP_TRANSPORT_CHECK_FAILED type=" + type(exc).__name__) from None
    print(json.dumps({"status": "passed", **result}))


if __name__ == "__main__":
    main()
