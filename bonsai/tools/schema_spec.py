"""Tool specs — loaded from tools/schema.json at the project root.

The schema.json file is the single source of truth for tool definitions
(name, description, input_schema). This module just reads it and wraps
entries as ToolSpec instances.
"""

from __future__ import annotations

from pathlib import Path

import orjson

from ..core.types import ToolSpec

# Tools available in Sprint 1 (subset of the full 9-tool schema).
SPRINT1_TOOLS = {"file_read", "file_write", "code_run", "memory_search", "ask_user"}
SPRINT2_TOOLS = SPRINT1_TOOLS | {"skill_lookup"}
BROWSER_TOOLS = {"web_scan", "web_execute_js", "web_click", "web_type",
                 "web_scroll", "web_navigate"}
ALL_TOOLS = SPRINT2_TOOLS | BROWSER_TOOLS
# memory_recall is added at runtime too; not in schema.json, so we inline it.
MEMORY_RECALL_SPEC = ToolSpec(
    name="memory_recall",
    description=("列出某 wing / room 里最近的 drawer (非搜索,无 query)。"
                 "用于回忆上次聊天 / 最近工作。"),
    input_schema={
        "type": "object",
        "properties": {
            "wing": {"type": "string", "description": "项目/人名过滤"},
            "room": {"type": "string", "description": "日期/话题过滤"},
            "limit": {"type": "integer", "default": 5},
        },
    },
)


def load_tool_specs(schema_path: Path, *, names: set[str] | None = None,
                    include_memory_recall: bool = False) -> list[ToolSpec]:
    data = orjson.loads(schema_path.read_bytes())
    names = names or SPRINT1_TOOLS
    specs: list[ToolSpec] = []
    for entry in data:
        if entry["name"] not in names:
            continue
        specs.append(ToolSpec(
            name=entry["name"],
            description=entry["description"],
            input_schema=entry["input_schema"],
        ))
    if include_memory_recall:
        specs.append(MEMORY_RECALL_SPEC)
    return specs
