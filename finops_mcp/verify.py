"""Local verification of modified infrastructure code.

Preference order:
1. `terraform validate` / `terraform plan` (or `tofu`) when the binary exists
2. HCL2 parse check of every modified file (always runs)

Also verifies the git diff touches only intended attributes (drift guard).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

from .patcher import ALLOWED_ATTRS, ALLOWED_YAML_KEYS


def _tf_binary() -> str | None:
    for b in ("terraform", "tofu"):
        if shutil.which(b):
            return b
    return None


def lint_hcl_files(files: list[str]) -> dict[str, Any]:
    """Parse-lint modified files: HCL2 for *.tf/*.tfvars, YAML for *.yaml/yml."""
    import hcl2
    import yaml

    results = {}
    ok = True
    for path in sorted(set(files)):
        try:
            if path.endswith((".yaml", ".yml")):
                with open(path, encoding="utf-8") as f:
                    yaml.safe_load(f)
            else:
                with open(path, encoding="utf-8") as f:
                    hcl2.load(f)
            results[path] = "ok"
        except Exception as e:
            results[path] = f"parse error: {e}"
            ok = False
    return {"passed": ok, "files": results}


def run_terraform_check(repo_path: str, plan: bool = False) -> dict[str, Any]:
    binary = _tf_binary()
    if not binary:
        return {"available": False, "note": "terraform/tofu binary not found; HCL lint used instead"}

    steps = {}
    ok = True
    for args in (["init", "-backend=false", "-input=false", "-no-color"],
                 ["validate", "-no-color"],
                 *([["plan", "-input=false", "-no-color", "-lock=false"]] if plan else [])):
        proc = subprocess.run(
            [binary, *args], cwd=repo_path, capture_output=True, text=True, timeout=300
        )
        steps[args[0]] = {
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }
        if proc.returncode != 0:
            ok = False
            break
    return {"available": True, "passed": ok, "binary": binary, "steps": steps}


def check_diff_scope(repo_path: str) -> dict[str, Any]:
    """Drift guard: every changed line in the working tree must touch only
    allowlisted rightsizing attributes or tfvars variable assignments."""
    proc = subprocess.run(
        ["git", "diff", "--unified=0", "--no-color"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {"passed": False, "error": proc.stderr.strip() or "git diff failed"}

    # Collect changed lines per file, then cancel identical added/removed
    # pairs: git's hunk alignment can render adjacent edits as a "move" of an
    # untouched line (e.g. a YAML mapping header), which is not a real change.
    changed: list[tuple[str, str, str]] = []  # (file, sign, body)
    current_file = None
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if not (line.startswith("+") or line.startswith("-")) or line.startswith(("+++", "---")):
            continue
        changed.append((current_file or "", line[0], line[1:]))

    from collections import Counter

    added = Counter((f, b) for f, s, b in changed if s == "+")
    removed = Counter((f, b) for f, s, b in changed if s == "-")
    moved = added & removed  # multiset intersection = pure moves

    # net changes: subtract moved counts from each side
    net = []
    seen_add: Counter = Counter()
    seen_rem: Counter = Counter()
    for f, s, b in changed:
        key = (f, b)
        if s == "+":
            seen_add[key] += 1
            if seen_add[key] <= moved.get(key, 0):
                continue
        else:
            seen_rem[key] += 1
            if seen_rem[key] <= moved.get(key, 0):
                continue
        net.append((f, s, b))

    violations = []
    for current_file, sign, raw_body in net:
        line = sign + raw_body
        body = raw_body.strip()
        if not body or body.startswith("#"):
            continue

        # YAML values files: changed lines must be allowlisted rightsizing keys
        if current_file and current_file.endswith((".yaml", ".yml")):
            ym = re.match(r'^([A-Za-z0-9_.-]+)\s*:', body)
            if not ym or ym.group(1) not in ALLOWED_YAML_KEYS:
                violations.append({"file": current_file, "line": line})
            continue

        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=', body)
        if not m:
            violations.append({"file": current_file, "line": line})
            continue
        key = m.group(1)
        is_tfvars = bool(current_file and current_file.endswith(".tfvars"))
        if key not in ALLOWED_ATTRS and key != "default" and not is_tfvars:
            violations.append({"file": current_file, "line": line})

    return {"passed": not violations, "violations": violations, "diff": proc.stdout[-8000:]}


def verify(repo_path: str, modified_files: list[str], plan: bool = False) -> dict[str, Any]:
    lint = lint_hcl_files(modified_files)
    tf = run_terraform_check(repo_path, plan=plan)
    scope = check_diff_scope(repo_path)
    passed = lint["passed"] and scope["passed"] and (tf.get("passed", True) if tf.get("available") else True)
    return {"passed": passed, "hcl_lint": lint, "terraform": tf, "diff_scope": scope}
