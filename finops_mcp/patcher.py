"""Safe, minimal-diff patching of Terraform/OpenTofu and Helm values files.

Safety prechecks enforced before any write:
- never touch secrets, networking blocks (VPC/subnet/SG), or state backends
- only modify allowlisted rightsizing attributes / YAML keys
- preserve formatting, comments, and surrounding blocks byte-for-byte
"""

from __future__ import annotations

import json
import re
from typing import Any

ALLOWED_ATTRS = {
    "instance_types",
    "instance_type",
    "instance_class",
    "min_size",
    "max_size",
    "desired_size",
    "allocated_storage",
    "node_count",
    "replicas",
    "cpu",
    "memory",
    "memory_size",
    "desired_count",
    "Schedule",  # scheduling tag (AWS Instance Scheduler-compatible)
}

# YAML leaf keys we may modify in Helm values files (pod/HPA rightsizing)
ALLOWED_YAML_KEYS = {
    "cpu",
    "memory",
    "minReplicas",
    "maxReplicas",
    "replicas",
    "replicaCount",
}

FORBIDDEN_PATTERNS = [
    r'\bbackend\s+"',            # state storage config
    r'resource\s+"aws_vpc"',
    r'resource\s+"aws_subnet"',
    r'resource\s+"aws_security_group"',
    r'resource\s+"aws_route_table"',
]

SECRET_HINTS = re.compile(
    r'(password|secret|token|api_?key|private_key|credentials)\s*[=:]', re.IGNORECASE
)


class SafetyViolation(Exception):
    pass


