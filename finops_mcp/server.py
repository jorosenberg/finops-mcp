"""finops-mcp server: FastMCP entrypoint exposing the optimization pipeline.

Tools:
  get_cost_recommendations   - Step 1: telemetry ingestion (AWS or mock)
  map_resource_to_code       - Step 2: locate the resource in local IaC
  draft_optimization         - Steps 3-4 for one recommendation: patch + verify + PR body
  run_finops_cycle           - full loop over all recommendations (Trigger A/B)
  verify_repository          - standalone verification of a repo's current state
  analyze_usage_windows      - detect recurring idle windows on EC2/RDS
  draft_schedule             - upsert Schedule tag for a schedule recommendation
  draft_scheduler_bootstrap  - generate the tag-driven scheduler as new Terraform

Run: `python -m finops_mcp.server` (stdio) or `finops-mcp` after pip install.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from . import bootstrap, gitops, mapper, patcher, runlog, scheduling, telemetry, verify as verifier

mcp = FastMCP(
    "finops-optimizer",
    instructions=(
        "Automated FinOps rightsizing and instance scheduling for EKS "
        "nodegroups, RDS instances, ECS services, Lambda functions, K8s "
        "workloads, and EC2. Ingests AWS Compute Optimizer recommendations "
        "and CloudWatch usage-timing analysis, maps them to local Terraform/"
        "OpenTofu or Helm code, applies minimal safe patches, verifies them, "
        "and drafts PR branches. Never applies changes to the cloud directly."
    ),
)


def _default_repo() -> str:
    return os.environ.get("FINOPS_REPO_PATH", os.getcwd())


@mcp.tool()
def get_cost_recommendations(
    resource_types: Optional[list[str]] = None,
    region: Optional[str] = None,
    min_monthly_savings: Optional[float] = None,
) -> str:
    """Step 1 - Fetch over-provisioning recommendations from AWS Compute
    Optimizer (falls back to mock data when no AWS credentials are available;
    controlled by FINOPS_MODE=aws|mock|auto).

    resource_types: subset of ["eks_nodegroup", "rds_instance", "ecs_service",
      "lambda_function", "k8s_workload"]; default all. K8s workload recs come
      from FINOPS_K8S_RECS_FILE (VPA/Goldilocks/Kubecost export) or mock data.
    min_monthly_savings: significance threshold in USD (default $20).
    """
    result = telemetry.get_recommendations(
        resource_types=resource_types,
        region=region,
        min_monthly_savings=min_monthly_savings,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def map_resource_to_code(recommendation_id: str, repo_path: Optional[str] = None) -> str:
    """Step 2 - Locate where the recommended resource is declared in the local
    codebase: Terraform/OpenTofu (*.tf, *.tfvars) for AWS resources, or Helm
    values.yaml for K8s workloads. Traces variable indirection to its true
    source (tfvars assignment or variable default)."""
    repo = os.path.abspath(repo_path or _default_repo())
    rec = telemetry.get_recommendation_by_id(recommendation_id)
    if not rec:
        return json.dumps({"error": f"unknown recommendation id: {recommendation_id}"})
    return json.dumps(mapper.map_recommendation_to_code(repo, rec), indent=2)


@mcp.tool()
def draft_optimization(
    recommendation_id: str,
    repo_path: Optional[str] = None,
    create_branch: bool = True,
    push: bool = False,
    run_plan: bool = False,
) -> str:
    """Steps 3-4 - For one recommendation: apply the minimal rightsizing patch,
    run local verification (terraform validate/plan when available, HCL/YAML
    lint otherwise, plus a diff-scope drift guard), and generate the structured
    PR body. Optionally commits to a new finops/* branch. Never applies to cloud.
    """
    repo = os.path.abspath(repo_path or _default_repo())
    rec = telemetry.get_recommendation_by_id(recommendation_id)
    if not rec:
        return json.dumps({"error": f"unknown recommendation id: {recommendation_id}"})

    result: dict[str, Any] = {"recommendation_id": recommendation_id, "resource": rec["resource_name"]}
    try:
        mapping = mapper.map_recommendation_to_code(repo, rec)
        result["mapping"] = mapping
        changes = patcher.apply_recommendation(mapping, rec)
        result["changes"] = changes
    except patcher.SafetyViolation as e:
        result["status"] = "blocked_by_safety_precheck"
        result["reason"] = str(e)
        return json.dumps(result, indent=2, default=str)

    modified = [c["file"] for c in changes if c.get("changed")]
    if not modified:
        result["status"] = "no_changes_needed"
        return json.dumps(result, indent=2, default=str)

    verification = verifier.verify(repo, modified, plan=run_plan)
    result["verification"] = {
        "passed": verification["passed"],
        "diff_scope_passed": verification["diff_scope"]["passed"],
        "hcl_lint_passed": verification["hcl_lint"]["passed"],
        "terraform_available": verification["terraform"].get("available", False),
    }

    if not verification["passed"]:
        # roll back working-tree changes - never leave a broken tree behind
        import subprocess

        subprocess.run(["git", "checkout", "--", "."], cwd=repo, capture_output=True)
        result["status"] = "verification_failed_rolled_back"
        result["verification_detail"] = verification
        return json.dumps(result, indent=2, default=str)

    pr_body = gitops.build_pr_body(rec, changes, verification)
    pr_path = gitops.write_pr_artifacts(repo, rec, pr_body)
    result["pr_body_file"] = pr_path

    if create_branch:
        git_result = gitops.create_branch_and_commit(repo, rec, changes, push=push)
        result["git"] = git_result

    result["status"] = "drafted"
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def run_finops_cycle(
    repo_path: Optional[str] = None,
    mode: str = "interactive",
    resource_types: Optional[list[str]] = None,
    min_monthly_savings: Optional[float] = None,
    push: bool = False,
) -> str:
    """Full optimization loop (Steps 1-4) over every significant recommendation.

    mode="interactive" (Trigger A): returns detailed per-step results for
      review in chat; creates branches but does not push.
    mode="scheduled" (Trigger B): headless - additionally appends a structured
      entry to finops_run_log.json and pushes branches when push=True.
    """
    repo = os.path.abspath(repo_path or _default_repo())
    ingest = telemetry.get_recommendations(
        resource_types=resource_types, min_monthly_savings=min_monthly_savings
    )

    summary: dict[str, Any] = {
        "mode": mode,
        "repo": repo,
        "telemetry_source": ingest["source"],
        "skipped": [
            {"id": r["id"], "resource": r["resource_name"], "reason": r["skip_reason"]}
            for r in ingest["filtered_out"]
        ],
        "results": [],
    }

    base_branch = None
    if gitops.ensure_repo(repo):
        import subprocess

        base_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo, capture_output=True, text=True,
        ).stdout.strip()

    for rec in ingest["recommendations"]:
        raw = draft_optimization(
            rec["id"], repo_path=repo, create_branch=True,
            push=(push and mode == "scheduled"),
        )
        summary["results"].append(json.loads(raw))
        # return to base branch so each recommendation gets an isolated branch
        if base_branch:
            import subprocess

            subprocess.run(["git", "checkout", base_branch], cwd=repo, capture_output=True)

    summary["drafted"] = sum(1 for r in summary["results"] if r.get("status") == "drafted")
    summary["failed"] = sum(
        1 for r in summary["results"]
        if r.get("status") not in ("drafted", "no_changes_needed")
    )

    if mode == "scheduled":
        summary["run_log"] = runlog.append_run(repo, summary)

    return json.dumps(summary, indent=2, default=str)


@mcp.tool()
def verify_repository(repo_path: Optional[str] = None, run_plan: bool = False) -> str:
    """Standalone verification: lint all *.tf/*.tfvars/values.yaml files in the
    repo, run terraform validate/plan when the binary is available, and report
    the current git diff scope."""
    repo = os.path.abspath(repo_path or _default_repo())
    files = [
        f for f in mapper._iter_files(repo)
        if f.endswith((".tf", ".tfvars")) or os.path.basename(f) in mapper.HELM_FILENAMES
    ]
    return json.dumps(verifier.verify(repo, files, plan=run_plan), indent=2, default=str)


@mcp.tool()
def analyze_usage_windows(
    region: Optional[str] = None,
    lookback_days: int = 14,
    min_monthly_savings: Optional[float] = None,
) -> str:
    """Scheduling Step 1 - Analyze EC2/RDS usage timing (CloudWatch hourly
    metrics folded into an hour-of-week profile; mock profiles without AWS
    credentials) and recommend start/stop schedules for resources with
    recurring idle windows (e.g. "running Mon-Fri 07-20, stopped nights and
    weekends"). Automatically excludes ASG members, RDS Multi-AZ/replicas/
    Aurora, prod-tagged, and already-scheduled resources. Savings estimates
    are compute-only (storage still bills while stopped)."""
    result = scheduling.get_schedule_recommendations(
        region=region,
        lookback_days=lookback_days,
        min_monthly_savings=min_monthly_savings,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def draft_schedule(
    recommendation_id: str,
    repo_path: Optional[str] = None,
    create_branch: bool = True,
    push: bool = False,
    schedule_override: Optional[str] = None,
) -> str:
    """Scheduling Steps 2-4 - For one schedule recommendation: locate the
    aws_instance / aws_db_instance block in Terraform, upsert a
    `Schedule = "<window>"` tag, verify, and draft the PR branch.

    schedule_override: replace the telemetry-derived window with a preferred
      one, e.g. "mon-fri-06-22", "daily-08-18", "sat-sun-10-16".

    Requires a tag-driven scheduler in the account for the tag to take effect
    (checked; see draft_scheduler_bootstrap). Never applies to cloud."""
    repo = os.path.abspath(repo_path or _default_repo())
    rec = scheduling.get_schedule_recommendation_by_id(recommendation_id)
    if not rec:
        return json.dumps({"error": f"unknown schedule recommendation id: {recommendation_id}"})

    result: dict[str, Any] = {"recommendation_id": recommendation_id, "resource": rec["resource_name"]}

    if schedule_override:
        spec = bootstrap.parse_schedule_name(schedule_override)
        if not spec:
            return json.dumps({
                "error": f"invalid schedule_override '{schedule_override}' "
                         "(expected mon-fri-HH-HH, daily-HH-HH, or sat-sun-HH-HH)"
            })
        rec["recommended"]["schedule"] = schedule_override
        rec["recommended"]["schedule_description"] = f"user-preferred window {schedule_override}"
        result["schedule_override"] = schedule_override
        # recompute savings from the chosen window's actual off-hours
        price = rec.get("metrics", {}).get("assumed_hourly_price_usd")
        if price:
            off = 168 - len(spec["days"]) * (spec["stop"] - spec["start"])
            rec["monthly_savings_usd"] = round(off * 4.345 * float(price), 2)
            rec["metrics"]["scheduled_off_hours_per_week"] = off

    detection = bootstrap.check_scheduler_deployed(rec.get("region"))
    result["scheduler_deployed"] = detection
    if not detection.get("deployed"):
        result["warning"] = (
            "no tag-driven scheduler detected in the account - the Schedule tag "
            "will have no effect until one is deployed (see draft_scheduler_bootstrap)"
        )

    if rec.get("externally_managed"):
        result["status"] = "blocked_by_safety_precheck"
        result["reason"] = rec.get("metrics", {}).get("skip_note", "lifecycle managed externally")
        return json.dumps(result, indent=2, default=str)

    mapping = mapper.map_schedule_target(repo, rec)
    result["mapping"] = mapping
    if not mapping.get("found"):
        result["status"] = "mapping_failed"
        return json.dumps(result, indent=2, default=str)

    try:
        scope = (
            rf'resource\s+"{mapping["terraform_type"]}"\s+'
            rf'"{mapping["terraform_name"]}"\s*'
        )
        change = patcher.upsert_schedule_tag(
            mapping["declaration_file"], scope, rec["recommended"]["schedule"]
        )
        result["changes"] = [change]
    except patcher.SafetyViolation as e:
        result["status"] = "blocked_by_safety_precheck"
        result["reason"] = str(e)
        return json.dumps(result, indent=2, default=str)

    if not change.get("changed"):
        result["status"] = "no_changes_needed"
        return json.dumps(result, indent=2, default=str)

    verification = verifier.verify(repo, [change["file"]])
    result["verification"] = {
        "passed": verification["passed"],
        "diff_scope_passed": verification["diff_scope"]["passed"],
        "hcl_lint_passed": verification["hcl_lint"]["passed"],
    }
    if not verification["passed"]:
        import subprocess

        subprocess.run(["git", "checkout", "--", "."], cwd=repo, capture_output=True)
        result["status"] = "verification_failed_rolled_back"
        result["verification_detail"] = verification
        return json.dumps(result, indent=2, default=str)

    pr_body = gitops.build_pr_body(rec, [change], verification, action="schedule")
    result["pr_body_file"] = gitops.write_pr_artifacts(repo, rec, pr_body)

    if create_branch:
        result["git"] = gitops.create_branch_and_commit(
            repo, rec, [change], push=push, action="schedule"
        )

    result["status"] = "drafted"
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def draft_scheduler_bootstrap(
    repo_path: Optional[str] = None,
    schedules: Optional[list[str]] = None,
    timezone: str = "UTC",
    create_branch: bool = True,
    push: bool = False,
    force: bool = False,
) -> str:
    """Generate the tag-driven instance scheduler itself as NEW Terraform in
    the repo (finops-scheduler.tf + Lambda source): a Lambda that runs every
    15 minutes via EventBridge and starts/stops EC2/RDS instances whose
    `Schedule` tag matches a defined window.

    Never deploys to the cloud - the generated code is drafted on a
    finops/bootstrap-* branch for review and applied via your normal
    terraform workflow. Skips when a scheduler is already detected in the
    account (override with force=True).

    schedules: window names to define, e.g. ["mon-fri-07-20"]. Defaults to
      every window currently recommended by analyze_usage_windows.
    timezone: IANA timezone the windows are evaluated in, e.g. "America/New_York".
    """
    repo = os.path.abspath(repo_path or _default_repo())
    result: dict[str, Any] = {}

    detection = bootstrap.check_scheduler_deployed()
    result["scheduler_deployed"] = detection
    if detection.get("deployed") and not force:
        result["status"] = "already_deployed"
        result["note"] = "a scheduler already exists in the account; pass force=True to generate anyway"
        return json.dumps(result, indent=2, default=str)

    names = schedules
    if not names:
        recs = scheduling.get_schedule_recommendations()
        names = sorted({r["recommended"]["schedule"] for r in recs["recommendations"]})
    result["schedules"] = names

    try:
        changes = bootstrap.generate_bootstrap_files(repo, names, timezone=timezone)
        result["changes"] = changes
    except ValueError as e:
        result["status"] = "blocked"
        result["reason"] = str(e)
        return json.dumps(result, indent=2, default=str)

    verification = verifier.verify(repo, [c["file"] for c in changes])
    result["verification"] = {
        "passed": verification["passed"],
        "hcl_lint_passed": verification["hcl_lint"]["passed"],
        "diff_scope_passed": verification["diff_scope"]["passed"],
    }
    if not verification["passed"]:
        for c in changes:
            if os.path.exists(c["file"]):
                os.remove(c["file"])
        result["status"] = "verification_failed_rolled_back"
        result["verification_detail"] = verification
        return json.dumps(result, indent=2, default=str)

    pseudo_rec = {
        "id": "rec-bootstrap-scheduler",
        "resource_name": "instance-scheduler",
        "resource_type": "scheduler_bootstrap",
        "resource_arn": "(new infrastructure)",
        "current": {"scheduler": "not deployed"},
        "recommended": {"scheduler": "finops Lambda scheduler", "schedules": ", ".join(names),
                        "timezone": timezone},
        "monthly_savings_usd": 0.0,
        "metrics": {"note": "enables Schedule tags drafted by draft_schedule", "lookback_days": 0},
        "finding": "SchedulerMissing",
    }
    pr_body = gitops.build_pr_body(pseudo_rec, changes, verification, action="bootstrap")
    result["pr_body_file"] = gitops.write_pr_artifacts(repo, pseudo_rec, pr_body)

    if create_branch:
        result["git"] = gitops.create_branch_and_commit(
            repo, pseudo_rec, changes, push=push, action="bootstrap"
        )

    result["status"] = "drafted"
    return json.dumps(result, indent=2, default=str)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
