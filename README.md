# finops-mcp

An [MCP](https://modelcontextprotocol.io) server for automated AWS cost optimization. It ingests AWS Compute Optimizer over-provisioning recommendations (EKS nodegroups, RDS instances, ECS services, Lambda functions) plus Kubernetes pod/HPA rightsizing, maps them to your local Terraform/OpenTofu codebase or Helm values.yaml, applies minimal safe rightsizing patches, verifies them locally, and drafts Git branches with structured PR bodies. It can also detect recurring idle windows on EC2/RDS and draft start/stop schedules - and generate the scheduler infrastructure itself when none exists.

**It never applies changes to your cloud environment directly.** All output is code changes on local branches for human review.

Supported resource types: `eks_nodegroup`, `rds_instance`, `ecs_service`, `lambda_function`, `k8s_workload` (pod requests/limits + HPA replicas via Helm values.yaml; recommendations from a VPA/Goldilocks/Kubecost JSON export via `FINOPS_K8S_RECS_FILE`), plus EC2/RDS instance scheduling from usage timing.

## How it works

```
AWS Compute Optimizer ----> 1. Telemetry ingestion (filter significant, safe recommendations)
(+ K8s recs export,         2. Codebase mapping    (find the resource in *.tf / *.tfvars /
 + CloudWatch timing)                               values.yaml, trace variable indirection)
                            3. Safe patching       (minimal diff, allowlisted attributes only)
                            4. Local verification  (terraform validate/plan or HCL/YAML lint,
                                                    plus a diff-scope drift guard)
                            5. PR drafting         (isolated finops/* branch + structured PR body)
```

## Tools

| Tool | What it does |
|---|---|
| `get_cost_recommendations` | Fetch over-provisioning recommendations from Compute Optimizer (mock fallback without credentials) |
| `map_resource_to_code` | Locate a recommended resource's declaration in local IaC, tracing `var.x` to tfvars or variable defaults; values.yaml for K8s workloads |
| `draft_optimization` | Patch one recommendation, verify, generate PR body, create a `finops/*` branch |
| `run_finops_cycle` | Full loop over all recommendations; `mode="interactive"` or `mode="scheduled"` (writes `finops_run_log.json`) |
| `verify_repository` | Standalone lint / `terraform validate` / diff-scope check of a repo |
| `analyze_usage_windows` | Detect recurring idle windows on EC2/RDS from CloudWatch hour-of-week profiles and recommend start/stop schedules |
| `draft_schedule` | Upsert a `Schedule` tag in Terraform for one schedule recommendation (optionally with a user-preferred `schedule_override`), verify, draft PR branch |
| `draft_scheduler_bootstrap` | Generate the tag-driven scheduler itself (Lambda + EventBridge + IAM + schedule definitions) as new Terraform files on a `finops/bootstrap-*` branch when none is detected in the account |

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
| `FINOPS_K8S_RECS_FILE` | Optional JSON export of K8s workload recommendations (VPA/Goldilocks/Kubecost) | - |
| `FINOPS_MIN_IDLE_HOURS_WEEK` | Minimum recurring idle time to recommend a schedule | `40` |
| `FINOPS_IDLE_CONFIDENCE` | Fraction of weeks an hour must be idle to count | `0.9` |
| `FINOPS_EC2_IDLE_CPU` | CPU % below which an EC2 hour counts as idle | `3.0` |
| `FINOPS_TF_TIMEOUT` | Per-step timeout (seconds) for terraform init/validate/plan during verification | `45` |
| `FINOPS_SKIP_TERRAFORM` | `true` to skip terraform verification entirely (HCL lint + diff guard still run) | `false` |
| `AWS_REGION` | Region queried for recommendations | `us-east-1` |
| `FINOPS_PUSH` | Scheduler only: `true` to push branches to origin | `false` |

### AWS prerequisites

- Credentials resolvable by boto3 (`aws configure`, SSO, or env vars) for an identity with `compute-optimizer:Get*` (plus `cloudwatch:GetMetricData`, `ec2:DescribeInstances`, `rds:DescribeDBInstances` for usage-window analysis) - avoid root credentials
- [Compute Optimizer opted in](https://docs.aws.amazon.com/compute-optimizer/latest/ug/getting-started.html) on the account; first findings appear up to 24h after opt-in and require sufficient CloudWatch history
- Without working credentials the server serves deterministic mock data and reports why in `aws_fallback_reason`

## Safety guarantees

- Only allowlisted rightsizing attributes are ever patched - Terraform: `instance_type(s)`, `instance_class`, `min/max/desired_size`, `allocated_storage`, `cpu`, `memory`, `memory_size`, `desired_count`, `Schedule` tag; YAML: `cpu`, `memory`, `minReplicas`, `maxReplicas`, `replicas`, `replicaCount`
- Secret-bearing lines (password/token/key), VPC/subnet/security-group blocks, and `backend` state configuration are never modified - enforced at patch time and re-checked by a diff-scope guard over `git diff`
- Resources whose lifecycle is managed externally (e.g. Karpenter nodepools, ASG members) are detected and skipped
- Failed verification automatically rolls back the working tree (generated bootstrap files are deleted on failure)
- Each recommendation gets its own isolated `finops/rightsize-*`, `finops/schedule-*`, or `finops/bootstrap-*` branch; nothing is pushed unless explicitly requested
- Bootstrap generation refuses to overwrite existing files that don't look finops-generated

## Instance scheduling (usage timing)

`analyze_usage_windows` pulls hourly CloudWatch metrics (EC2 `CPUUtilization`, RDS `DatabaseConnections`) over a lookback window, folds them into a 168-slot hour-of-week profile, and recommends start/stop schedules for resources with recurring idle windows. Each day's window is the longest contiguous activity block; isolated spikes (crons, backups) are excluded from the window but reported with a review warning. An hour counts as idle when it was idle in >=90% of observed weeks (tunable via `FINOPS_IDLE_CONFIDENCE`; minimum recurring idle time `FINOPS_MIN_IDLE_HOURS_WEEK`, default 40h/week). Savings are computed from the hours the window actually turns the instance off.

`draft_schedule` enforces the schedule GitOps-style: it upserts a `Schedule = "<window>"` tag into the resource's Terraform `tags` map - compatible with [AWS Instance Scheduler](https://aws.amazon.com/solutions/implementations/instance-scheduler-on-aws/) and other tag-driven schedulers.

Prefer a different window than the telemetry-derived one? Pass `schedule_override` to `draft_schedule` (formats: `mon-fri-HH-HH`, `daily-HH-HH`, `sat-sun-HH-HH`); savings are recomputed for your chosen window.

**No scheduler in the account yet?** `draft_schedule` detects this (read-only probe for a scheduler Lambda/EventBridge rule/Instance Scheduler stack) and warns in its output. `draft_scheduler_bootstrap` then generates a complete lightweight scheduler as new Terraform files in your repo - `finops-scheduler.tf` (Lambda + EventBridge 15-minute rule + IAM + a `locals` map of schedule definitions, timezone-aware) and `finops_scheduler_lambda.py` (the ~75-line reviewable Lambda source) - on a `finops/bootstrap-*` branch. You review and `terraform apply` it yourself; the MCP never deploys anything.

Scheduling safety exclusions: EC2 instances in ASGs, RDS Multi-AZ/read replicas/Aurora members, resources tagged `Environment=prod/production`, and resources that already carry a `Schedule` tag. Savings estimates are compute-only (EBS/RDS storage still bills while stopped) from an approximate on-demand price table.

## Scheduled runs (Trigger B)

`finops_scheduler.py` runs the full cycle headlessly every Friday at 15:00 and logs to `finops_pipeline.log` + `finops_run_log.json`:

```bash
pip install schedule
FINOPS_REPO_PATH=/path/to/repo python finops_scheduler.py
# set FINOPS_RUN_NOW=true to fire immediately for testing
```

## Try it without AWS

`sample-infra/` is a demo repo (EKS nodegroup, RDS instances, ECS service, Lambda function, EC2 instance, and a Helm chart for a K8s workload - with tfvars indirection, protected networking blocks, and sensitive lines) that matches the built-in mock recommendations:

```bash
cd sample-infra && git init -b main && git add -A && git commit -m initial && cd ..
FINOPS_MODE=mock python -c "from finops_mcp import server; print(server.run_finops_cycle(repo_path='sample-infra'))"
FINOPS_MODE=mock python -c "from finops_mcp import server; print(server.draft_schedule('rec-sched-ec2-dev-runner', repo_path='sample-infra'))"
FINOPS_MODE=mock python -c "from finops_mcp import server; print(server.draft_scheduler_bootstrap(repo_path='sample-infra', timezone='America/New_York'))"
```

Expected result: five rightsizing branches (EKS, RDS, ECS, Lambda, K8s), `finops/schedule-*` branches adding `Schedule` tags to dev-runner and staging-db, and a `finops/bootstrap-*` branch containing the generated scheduler - each with a PR body under `.finops/`.

## Example PR body

```markdown
# chore(finops): rightsize orders-db (rds_instance) for cost optimization

- **Target Resource:** orders-db (arn:aws:rds:...)
- **Current Configuration:** instance_class=db.r5.2xlarge
- **Optimized Configuration:** instance_class=db.r5.xlarge
- **Projected Savings:** $487.20/month (~$5,846.40/year)
- **Justification:** Compute Optimizer finding Overprovisioned - cpu p95 = 11.2%;
  connections p95 = 42 over a 14-day lookback.
```

## Known issues

- Non-ASCII characters (em dashes) previously appeared in source strings and generated artifacts (PR bodies, `finops-scheduler.tf`, Lambda source) and caused encoding problems on Windows/cp1252 environments, particularly when the remote state is not local. As of this commit all em dashes are replaced with ASCII hyphens; generated artifacts are pure ASCII.

## License

MIT