def _render_value(value: Any) -> str:
    """Render a Python value as an HCL literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_render_value(v) for v in value) + "]"
    return json.dumps(str(value))  # quoted string


def _line_of(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def check_line_safety(line: str) -> None:
    if SECRET_HINTS.search(line):
        raise SafetyViolation(f"refusing to modify a line that looks secret-bearing: {line.strip()!r}")


def patch_attribute(
    file_path: str,
    attr_or_var: str,
    new_value: Any,
    scope_header_re: str | None = None,
) -> dict[str, Any]:
    """Replace `attr_or_var = <old>` with the new value, minimal diff.

    scope_header_re optionally confines the substitution to a single HCL
    block (e.g. one resource) so same-named attributes elsewhere are untouched.
    Returns {"file", "line", "old", "new", "changed"}.
    """
    content = open(file_path, encoding="utf-8").read()

    # Determine searchable region
    region_start, region_end = 0, len(content)
    if scope_header_re:
        from .mapper import _find_block

        span = _find_block(content, scope_header_re)
        if not span:
            raise SafetyViolation(f"scoped block not found in {file_path}: {scope_header_re}")
        region_start, region_end = span

    region = content[region_start:region_end]
    m = re.search(
        rf'^(\s*){re.escape(attr_or_var)}(\s*=\s*)(.+?)(\s*(?:#.*)?)$',
        region,
        re.MULTILINE,
    )
    if not m:
        raise SafetyViolation(f"attribute '{attr_or_var}' not found in {file_path}")

    old_line = m.group(0)
    check_line_safety(old_line)

    # Ensure the matched line is not inside a forbidden block type
    abs_offset = region_start + m.start()
    prefix = content[:abs_offset]
    for pat in FORBIDDEN_PATTERNS:
        for bm in re.finditer(pat, prefix):
            from .mapper import _find_block

            span = _find_block(content[bm.start():], pat)
            if span and bm.start() <= abs_offset < bm.start() + span[1]:
                raise SafetyViolation(
                    f"edit target sits inside a protected block ({pat}) in {file_path}"
                )

    old_expr = m.group(3).strip()
    new_expr = _render_value(new_value)
    if old_expr == new_expr:
        return {
            "file": file_path,
            "line": _line_of(content, abs_offset),
            "old": old_expr,
            "new": new_expr,
            "changed": False,
        }

    new_line = f"{m.group(1)}{attr_or_var}{m.group(2)}{new_expr}{m.group(4)}"
    new_content = content[: region_start + m.start()] + new_line + content[region_start + m.end():]

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return {
        "file": file_path,
        "line": _line_of(content, abs_offset),
        "old": old_expr,
        "new": new_expr,
        "changed": True,
    }


def upsert_schedule_tag(
    file_path: str,
    scope_header_re: str,
    tag_value: str,
) -> dict[str, Any]:
    """Insert (or update) `Schedule = "<value>"` inside the `tags` map of one
    resource block. Additive, minimal diff. Requires an existing tags map -
    creating whole new blocks is out of scope for safe automation.
    """
    content = open(file_path, encoding="utf-8").read()

    from .mapper import _find_block

    span = _find_block(content, scope_header_re)
    if not span:
        raise SafetyViolation(f"scoped block not found in {file_path}: {scope_header_re}")
    block = content[span[0]: span[1]]

    tm = re.search(r'^(\s*)tags\s*=?\s*\{', block, re.MULTILINE)
    if not tm:
        raise SafetyViolation(
            f"no tags map found in target resource in {file_path}; "
            "add a tags block first so the Schedule tag can be managed"
        )

    # If a Schedule tag already exists in this block, update it in place.
    existing = re.search(
        r'^(\s*)(Schedule)(\s*=\s*)(.+?)(\s*(?:#.*)?)$', block, re.MULTILINE
    )
    if existing:
        old = existing.group(4).strip()
        new = f'"{tag_value}"'
        if old == new:
            return {"file": file_path, "line": _line_of(content, span[0] + existing.start()),
                    "old": old, "new": new, "changed": False}
        new_line = f"{existing.group(1)}Schedule{existing.group(3)}{new}{existing.group(5)}"
        new_block = block[: existing.start()] + new_line + block[existing.end():]
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content[: span[0]] + new_block + content[span[1]:])
        return {"file": file_path, "line": _line_of(content, span[0] + existing.start()),
                "old": old, "new": new, "changed": True}

    # Insert right after the tags map opening brace, matching inner indentation.
    brace_end = tm.end()  # position just after '{' within block
    rest = block[brace_end:]
    indent_m = re.search(r'\n(\s*)\S', rest)
    inner_indent = indent_m.group(1) if indent_m else tm.group(1) + "  "
    insertion = f'\n{inner_indent}Schedule = "{tag_value}"'
    new_block = block[:brace_end] + insertion + block[brace_end:]

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content[: span[0]] + new_block + content[span[1]:])
    return {
        "file": file_path,
        "line": _line_of(content, span[0] + brace_end) + 1,
        "old": "(no Schedule tag)",
        "new": f'"{tag_value}"',
        "changed": True,
    }


def patch_yaml_path(file_path: str, dotted_path: str, new_value: Any) -> dict[str, Any]:
    """Replace the scalar at a dotted YAML path (e.g. resources.requests.cpu)
    with new_value, minimal diff, preserving indentation and comments.

    Only allowlisted leaf keys may be modified; secret-looking lines refuse.
    """
    leaf = dotted_path.split(".")[-1]
    if leaf not in ALLOWED_YAML_KEYS:
        raise SafetyViolation(f"yaml key '{leaf}' is not an allowlisted rightsizing key")

    lines = open(file_path, encoding="utf-8").read().splitlines(keepends=True)
    stack: list[tuple[int, str]] = []  # (indent, key)

    for i, line in enumerate(lines):
        m = re.match(r'^(\s*)([A-Za-z0-9_.-]+):(\s*)(.*?)(\s*(?:#.*)?)$', line.rstrip("\n"))
        if not m:
            continue
        indent = len(m.group(1))
        key = m.group(2)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        path = ".".join(k for _, k in stack)

        if path == dotted_path:
            value = m.group(4)
            if value == "":
                raise SafetyViolation(
                    f"'{dotted_path}' in {file_path} is a mapping, not a scalar"
                )
            check_line_safety(line)
            new_scalar = str(new_value)
            if value.strip() == new_scalar:
                return {"file": file_path, "line": i + 1, "old": value.strip(),
                        "new": new_scalar, "changed": False}
            eol = "\n" if line.endswith("\n") else ""
            lines[i] = f"{m.group(1)}{key}:{m.group(3) or ' '}{new_scalar}{m.group(5)}{eol}"
            with open(file_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return {"file": file_path, "line": i + 1, "old": value.strip(),
                    "new": new_scalar, "changed": True}

    raise SafetyViolation(f"yaml path '{dotted_path}' not found in {file_path}")


def apply_recommendation(mapping: dict[str, Any], rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply every recommended attribute change through its mapped location."""
    if rec.get("externally_managed"):
        raise SafetyViolation(
            f"{rec['resource_name']}: lifecycle managed externally (e.g. Karpenter); "
            "refusing to patch static definitions"
        )
    if not mapping.get("found"):
        raise SafetyViolation(mapping.get("error", "mapping failed"))

    changes: list[dict[str, Any]] = []

    # --- Helm values.yaml (K8s pod/HPA rightsizing) ---
    if mapping.get("kind") == "helm_values":
        for yaml_path, new_value in rec["recommended"].items():
            if new_value is None or rec["current"].get(yaml_path) == new_value:
                continue
            changes.append(patch_yaml_path(mapping["values_file"], yaml_path, new_value))
        return changes

    # --- Terraform ---
    # recommendation key -> terraform attribute name
    key_translation = {
        "instance_type": ["instance_types", "instance_type"],
        "instance_class": ["instance_class"],
        "min_size": ["min_size"],
        "max_size": ["max_size"],
        "desired_size": ["desired_size"],
        "allocated_storage_gb": ["allocated_storage"],
        "cpu": ["cpu"],
        "memory": ["memory"],
        "memory_size": ["memory_size"],
        "desired_count": ["desired_count"],
    }

    attrs = mapping["attributes"]
    for rec_key, new_value in rec["recommended"].items():
        if new_value is None or rec["current"].get(rec_key) == new_value:
            continue
        tf_attr = next((a for a in key_translation.get(rec_key, []) if a in attrs), None)
        if not tf_attr:
            continue
        loc = attrs[tf_attr]

        # instance_types on EKS nodegroups is a list
        value: Any = [new_value] if tf_attr == "instance_types" else new_value

        if loc["location"] == "inline":
            tf_type = loc.get("terraform_type", mapping["terraform_type"])
            tf_name = loc.get("terraform_name", mapping["terraform_name"])
            scope = rf'resource\s+"{tf_type}"\s+"{re.escape(tf_name)}"\s*'
            changes.append(patch_attribute(loc["file"], tf_attr, value, scope_header_re=scope))
        elif loc["location"] == "tfvars":
            changes.append(patch_attribute(loc["file"], loc["variable"], value))
        elif loc["location"] == "variable_default":
            scope = rf'variable\s+"{re.escape(loc["variable"])}"\s*'
            changes.append(patch_attribute(loc["file"], "default", value, scope_header_re=scope))

    return changes
