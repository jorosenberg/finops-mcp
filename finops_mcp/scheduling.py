"""Instance scheduling: detect recurring idle windows on EC2/RDS and
recommend start/stop schedules enforced via a `Schedule` tag (compatible with
AWS Instance Scheduler and similar tag-driven schedulers).

Analysis: pull hourly CloudWatch metrics over a lookback window, fold them
into a 168-slot hour-of-week profile, and mark an hour idle when it was idle
in >= FINOPS_IDLE_CONFIDENCE of observed weeks. A schedule is recommended
when the recurring idle time is >= FINOPS_MIN_IDLE_HOURS_WEEK.

Safety exclusions (never scheduled):
- EC2 instances in an Auto Scaling Group (the ASG would restart them)
- RDS Multi-AZ instances, read replicas, and Aurora cluster members
- anything tagged Environment=prod/production or Schedule=<already set>

Savings are estimates from a static on-demand price table (documented
approximation; exact prices vary by region/OS).
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .telemetry import Recommendation

MIN_IDLE_HOURS_WEEK = float(os.environ.get("FINOPS_MIN_IDLE_HOURS_WEEK", "40"))
IDLE_CONFIDENCE = float(os.environ.get("FINOPS_IDLE_CONFIDENCE", "0.9"))
EC2_IDLE_CPU_PERCENT = float(os.environ.get("FINOPS_EC2_IDLE_CPU", "3.0"))
WEEKS_PER_MONTH = 4.345

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Approximate us-east-1 on-demand hourly prices (Linux / single-AZ).
PRICE_PER_HOUR = {
    "t3.medium": 0.0416, "t3.large": 0.0832, "t3.xlarge": 0.1664,
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384,
    "c5.xlarge": 0.17, "c5.2xlarge": 0.34,
    "r5.large": 0.126, "r5.xlarge": 0.252,
    "db.t3.medium": 0.068, "db.t3.large": 0.136,
    "db.r5.large": 0.24, "db.r5.xlarge": 0.48, "db.r5.2xlarge": 0.96,
    "db.m5.large": 0.171, "db.m5.xlarge": 0.342,
}
DEFAULT_PRICE = 0.10


# --------------------------------------------------------------------------
# Profile -> schedule derivation
# --------------------------------------------------------------------------

def _profile_to_schedule(active: list[bool]) -> Optional[dict[str, Any]]:
    """active: 168 bools (Mon 00:00 .. Sun 23:00). Returns a schedule spec or
    None when the instance is essentially always active / always idle."""
    idle_hours = active.count(False)
    if idle_hours < MIN_IDLE_HOURS_WEEK:
        return None
    if idle_hours >= 166:
        # effectively never used — that's a decommission candidate, not a schedule
        return {"kind": "always_idle", "idle_hours_per_week": idle_hours}

    # Per-day active spans
    day_spans: dict[str, Optional[tuple[int, int]]] = {}
    for d in range(7):
        hours = [h for h in range(24) if active[d * 24 + h]]
        day_spans[DAYS[d]] = (min(hours), max(hours) + 1) if hours else None

    weekday_spans = [s for day, s in day_spans.items() if day in DAYS[:5] and s]
    weekend_active = any(day_spans[d] for d in DAYS[5:])

    if weekday_spans and not weekend_active:
        start = min(s[0] for s in weekday_spans)
        stop = max(s[1] for s in weekday_spans)
        name = f"mon-fri-{start:02d}-{stop:02d}"
        description = f"running Mon-Fri {start:02d}:00-{stop:02d}:00, stopped nights and weekends"
    else:
        start = min(s[0] for s in day_spans.values() if s)
        stop = max(s[1] for s in day_spans.values() if s)
        name = f"daily-{start:02d}-{stop:02d}"
        description = f"running daily {start:02d}:00-{stop:02d}:00, stopped overnight"

    return {
        "kind": "window",
        "name": name,
        "description": description,
        "idle_hours_per_week": idle_hours,
    }


def _build_recommendation(
    kind: str,
    name: str,
    arn: str,
    region: str,
    instance_type: str,
    schedule: dict[str, Any],
    metrics: dict[str, Any],
    externally_managed: bool = False,
    skip_note: str = "",
) -> Recommendation:
    idle = schedule["idle_hours_per_week"]
    price = PRICE_PER_HOUR.get(instance_type, DEFAULT_PRICE)
    savings = round(idle * WEEKS_PER_MONTH * price, 2)
    metrics = {
        **metrics,
        "idle_hours_per_week": idle,
        "assumed_hourly_price_usd": price,
        "note": "compute-only savings; EBS/RDS storage still billed while stopped",
    }
    if skip_note:
        metrics["skip_note"] = skip_note
    return Recommendation(
        id=f"rec-sched-{kind}-{name}",
        resource_type=f"{kind}_schedule",
        resource_name=name,
        resource_arn=arn,
        region=region,
        current={"schedule": "always-on (24x7)"},
        recommended={
            "schedule": schedule.get("name", "n/a"),
            "schedule_description": schedule.get("description", ""),
            "enforcement": 'tags: Schedule = "' + schedule.get("name", "") + '"',
        },
        monthly_savings_usd=savings,
        metrics=metrics,
        finding="RecurringIdleWindow" if schedule["kind"] == "window" else "AlwaysIdle",
        externally_managed=externally_managed,
    )


# --------------------------------------------------------------------------
# Mock profiles — deterministic synthetic workloads
# --------------------------------------------------------------------------

def _mock_profile(active_weekday: tuple[int, int], weekend: bool) -> list[bool]:
    prof = []
    for d in range(7):
        for h in range(24):
            if d < 5:
                prof.append(active_weekday[0] <= h < active_weekday[1])
            else:
                prof.append(weekend)
    return prof


def _mock_schedule_recommendations() -> list[Recommendation]:
    recs = []

    # dev-runner: busy weekdays 07-20, dead nights/weekends
    sched = _profile_to_schedule(_mock_profile((7, 20), weekend=False))
    recs.append(_build_recommendation(
        "ec2", "dev-runner",
        "arn:aws:ec2:us-east-1:123456789012:instance/i-0abc123def456",
        "us-east-1", "m5.2xlarge", sched,
        {"cpu_p95_active_window_percent": 46.0, "cpu_p95_idle_window_percent": 0.8,
         "lookback_days": 14},
    ))

    # staging-db: connections only weekdays 06-22
    sched = _profile_to_schedule(_mock_profile((6, 22), weekend=False))
    recs.append(_build_recommendation(
        "rds", "staging-db",
        "arn:aws:rds:us-east-1:123456789012:db:staging-db",
        "us-east-1", "db.r5.large", sched,
        {"connections_p95_active_window": 18, "connections_max_idle_window": 0,
         "lookback_days": 14},
    ))

    # ci-worker: idle nights but in an ASG — must be skipped
    sched = _profile_to_schedule(_mock_profile((8, 18), weekend=False))
    recs.append(_build_recommendation(
        "ec2", "ci-worker",
        "arn:aws:ec2:us-east-1:123456789012:instance/i-0asg99887766",
        "us-east-1", "c5.xlarge", sched,
        {"cpu_p95_active_window_percent": 62.0, "lookback_days": 14},
        externally_managed=True,
        skip_note="member of ASG ci-workers-asg; lifecycle managed by the ASG",
    ))
    return recs


# --------------------------------------------------------------------------
# Real AWS analysis
# --------------------------------------------------------------------------

def _hourly_idle_profile(
    cloudwatch: Any,
    namespace: str,
    metric: str,
    dimensions: list[dict[str, str]],
    idle_threshold: float,
    lookback_days: int,
) -> Optional[list[bool]]:
    """Fold hourly datapoints into a 168-slot active/idle profile. An hour-of-
    week slot counts as idle when it was idle in >= IDLE_CONFIDENCE of the
    weeks it was observed."""
    import datetime

    end = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - datetime.timedelta(days=lookback_days)
    resp = cloudwatch.get_metric_data(
        MetricDataQueries=[{
            "Id": "m1",
            "MetricStat": {
                "Metric": {"Namespace": namespace, "MetricName": metric, "Dimensions": dimensions},
                "Period": 3600,
                "Stat": "Maximum",
            },
        }],
        StartTime=start, EndTime=end,
    )
    results = resp["MetricDataResults"][0]
    points = list(zip(results.get("Timestamps", []), results.get("Values", [])))
    if len(points) < 24 * 7:  # need at least a week of data
        return None

    idle_count = [0] * 168
    seen_count = [0] * 168
    for ts, value in points:
        slot = ts.weekday() * 24 + ts.hour
        seen_count[slot] += 1
        if value <= idle_threshold:
            idle_count[slot] += 1

    active = []
    for slot in range(168):
        if seen_count[slot] == 0:
            active.append(True)  # unknown -> assume active (safe)
        else:
            active.append((idle_count[slot] / seen_count[slot]) < IDLE_CONFIDENCE)
    return active


def _analyze_ec2(region: Optional[str], lookback_days: int) -> list[Recommendation]:
    import boto3

    ec2 = boto3.client("ec2", region_name=region or "us-east-1")
    cw = boto3.client("cloudwatch", region_name=region or "us-east-1")
    recs: list[Recommendation] = []

    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=[{"Name": "instance-state-name", "Values": ["running"]}]):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                name = tags.get("Name", inst["InstanceId"])
                env = tags.get("Environment", "").lower()
                in_asg = "aws:autoscaling:groupName" in tags
                already = "Schedule" in tags
                if env in ("prod", "production") or already:
                    continue
                profile = _hourly_idle_profile(
                    cw, "AWS/EC2", "CPUUtilization",
                    [{"Name": "InstanceId", "Value": inst["InstanceId"]}],
                    EC2_IDLE_CPU_PERCENT, lookback_days,
                )
                if not profile:
                    continue
                sched = _profile_to_schedule(profile)
                if not sched or sched["kind"] != "window":
                    continue
                recs.append(_build_recommendation(
                    "ec2", name,
                    f"arn:aws:ec2:{region or 'us-east-1'}::instance/{inst['InstanceId']}",
                    region or "us-east-1", inst.get("InstanceType", ""), sched,
                    {"lookback_days": lookback_days},
                    externally_managed=in_asg,
                    skip_note=f"member of ASG {tags.get('aws:autoscaling:groupName')}" if in_asg else "",
                ))
    return recs


def _analyze_rds(region: Optional[str], lookback_days: int) -> list[Recommendation]:
    import boto3

    rds = boto3.client("rds", region_name=region or "us-east-1")
    cw = boto3.client("cloudwatch", region_name=region or "us-east-1")
    recs: list[Recommendation] = []

    paginator = rds.get_paginator("describe_db_instances")
    for page in paginator.paginate():
        for db in page["DBInstances"]:
            ident = db["DBInstanceIdentifier"]
            engine = db.get("Engine", "")
            unschedulable = (
                db.get("MultiAZ")
                or db.get("ReadReplicaSourceDBInstanceIdentifier")
                or db.get("ReadReplicaDBInstanceIdentifiers")
                or engine.startswith("aurora")
                or db.get("DBClusterIdentifier")
            )
            profile = _hourly_idle_profile(
                cw, "AWS/RDS", "DatabaseConnections",
                [{"Name": "DBInstanceIdentifier", "Value": ident}],
                0.0, lookback_days,
            )
            if not profile:
                continue
            sched = _profile_to_schedule(profile)
            if not sched or sched["kind"] != "window":
                continue
            reason = ""
            if unschedulable:
                reason = "Multi-AZ / replica / Aurora member — stop-start scheduling unsafe"
            recs.append(_build_recommendation(
                "rds", ident,
                db.get("DBInstanceArn", ""),
                region or "us-east-1", db.get("DBInstanceClass", ""), sched,
                {"lookback_days": lookback_days},
                externally_managed=bool(unschedulable),
                skip_note=reason,
            ))
    return recs


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def get_schedule_recommendations(
    region: Optional[str] = None,
    lookback_days: int = 14,
    min_monthly_savings: Optional[float] = None,
) -> dict[str, Any]:
    """Analyze usage timing and return schedule recommendations, filtered the
    same way as rightsizing recs (significance + external management)."""
    from .telemetry import SIGNIFICANT_SAVINGS_USD, _aws_available

    mode = os.environ.get("FINOPS_MODE", "auto").lower()
    threshold = min_monthly_savings if min_monthly_savings is not None else SIGNIFICANT_SAVINGS_USD

    source = "mock"
    aws_error: Optional[str] = None
    if mode == "aws" or (mode == "auto" and _aws_available()):
        try:
            raw = _analyze_ec2(region, lookback_days) + _analyze_rds(region, lookback_days)
            source = "aws"
        except Exception as e:
            if mode == "aws":
                raise
            aws_error = f"{type(e).__name__}: {e}"
            raw = _mock_schedule_recommendations()
    else:
        raw = _mock_schedule_recommendations()

    kept, dropped = [], []
    for rec in raw:
        reason = None
        if rec.monthly_savings_usd < threshold:
            reason = f"monthly savings ${rec.monthly_savings_usd:.2f} below threshold ${threshold:.2f}"
        elif rec.externally_managed:
            reason = rec.metrics.get("skip_note", "lifecycle managed externally")
        if reason:
            dropped.append({**rec.to_dict(), "skip_reason": reason})
        else:
            kept.append(rec.to_dict())

    result: dict[str, Any] = {"source": source, "recommendations": kept, "filtered_out": dropped}
    if aws_error:
        result["aws_fallback_reason"] = aws_error
    return result


def get_schedule_recommendation_by_id(rec_id: str, **kwargs: Any) -> Optional[dict[str, Any]]:
    result = get_schedule_recommendations(min_monthly_savings=0, **kwargs)
    for rec in result["recommendations"] + result["filtered_out"]:
        if rec["id"] == rec_id:
            return rec
    return None
