"""Git branch/commit management and structured PR body generation."""

from __future__ import annotations

import datetime
import os
import subprocess
from typing import Any


def _git(repo_path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo_path, capture_output=True, text=True)


def ensure_repo(repo_path: str) -> bool:
    return _git(repo_path, "rev-parse", "--is-inside-work-tree").returncode == 0


def create_branch_and_commit(
    repo_path: str,
    rec: dict[str, Any],
    changes: list[dict[str, Any]],
    push: bool = False,
    action: str = "rightsize",
) -> dict[str, Any]:
    if not ensure_repo(repo_path):
        return {"ok": False, "error": f"{repo_path} is not a git repository"}

    stamp = datetime.date.today().strftime("%Y%m%d")
    branch = f"finops/{action}-{rec['resource_name']}-{stamp}"
    title = f"chore(finops): {action} {rec['resource_name']} ({rec['resource_type']}) for cost optimization"

    r = _git(repo_path, "checkout", "-B", branch)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip()}

    files = sorted({c["file"] for c in changes if c.get("changed")})
    rel_files = [os.path.relpath(f, repo_path) for f in files]
    if not rel_files:
        return {"ok": False, "error": "no changed files to commit", "branch": branch}

    _git(repo_path, "add", *rel_files)
    r = _git(repo_path, "commit", "-m", title)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip() or r.stdout.strip(), "branch": branch}
    sha = _git(repo_path, "rev-parse", "--short", "HEAD").stdout.strip()

    pushed = False
    if push:
        p = _git(repo_path, "push", "-u", "origin", branch)
        pushed = p.returncode == 0

    return {"ok": True, "branch": branch, "commit": sha, "files": rel_files,
            "pushed": pushed, "title": title}


def _fmt_config(cfg: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in cfg.items() if v is not None)


def _fmt_metrics(metrics: dict[str, Any]) -> str:
    parts = []
    lookback = metrics.get("lookback_days", 14)
    for k, v in metrics.items():
        if k == "lookback_days":
            continue
        label = k.replace("_", " ")
        parts.append(f"{label} = {v}")
    return f"{'; '.join(parts)} over a {lookback}-day lookback"


def build_pr_body(
    rec: dict[str, Any],
    changes: list[dict[str, Any]],
    verification: dict[str, Any],
    action: str = "rightsize",
) -> str:
    title = f"chore(finops): {action} {rec['resource_name']} ({rec['resource_type']}) for cost optimization"

    change_lines = "\n".join(
        f"- `{os.path.basename(c['file'])}` line {c['line']}: `{c['old']}` → `{c['new']}`"
        for c in changes if c.get("changed")
    ) or "- (no file changes)"

    tf = verification.get("terraform", {})
    if tf.get("available"):
        plan_out = tf.get("steps", {}).get("plan", tf.get("steps", {}).get("validate", {}))
        verif_snippet = plan_out.get("stdout", "").strip() or plan_out.get("stderr", "").strip()
        verif_label = f"`{tf.get('binary', 'terraform')}` output"
    else:
        lint = verification.get("hcl_lint", {})
        verif_snippet = "\n".join(f"{f}: {s}" for f, s in lint.get("files", {}).items())
        verif_label = "HCL syntax lint (terraform binary unavailable in this environment)"

    scope = verification.get("diff_scope", {})
    diff = scope.get("diff", "").strip()

    return f"""# {title}

## Description

- **Target Resource:** {rec['resource_name']} (`{rec['resource_arn']}`)
- **Current Configuration:** {_fmt_config(rec['current'])}
- **Optimized Configuration:** {_fmt_config(rec['recommended'])}
- **Projected Savings:** ${rec['monthly_savings_usd']:,.2f}/month (~${rec['monthly_savings_usd'] * 12:,.2f}/year)
- **Justification:** Finding **{rec['finding']}** — {_fmt_metrics(rec['metrics'])}.

## Changes

{change_lines}

## Verification Logs

Verification passed: **{verification.get('passed')}** · Diff-scope guard: **{scope.get('passed')}** (only allowlisted attributes touched)

### {verif_label}

```
{verif_snippet[:3000]}
```

### Diff

```diff
{diff[:3000]}
```

---
*Generated automatically by finops-mcp. Review and merge — changes are never applied directly to the cloud environment.*
"""


def write_pr_artifacts(repo_path: str, rec: dict[str, Any], body: str) -> str:
    out_dir = os.path.join(repo_path, ".finops")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"PR_{rec['id']}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path
