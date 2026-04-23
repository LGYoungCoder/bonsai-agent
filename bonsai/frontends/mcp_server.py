"""MCP server — expose Bonsai's tools to Claude Code / Cursor via stdio.

Each MCP call maps to a Handler dispatch (same code path as the CLI uses).
This lets external IDEs delegate personal-memory lookups and SOP retrieval
to the user's own Bonsai without a network hop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


async def run_stdio(cfg: Any, root: Path) -> None:
    """Run Bonsai as an MCP server over stdio."""
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import TextContent, Tool
    except ImportError:
        import sys
        print("[bonsai mcp] install with: pip install mcp", file=sys.stderr)
        raise

    from ..core.handler import Handler
    from ..core.session import Session
    from ..core.types import ToolCall
    from ..maintenance import start_maintenance
    from ..stores.embed import build_embedder
    from ..stores.evidence import EvidenceRecorder
    from ..stores.memory_store import MemoryStore
    from ..stores.skill_store import SkillStore
    from ..tools.schema_spec import ALL_TOOLS, load_tool_specs

    skill_store = SkillStore((root / cfg.memory.skill_dir.lstrip("./")).resolve())
    skill_store.init()
    memory_store = MemoryStore(
        (root / cfg.memory.memory_db.lstrip("./")).resolve(),
        embedder=build_embedder({
            "embed_provider": cfg.memory.embed_provider,
            "embed_model": cfg.memory.embed_model,
        }),
    )
    session = Session(cwd=root)
    # Evidence so MCP-driven work also feeds `bonsai distill`; maintenance
    # daemon so a long-lived MCP session gets the same 15d gc as other entries.
    evidence = EvidenceRecorder(skill_store.root, session_id=session.session_id)
    handler = Handler(session=session, memory_store=memory_store,
                      skill_store=skill_store, evidence=evidence,
                      browser_headless=True)
    start_maintenance(root, cfg)

    schema_path = root / "tools" / "schema.json"
    # ALL_TOOLS so external IDEs (Claude Code / Cursor) also see web_* etc.
    specs = load_tool_specs(schema_path, names=ALL_TOOLS) if schema_path.exists() else []

    server = Server("bonsai")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
            for s in specs
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        import uuid
        tc = ToolCall(id=f"mcp_{uuid.uuid4().hex[:8]}", name=name, args=arguments or {})
        outcome = await handler.dispatch(tc)
        return [TextContent(type="text", text=outcome.tool_result.content)]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
