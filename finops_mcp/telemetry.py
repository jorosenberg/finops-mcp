"""Telemetry ingestion: AWS Compute Optimizer / Cost Explorer with mock fallback.

Attempts real AWS API calls via boto3. If credentials are missing or calls
fail, falls back to deterministic mock recommendations so the full pipeline
remains testable end-to-end (controlled by FINOPS_MODE=mock|aws|auto).
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any, Optional

SIGNIFICANT_SAVINGS_USD = float(os.environ.get("FINOPS_MIN_MONTHLY_SAVINGS", "20"))


@dataclasses.dataclass
class Recommendation:
    id: str
    resource_type: str            # "eks_nodegroup" | "rds_instance"
    resource_name: str            # logical/physical name
    resource_arn: str
    region: str
    current: dict[str, Any]       # e.g. {"instance_type": "m5.xlarge", "min_size": 3, ...}
    recommended: dict[str, Any]
    monthly_savings_usd: float
    metrics: dict[str, Any]       # utilization telemetry justifying the change
    finding: str                  # e.g. "Overprovisioned"
    externally_managed: bool = False  # e.g. Karpenter-managed; do not patch statically

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------
# Mock data — deterministic, mirrors real Compute Optimizer response shapes
# --------------------------------------------------------------------------

MOCK_RECOMMENDATIONS: list[Recommendation] = [
    Recommendation(
        id="rec-eks-001",
        resource_type="eks_nodegroup",
        resource_name="app-workers",
        resource_arn="arn:aws:eks:us-east-1:123456789012:nodegroup/prod-cluster/app-workers/abc",
        region="us-east-1",
        current={"instance_type": "m5.xlarge", "min_size": 3, "max_size": 10, "desired_size": 6},
        recommended={"instance_type": "m5.large", "min_size": 2, "max_size": 8, "desired_size": 4},
        monthly_savings_usd=412.56,
        metrics={
            "cpu_p95_percent": 7.8,
            "memory_p95_percent": 22.4,
            "lookback_days": 14,
            "node_count_avg": 6,
        },
        finding="Overprovisioned",
    ),
    Recommendation(
        id="rec-rds-001",
        resource_type="rds_instance",
        resource_name="orders-db",
        resource_arn="arn:aws:rds:us-east-1:123456789012:db:orders-db",
        region="us-east-1",
        current={"instance_class": "db.r5.2xlarge", "allocated_storage_gb": 500},
        recommended={"instance_class": "db.r5.xlarge", "allocated_storage_gb": 500},
        monthly_savings_usd=487.20,
        metrics={
            "cpu_p95_percent": 11.2,
            "connections_p95": 42,
            "freeable_memory_min_gb": 38.5,
            "lookback_days": 14,
        },
        finding="Overprovisioned",
    ),
    Recommendation(
        id="rec-eks-002",
        resource_type="eks_nodegroup",
        resource_name="karpenter-managed-pool",
        resource_arn="arn:aws:eks:us-east-1:123456789012:nodegroup/prod-cluster/karpenter-managed-pool/def",
        region="us-east-1",
        current={"instance_type": "c5.2xlarge", "min_size": 1, "max_size": 20},
        recommended={"instance_type": "c5.xlarge", "min_size": 1, "max_size": 20},
        monthly_savings_usd=155.0,
        metrics={"cpu_p95_percent": 14.0, "lookback_days": 14},
        finding="Overprovisioned",
        externally_managed=True,  # lifecycle owned by Karpenter — must be skipped
    ),
    Recommendation(
        id="rec-rds-002",
        resource_type="rds_instance",
        resource_name="analytics-db",
        resource_arn="arn:aws:rds:us-east-1:123456789012:db:analytics-db",
        region="us-east-1",
        current={"instance_class": "db.t3.medium", "allocated_storage_gb": 100},
        recommended={"instance_class": "db.t3.small", "allocated_storage_gb": 100},
        monthly_savings_usd=8.40,  # below significance threshold — filtered out
        metrics={"cpu_p95_percent": 18.0, "lookback_days": 14},
        finding="Overprovisioned",
    ),
]


# --------------------------------------------------------------------------
# Real AWS ingestion
# --------------------------------------------------------------------------

_LAST_AWS_ERROR: Optional[str] = None


def _aws_available() -> bool:
    global _LAST_AWS_ERROR
    try:
        import boto3

        sts = boto3.client(
            "sts",
            region_name=os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1",
        )
        sts.get_caller_identity()
        _LAST_AWS_ERROR = None
        return True
    except Exception as e:
        _LAST_AWS_ERROR = f"{type(e).__name__}: {e}"
        return False


def _fetch_rds_from_aws(region: Optional[str]) -> list[Recommendation]:
    import boto3

    client = boto3.client("compute-optimizer", region_name=region or "us-east-1")
    recs: list[Recommendation] = []
    paginator_kwargs: dict[str, Any] = {}
    while True:
        resp = client.get_rds_database_recommendations(**paginator_kwargs)
        for r in resp.get("rdsDBRecommendations", []):
            if "Overprovisioned" not in (r.get("instanceFinding") or ""):
                continue
            options = r.get("instanceRecommendationOptions") or []
            if not options:
                continue
            best = options[0]
            savings = (
                best.get("savingsOpportunity", {})
                .get("estimatedMonthlySavings", {})
                .get("value", 0.0)
            )
            metrics = {
                m["name"].lower(): m.get("value")
                for m in r.get("utilizationMetrics", [])
                if "name" in m
            }
            metrics["lookback_days"] = int(r.get("lookbackPeriodInDays", 14))
            arn = r.get("resourceArn", "")
            recs.append(
                Recommendation(
                    id=f"rec-rds-{arn.rsplit(':', 1)[-1]}",
                    resource_type="rds_instance",
                    resource_name=arn.rsplit(":", 1)[-1],
                    resource_arn=arn,
                    region=arn.split(":")[3] if arn.count(":") >= 4 else (region or ""),
                    current={"instance_class": r.get("currentDBInstanceClass", "")},
                    recommended={"instance_class": best.get("dbInstanceClass", "")},
                    monthly_savings_usd=float(savings or 0.0),
                    metrics=metrics,
                    finding=r.get("instanceFinding", ""),
                )
            )
        token = resp.get("nextToken")
        if not token:
            break
        paginator_kwargs = {"nextToken": token}
    return recs


def _fetch_eks_from_aws(region: Optional[str]) -> list[Recommendation]:
    """Compute Optimizer has no direct EKS-nodegroup API; derive from ASG
    recommendations, matching ASGs that belong to EKS nodegroups by tag."""
    import boto3

    client = boto3.client("compute-optimizer", region_name=region or "us-east-1")
    recs: list[Recommendation] = []
    kwargs: dict[str, Any] = {}
    while True:
        resp = client.get_auto_scaling_group_recommendations(**kwargs)
        for r in resp.get("autoScalingGroupRecommendations", []):
            if "Overprovisioned" not in (r.get("finding") or ""):
                continue
            options = r.get("recommendationOptions") or []
            if not options:
                continue
            best = options[0].get("configuration", {})
            cur = r.get("currentConfiguration", {})
            savings = (
                (options[0].get("savingsOpportunity") or {})
                .get("estimatedMonthlySavings", {})
                .get("value", 0.0)
            )
            metrics = {
                m["name"].lower(): m.get("value")
                for m in r.get("utilizationMetrics", [])
                if "name" in m
            }
            metrics["lookback_days"] = int(r.get("lookBackPeriodInDays", 14))
            name = r.get("autoScalingGroupName", "")
            recs.append(
                Recommendation(
                    id=f"rec-eks-{name}",
                    resource_type="eks_nodegroup",
                    resource_name=name,
                    resource_arn=r.get("autoScalingGroupArn", ""),
                    region=region or "",
                    current={
                        "instance_type": cur.get("instanceType", ""),
                        "min_size": cur.get("minSize"),
                        "max_size": cur.get("maxSize"),
                        "desired_size": cur.get("desiredCapacity"),
                    },
                    recommended={
                        "instance_type": best.get("instanceType", ""),
                        "min_size": best.get("minSize"),
                        "max_size": best.get("maxSize"),
                        "desired_size": best.get("desiredCapacity"),
                    },
                    monthly_savings_usd=float(savings or 0.0),
                    metrics=metrics,
                    finding=r.get("finding", ""),
                )
            )
        token = resp.get("nextToken")
        if not token:
            break
        kwargs = {"nextToken": token}
    return recs


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def get_recommendations(
    resource_types: Optional[list[str]] = None,
    region: Optional[str] = None,
    min_monthly_savings: Optional[float] = None,
    include_externally_managed: bool = False,
) -> dict[str, Any]:
    """Fetch and filter over-provisioning recommendations.

    Returns {"source": "aws"|"mock", "recommendations": [...], "filtered_out": [...]}.
    """
    mode = os.environ.get("FINOPS_MODE", "auto").lower()
    threshold = (
        min_monthly_savings if min_monthly_savings is not None else SIGNIFICANT_SAVINGS_USD
    )
    wanted = set(resource_types or ["eks_nodegroup", "rds_instance"])

    source = "mock"
    aws_error: Optional[str] = None
    raw: list[Recommendation]
    if mode == "aws" or (mode == "auto" and _aws_available()):
        try:
            raw = []
            if "rds_instance" in wanted:
                raw += _fetch_rds_from_aws(region)
            if "eks_nodegroup" in wanted:
                raw += _fetch_eks_from_aws(region)
            source = "aws"
        except Exception as e:
            if mode == "aws":
                raise
            aws_error = f"{type(e).__name__}: {e}"
            raw = list(MOCK_RECOMMENDATIONS)
    else:
        aws_error = _LAST_AWS_ERROR
        raw = list(MOCK_RECOMMENDATIONS)

    kept, dropped = [], []
    for rec in raw:
        if rec.resource_type not in wanted:
            continue
        reason = None
        if rec.monthly_savings_usd < threshold:
            reason = f"monthly savings ${rec.monthly_savings_usd:.2f} below threshold ${threshold:.2f}"
        elif rec.externally_managed and not include_externally_managed:
            reason = "lifecycle managed externally (e.g. Karpenter) — static patch unsafe"
        if reason:
            dropped.append({**rec.to_dict(), "skip_reason": reason})
        else:
            kept.append(rec.to_dict())

    result: dict[str, Any] = {
        "source": source,
        "recommendations": kept,
        "filtered_out": dropped,
    }
    if aws_error:
        result["aws_fallback_reason"] = aws_error
    return result


def get_recommendation_by_id(rec_id: str, **kwargs: Any) -> Optional[dict[str, Any]]:
    result = get_recommendations(include_externally_managed=True, min_monthly_savings=0, **kwargs)
    for rec in result["recommendations"]:
        if rec["id"] == rec_id:
            return rec
    return None
