"""Accessibility tree extraction + LLM-friendly rendering.

Uses Accessibility.getFullAXTree which returns a flat list of nodes with
parent/child links. We walk interesting nodes (landmarks, controls,
text of sufficient length) and assign short IDs via the ElementPool.
"""

from __future__ import annotations

from .element_pool import ElementPool

# Roles we always want to expose to the LLM
ACTIONABLE_ROLES = {
    "button", "link", "textbox", "combobox", "checkbox", "radio",
    "switch", "option", "tab", "menuitem", "slider",
}
LANDMARK_ROLES = {
    "main", "navigation", "banner", "contentinfo", "complementary",
    "search", "form", "region", "dialog",
}
STRUCTURAL_ROLES = {
    "heading", "list", "listitem", "article", "table", "row", "cell",
    "group", "paragraph",
}


def ax_tree_to_text(nodes: list[dict], pool: ElementPool,
                    *, scope_id: str | None = None,
                    max_nodes: int = 400) -> str:
    """Render the AX tree in the compressed format documented in TOOLS.md.

    Example output:
      [a1] main
        [a2] heading "搜索结果"
        [a3] listbox "排序"
          [a4] option "价格升序" selected
    """
    by_id = {n["nodeId"]: n for n in nodes}
    children_of: dict[str, list[str]] = {}
    for n in nodes:
        for c in n.get("childIds", []) or []:
            children_of.setdefault(n["nodeId"], []).append(c)

    # Find root: if a scope is given, render that subtree; else start at the
    # document root (the node with role=RootWebArea).
    if scope_id:
        root = by_id.get(scope_id)
        if not root:
            return f"[scope {scope_id} not found in current AX tree]"
        roots = [root["nodeId"]]
    else:
        roots = [n["nodeId"] for n in nodes
                 if _role(n) in ("RootWebArea", "WebArea")]
        if not roots:
            roots = [nodes[0]["nodeId"]] if nodes else []

    lines: list[str] = []
    counter = [0]

    def walk(node_id: str, depth: int) -> None:
        if counter[0] >= max_nodes:
            return
        node = by_id.get(node_id)
        if not node or node.get("ignored"):
            for c in children_of.get(node_id, []):
                walk(c, depth)
            return

        role = _role(node)
        name = _name(node)
        # Skip nodes that add no info (no role AND no name).
        if not role and not name:
            for c in children_of.get(node_id, []):
                walk(c, depth)
            return

        interesting = (
            role in ACTIONABLE_ROLES or role in LANDMARK_ROLES
            or role in STRUCTURAL_ROLES or (name and len(name) >= 3)
        )
        if interesting:
            counter[0] += 1
            sid = pool.assign(
                ax_node_id=node["nodeId"],
                backend_node_id=node.get("backendDOMNodeId"),
                role=role,
                name=name,
            )
            state = _state_flags(node)
            name_part = f' "{name}"' if name else ""
            state_part = f" {state}" if state else ""
            lines.append(f"{'  ' * depth}[{sid}] {role}{name_part}{state_part}")

        for c in children_of.get(node_id, []):
            walk(c, depth + 1 if interesting else depth)

    for rid in roots:
        walk(rid, 0)
    if counter[0] >= max_nodes:
        lines.append(f"... [truncated at {max_nodes} nodes — drill in via web_scan(scope=...)]")
    return "\n".join(lines) if lines else "(empty page)"


def _role(node: dict) -> str:
    r = node.get("role") or {}
    if isinstance(r, dict):
        return str(r.get("value") or "")
    return str(r or "")


def _name(node: dict) -> str:
    n = node.get("name") or {}
    if isinstance(n, dict):
        return str(n.get("value") or "").strip()
    return str(n or "").strip()


def _state_flags(node: dict) -> str:
    flags = []
    props = node.get("properties") or []
    for p in props:
        name = (p.get("name") or "").lower()
        val = p.get("value", {})
        if isinstance(val, dict):
            v = val.get("value")
        else:
            v = val
        if name in ("selected", "checked", "disabled", "expanded", "focused",
                    "required") and v:
            flags.append(name)
    # Convenience from top-level fields
    if (node.get("focused") or {}).get("value"):
        flags.append("focused")
    return " ".join(sorted(set(flags)))
