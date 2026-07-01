"""Codebase mapping: locate where a recommended resource is declared.

Scans *.tf / *.tfvars / values.yaml files, matches resources by name, tags,
or AWS identifiers, and traces variable indirection to its source so patches
land on tfvars/defaults instead of inline module internals.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

SCAN_EXTENSIONS = (".tf", ".tfvars")
HELM_FILENAMES = ("values.yaml", "values.yml")
SKIP_DIRS = {".git", ".terraform", "node_modules", "__pycache__"}

# Attribute we patch, per resource type
TARGET_ATTRS = {
    "eks_nodegroup": ["instance_types", "min_size", "max_size", "desired_size"],
    "rds_instance": ["instance_class", "allocated_storage"],
}

RESOURCE_BLOCK_TYPES = {
    "eks_nodegroup": ["aws_eks_node_group"],
    "rds_instance": ["aws_db_instance", "aws_rds_cluster_instance"],
}


def _iter_files(repo_path: str) -> list[str]:
    found = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(SCAN_EXTENSIONS) or f in HELM_FILENAMES:
                found.append(os.path.join(root, f))
    return sorted(found)


def _find_block(content: str, block_header_re: str) -> Optional[tuple[int, int]]:
    """Return (start_offset, end_offset) of a brace-balanced HCL block whose
    header matches block_header_re (must end just before the opening brace)."""
    m = re.search(block_header_re, content)
    if not m:
        return None
    brace_start = content.find("{", m.end() - 1)
    if brace_start == -1:
        return None
    depth = 0
    for i in range(brace_start, len(content)):
        c = content[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return (m.start(), i + 1)
    return None


def _block_matches_resource(block_text: str, rec: dict[str, Any]) -> bool:
    """Check the block references the recommendation by name, tag, or ARN/id."""
    name = rec["resource_name"]
    candidates = [name, rec.get("resource_arn", "")]
    for c in candidates:
        if c and re.search(re.escape(c), block_text):
            return True
    return False


def _extract_attr(block_text: str, attr: str) -> Optional[str]:
    m = re.search(rf'^\s*{re.escape(attr)}\s*=\s*(.+?)\s*$', block_text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _trace_variable(repo_path: str, var_expr: str) -> Optional[dict[str, Any]]:
    """If an attribute value is `var.foo`, find where foo gets its value:
    terraform.tfvars first, then the variable's default. Returns the
    patchable location, or None if the expression isn't a simple variable."""
    m = re.match(r'^var\.([A-Za-z_][A-Za-z0-9_]*)$', var_expr)
    if not m:
        return None
    var_name = m.group(1)

    # 1) tfvars assignment wins
    for path in _iter_files(repo_path):
        if not path.endswith(".tfvars"):
            continue
        content = open(path, encoding="utf-8").read()
        vm = re.search(
            rf'^\s*{re.escape(var_name)}\s*=\s*(.+?)\s*$', content, re.MULTILINE
        )
        if vm:
            return {
                "kind": "tfvars",
                "file": path,
                "variable": var_name,
                "current_value": vm.group(1).strip(),
            }

    # 2) fall back to the variable block's default
    for path in _iter_files(repo_path):
        if not path.endswith(".tf"):
            continue
        content = open(path, encoding="utf-8").read()
        span = _find_block(content, rf'variable\s+"{re.escape(var_name)}"\s*')
        if span:
            block = content[span[0]: span[1]]
            default = _extract_attr(block, "default")
            if default is not None:
                return {
                    "kind": "variable_default",
                    "file": path,
                    "variable": var_name,
                    "current_value": default,
                }
    return None


def map_recommendation_to_code(repo_path: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Locate the declaration of the recommended resource and, per attribute,
    the exact file/expression to patch (following variable indirection)."""
    repo_path = os.path.abspath(repo_path)
    if not os.path.isdir(repo_path):
        return {"found": False, "error": f"repo path does not exist: {repo_path}"}

    block_types = RESOURCE_BLOCK_TYPES.get(rec["resource_type"], [])
    attrs = TARGET_ATTRS.get(rec["resource_type"], [])

    for path in _iter_files(repo_path):
        if not path.endswith(".tf"):
            continue
        content = open(path, encoding="utf-8").read()
        for btype in block_types:
            for header in re.finditer(
                rf'resource\s+"{btype}"\s+"([A-Za-z0-9_-]+)"\s*\{{', content
            ):
                span = _find_block(
                    content,
                    rf'resource\s+"{btype}"\s+"{re.escape(header.group(1))}"\s*',
                )
                if not span:
                    continue
                block_text = content[span[0]: span[1]]
                if not _block_matches_resource(block_text, rec):
                    continue

                attr_map = {}
                for attr in attrs:
                    raw_value = _extract_attr(block_text, attr)
                    if raw_value is None:
                        continue
                    entry: dict[str, Any] = {
                        "file": path,
                        "location": "inline",
                        "current_expression": raw_value,
                    }
                    traced = _trace_variable(repo_path, raw_value)
                    if traced:
                        entry.update(
                            {
                                "file": traced["file"],
                                "location": traced["kind"],
                                "variable": traced["variable"],
                                "current_expression": traced["current_value"],
                            }
                        )
                    attr_map[attr] = entry

                return {
                    "found": True,
                    "resource_type": rec["resource_type"],
                    "terraform_type": btype,
                    "terraform_name": header.group(1),
                    "declaration_file": path,
                    "attributes": attr_map,
                }

    return {
        "found": False,
        "error": (
            f"no declaration found for {rec['resource_type']} "
            f"'{rec['resource_name']}' in {repo_path}"
        ),
    }
