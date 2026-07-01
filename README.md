# finops-mcp

An [MCP](https://modelcontextprotocol.io) server for automated AWS cost optimization. It ingests AWS Compute Optimizer over-provisioning recommendations (EKS nodegroups, RDS instances), maps them to your local Terraform/OpenTofu codebase, applies minimal safe rightsizing patches, verifies them locally, and drafts Git branches with structured PR bodies.

**It never applies changes to your cloud environment directly.** All output is code changes on local branches for human review.

## How it works

```
AWS Compute Optimizer ──▶ 1. Telemetry ingestion (filter significant, safe recommendations)
                          2. Codebase mapping    (find the resource in *.tf / *.tfvars,
                                                  trace variable indirection to its source)
                          3. Safe patching       (minimal diff, allowlisted attributes only)
                          4. Local verification  (terraform validate/plan or HCL lint,
                                                  plus a diff-scope drift guard)
                          5. PR drafting         (isolated finops/* branch + structured PR body)
```

## Tools

| Tool | What it does |
|---|---|
| `get_cost_recommendations` | Fetch over-provisioning recommendations from Compute Optimizer (mock fallback without credentials) |
| `map_resource_to_code` | Locate a recommended resource's declaration in local IaC, tracing `var.x` to tfvars or variable defaults |
| `draft_optimization` | Patch one recommendation, verify, generate PR body, create a `finops/*` branch |
| `run_finops_cycle` | Full loop over all recommendations; `mode="interactive"` or `mode="scheduled"` (writes `finops_run_log.json`) |
| `verify_repository` | Standalone lint / `terraform validate` / diff-scope check of a repo |

## Install

```bash
git clone https://github.com/jorosenberg/finops-mcp
cd finops-mcp
pip install -e .
```

## Register with an MCP client

Claude Desktop (`claude_desktop_config.json`) or any MCP-compatible client:

```json
{
  "mcpServers": {
    "finops-optimizer": {
      "command": "python",
      "args": ["-m", "finops_mcp.server"],
      "cwd": "<path-to>/finops-mcp",
      "env": {
        "FINOPS_REPO_PATH": "<path-to-your-terraform-repo>",
        "FINOPS_MODE": "auto",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

Claude Code: `claude mcp add finops-optimizer -- python -m finops_mcp.server`

## Configuration

| Env var | Meaning | Default |
|---|---|---|
| `FINOPS_MODE` | `auto` (real AWS if credentials work, else mock), `aws`, or `mock` | `auto` |
| `FINOPS_REPO_PATH` | Default Terraform repo when tools omit `repo_path` | cwd |
| `FINOPS_MIN_MONTHLY_SAVINGS` | Significance threshold in USD | `20` |
| `AWS_REGION` | Region queried for recommendations | `us-east-1` |
| `FINOPS_PUSH` | Scheduler only: `true` to push branches to origin | `false` |

### AWS prerequisites

- Credentials resolvable by boto3 (`aws configure`, SSO, or env vars) for an identity with `compute-optimizer:Get*` — avoid root credentials
- [Compute Optimizer opted in](https://docs.aws.amazon.com/compute-optimizer/latest/ug/getting-started.html) on the account; first findings appear up to 24h after opt-in and require sufficient CloudWatch history
- Without working credentials the server serves deterministic mock data and reports why in `aws_fallback_reason`

## Safety guarantees

- Only allowlisted rightsizing attributes are ever patched: `instance_type(s)`, `instance_class`, `min/max/desired_size`, `allocated_storage`
- Secret-bearing lines (password/token/key), VPC/subnet/security-group blocks, and `backend` state configuration are never modified — enforced at patch time and re-checked by a diff-scope guard over `git diff`
- Resources whose lifecycle is managed externally (e.g. Karpenter nodepools) are detected and skipped
- Failed verification automatically rolls back the working tree
- Each recommendation gets its own isolated `finops/rightsize-<name>-<date>` branch; nothing is pushed unless explicitly requested

## Scheduled runs (Trigger B)

`finops_scheduler.py` runs the full cycle headlessly every Friday at 15:00 and logs to `finops_pipeline.log` + `finops_run_log.json`:

```bash
pip install schedule
FINOPS_REPO_PATH=/path/to/repo python finops_scheduler.py
# set FINOPS_RUN_NOW=true to fire immediately for testing
```

## Try it without AWS

`sample-infra/` is a demo Terraform repo (EKS nodegroup + RDS instance, tfvars indirection, protected networking blocks, a sensitive password line) that matches the built-in mock recommendations:

```bash
cd sample-infra && git init -b main && git add -A && git commit -m initial && cd ..
FINOPS_MODE=mock python -c "from finops_mcp import server; print(server.run_finops_cycle(repo_path='sample-infra'))"
```

Expected result: two isolated branches — `finops/rightsize-app-workers-<date>` (m5.xlarge→m5.large via tfvars + scaling 3/10/6→2/8/4) and `finops/rightsize-orders-db-<date>` (db.r5.2xlarge→db.r5.xlarge) — each with a PR body under `.finops/`.

## Example PR body

```markdown
# chore(finops): rightsize orders-db (rds_instance) for cost optimization

- **Target Resource:** orders-db (arn:aws:rds:...)
- **Current Configuration:** instance_class=db.r5.2xlarge
- **Optimized Configuration:** instance_class=db.r5.xlarge
- **Projected Savings:** $487.20/month (~$5,846.40/year)
- **Justification:** Compute Optimizer finding Overprovisioned — cpu p95 = 11.2%;
  connections p95 = 42 over a 14-day lookback.
```

## License

MIT
