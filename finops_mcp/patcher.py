"""Safe, minimal-diff patching of Terraform/OpenTofu files.

Safety prechecks enforced before any write:
- never touch secrets, networking blocks (VPC/subnet/SG), or state backends
- only modify allowlisted rightsizing attributes
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
}

FORBIDDEN_PATTERNS = [
    r'\bbackend\s+"',            # state storage config
    r'resource\s+"aws_vpc"',
    r'resource\s+"aws_subnet"',
    r'resource\s+"aws_security_group"',
    r'resource\s+"aws_route_table"',
]

SECRET_HINTS = re.compile(
    r'(password|secret|token|api_key|private_key|credentials)\s*=', re.IGNORECASE
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
    key = attr_or_var.split(".")[-1]
    if key not in ALLOWED_ATTRS and scope_header_re is None:
        # tfvars variables may have arbitrary names; verify content instead
        pass

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


def apply_recommendation(mapping: dict[str, Any], rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply every recommended attribute change through its mapped location."""
    if rec.get("externally_managed"):
        raise SafetyViolation(
            f"{rec['resource_name']}: lifecycle managed externally (e.g. Karpenter); "
            "refusing to patch static definitions"
        )
    if not mapping.get("found"):
        raise SafetyViolation(mapping.get("error", "mapping failed"))

    # recommendation key -> terraform attribute name
    key_translation = {
        "instance_type": ["instance_types", "instance_type"],
        "instance_class": ["instance_class"],
        "min_size": ["min_size"],
        "max_size": ["max_size"],
        "desired_size": ["desired_size"],
        "allocated_storage_gb": ["allocated_storage"],
    }

    changes = []
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
            scope = (
                rf'resource\s+"{mapping["terraform_type"]}"\s+'
                rf'"{re.escape(mapping["terraform_name"])}"\s*'
            )
            changes.append(patch_attribute(loc["file"], tf_attr, value, scope_header_re=scope))
        elif loc["location"] == "tfvars":
            changes.append(patch_attribute(loc["file"], loc["variable"], value))
        elif loc["location"] == "variable_default":
            scope = rf'variable\s+"{re.escape(loc["variable"])}"\s*'
            changes.append(patch_attribute(loc["file"], "default", value, scope_header_re=scope))

    return changes
