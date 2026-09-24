"""领域对象到对外 JSON 结构的序列化。"""

from __future__ import annotations

from ..domain import entities as e


def _enum(value) -> str | int | None:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else value


def link_dict(link: e.Link) -> dict:
    return {
        "code": link.code,
        "name": link.name,
        "total_capacity_mbps": link.total_capacity_mbps,
        "supported_latency": link.supported_latency.value,
        "supported_latency_label": link.supported_latency.label,
        "reliability_target": link.reliability_target,
        "status": link.status.value,
        "alternative_codes": link.alternative_codes,
    }


def tenant_dict(tenant: e.Tenant) -> dict:
    return {
        "code": tenant.code,
        "name": tenant.name,
        "quota_mbps": tenant.quota_mbps,
    }


def maintenance_dict(window: e.MaintenanceWindow) -> dict:
    return {
        "code": window.code,
        "link_code": window.link_code,
        "starts_at": window.starts_at,
        "ends_at": window.ends_at,
        "note": window.note,
    }


def reservation_dict(res: e.Reservation) -> dict:
    return {
        "code": res.code,
        "tenant_code": res.tenant_code,
        "link_codes": res.link_codes,
        "current_links": res.current_links,
        "starts_at": res.starts_at,
        "ends_at": res.ends_at,
        "bandwidth_mbps": res.bandwidth_mbps,
        "latency_class": res.latency_class.value,
        "current_latency": _enum(res.current_latency),
        "reliability": res.reliability,
        "priority": int(res.priority),
        "priority_label": res.priority.label,
        "status": res.status.value,
        "batch_code": res.batch_code,
        "created_at": res.created_at,
    }


def batch_dict(batch: e.BatchResult) -> dict:
    return {
        "code": batch.code,
        "status": batch.status,
        "accepted": batch.accepted,
        "rejected": batch.rejected,
        "decided_at": batch.decided_at,
    }


def telemetry_dict(sample: e.TelemetrySample, *, include_version: bool = True) -> dict:
    out = {
        "link_code": sample.link_code,
        "bucket_ts": sample.bucket_ts,
        "observed_latency_ms": sample.observed_latency_ms,
        "observed_reliability": sample.observed_reliability,
        "observed": sample.observed,
    }
    if include_version:
        out["version"] = sample.version
        out["superseded"] = sample.superseded
    return out


def incident_dict(incident: e.Incident) -> dict:
    return {
        "code": incident.code,
        "kind": incident.kind.value,
        "link_code": incident.link_code,
        "starts_at": incident.starts_at,
        "ends_at": incident.ends_at,
        "status": incident.status.value,
        "note": incident.note,
        "evidence": incident.evidence,
        "created_at": incident.created_at,
        "closed_at": incident.closed_at,
    }


def action_dict(action: e.ReservationAction) -> dict:
    return {
        "code": action.code,
        "incident_code": action.incident_code,
        "reservation_code": action.reservation_code,
        "action": action.action.value,
        "impact": action.impact.value,
        "decided_at": action.decided_at,
        "effective_from": action.effective_from,
        "effective_to": action.effective_to,
        "reason": action.reason,
        "from_links": action.from_links,
        "to_links": action.to_links,
        "from_latency": _enum(action.from_latency),
        "to_latency": _enum(action.to_latency),
    }


def compensation_dict(comp: e.Compensation) -> dict:
    return {
        "code": comp.code,
        "incident_code": comp.incident_code,
        "reservation_code": comp.reservation_code,
        "tenant_code": comp.tenant_code,
        "amount": comp.amount,
        "degraded_minutes": comp.degraded_minutes,
        "down_minutes": comp.down_minutes,
        "window": [comp.window_start, comp.window_end],
        "attribution": comp.attribution,
        "version": comp.version,
        "status": comp.status.value,
        "ledger_entry_code": comp.ledger_entry_code,
        "created_at": comp.created_at,
    }


def period_dict(period: e.AccountingPeriod) -> dict:
    return {
        "period": period.period,
        "status": period.status.value,
        "closed_at": period.closed_at,
    }
