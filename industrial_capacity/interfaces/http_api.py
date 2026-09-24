"""HTTP 接口层:基于标准库的本地 JSON API。

仅负责协议解析、参数校验与领域对象序列化;业务规则全部在应用/领域层。
错误统一映射为 ``{"error": {"code", "message", "details"}}``。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ..domain.errors import ConflictError, DomainError, NotFoundError, ValidationError
from ..domain.models import (
    LatencyClass,
    MaintenanceKind,
    ReservationItem,
    TelemetrySample,
)
from ..domain.timeutil import format_instant, parse_instant

Json = dict[str, Any]


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def link_to_json(link) -> Json:
    return {
        "link_id": link.link_id,
        "name": link.name,
        "capacity_mbps": link.capacity_mbps,
        "latency_class": link.latency_class.value,
        "reliability_target": link.reliability_target,
        "workshops": list(link.workshops),
        "active": link.active,
    }


def quota_to_json(quota) -> Json:
    return {
        "tenant_id": quota.tenant_id,
        "max_mbps": quota.max_mbps,
        "max_active_reservations": quota.max_active_reservations,
    }


def maintenance_to_json(window) -> Json:
    return {
        "window_id": window.window_id,
        "link_id": window.link_id,
        "start": format_instant(window.start),
        "end": format_instant(window.end),
        "kind": window.kind.value,
        "available_mbps": window.available_mbps,
    }


def reservation_to_json(reservation) -> Json:
    return {
        "reservation_id": reservation.reservation_id,
        "batch_id": reservation.batch_id,
        "tenant_id": reservation.tenant_id,
        "link_id": reservation.link_id,
        "start": format_instant(reservation.start),
        "end": format_instant(reservation.end),
        "bandwidth_mbps": reservation.bandwidth_mbps,
        "latency_class": reservation.latency_class.value,
        "business_priority": reservation.business_priority,
        "status": reservation.status.value,
        "terminated_at": (
            format_instant(reservation.terminated_at) if reservation.terminated_at else None
        ),
        "version": reservation.version,
    }


def batch_to_json(batch) -> Json:
    return {
        "batch_id": batch.batch_id,
        "tenant_id": batch.tenant_id,
        "status": batch.status.value,
        "reservation_ids": list(batch.reservation_ids),
        "rejections": [
            {"item_id": r.item_id, "code": r.code, "message": r.message}
            for r in batch.rejections
        ],
    }


def window_to_json(window) -> Json:
    return {
        "window_id": window.window_id,
        "link_id": window.link_id,
        "start": format_instant(window.start),
        "end": format_instant(window.end),
        "committed_mbps": window.committed_mbps,
        "worst_available_mbps": window.worst_available_mbps,
        "service_ratio": round(window.service_ratio, 6),
        "attribution": window.attribution.value,
        "affected": [
            {
                "reservation_id": a.reservation_id,
                "tenant_id": a.tenant_id,
                "bandwidth_mbps": a.bandwidth_mbps,
                "business_priority": a.business_priority,
            }
            for a in window.affected
        ],
    }


def action_to_json(action) -> Json:
    return {
        "action_id": action.action_id,
        "reservation_id": action.reservation_id,
        "trigger": action.trigger.value,
        "kind": action.kind.value,
        "detail": action.detail,
        "created_at": format_instant(action.created_at),
    }


def entry_to_json(entry) -> Json:
    return {
        "entry_id": entry.entry_id,
        "tenant_id": entry.tenant_id,
        "reservation_id": entry.reservation_id,
        "period": entry.period,
        "kind": entry.kind.value,
        "credit_mb_minutes": entry.credit_mb_minutes,
        "window_start": format_instant(entry.window_start),
        "window_end": format_instant(entry.window_end),
        "attribution": entry.attribution.value if entry.attribution else None,
        "target_period": entry.target_period,
        "dedup_key": entry.dedup_key,
    }


def period_to_json(record) -> Json:
    return {
        "period": record.period,
        "status": record.status.value,
        "run_count": record.run_count,
        "sealed_at": format_instant(record.sealed_at) if record.sealed_at else None,
    }


# ---------------------------------------------------------------------------
# 请求解析
# ---------------------------------------------------------------------------
def _required(body: Json, field: str):
    value = body.get(field)
    if value is None:
        raise ValidationError(f"缺少必填字段 {field}")
    return value


def _parse_latency(value: str) -> LatencyClass:
    try:
        return LatencyClass(value)
    except ValueError:
        raise ValidationError(
            f"未知时延等级 {value!r},可选: {[c.value for c in LatencyClass]}"
        ) from None


def _parse_maintenance_kind(value: str) -> MaintenanceKind:
    try:
        return MaintenanceKind(value)
    except ValueError:
        raise ValidationError(
            f"未知维护类型 {value!r},可选: {[k.value for k in MaintenanceKind]}"
        ) from None


def _parse_item(raw: Json, batch_id: str, index: int) -> ReservationItem:
    return ReservationItem(
        item_id=str(raw.get("item_id") or f"{batch_id}:{index}"),
        link_id=str(_required(raw, "link_id")),
        start=parse_instant(str(_required(raw, "start"))),
        end=parse_instant(str(_required(raw, "end"))),
        bandwidth_mbps=int(_required(raw, "bandwidth_mbps")),
        latency_class=_parse_latency(str(_required(raw, "latency_class"))),
        business_priority=int(raw.get("business_priority", 0)),
    )


def _parse_sample(raw: Json, *, correction: bool) -> TelemetrySample:
    return TelemetrySample(
        sample_id=str(_required(raw, "sample_id")),
        link_id=str(_required(raw, "link_id")),
        ts=parse_instant(str(_required(raw, "ts"))),
        available_mbps=int(_required(raw, "available_mbps")),
        latency_ms=float(raw.get("latency_ms", 0.0)),
        loss_ratio=float(raw.get("loss_ratio", 0.0)),
        corrects_sample_id=(
            str(_required(raw, "corrects_sample_id")) if correction else None
        ),
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
Handler = Callable[[Json, dict[str, str], dict[str, str]], tuple[int, Json]]


class _Route:
    def __init__(self, method: str, pattern: str, handler: Handler) -> None:
        self.method = method
        self.regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self.handler = handler


def build_routes(app) -> list[_Route]:
    def routes() -> list[_Route]:
        def create_link(body, path, query):
            link = app.catalog.create_link(
                name=str(_required(body, "name")),
                capacity_mbps=int(_required(body, "capacity_mbps")),
                latency_class=_parse_latency(str(_required(body, "latency_class"))),
                reliability_target=float(_required(body, "reliability_target")),
                workshops=tuple(body.get("workshops", [])),
                link_id=body.get("link_id"),
            )
            return 201, link_to_json(link)

        def list_links(body, path, query):
            return 200, {"links": [link_to_json(l) for l in app.catalog.list_links()]}

        def set_quota(body, path, query):
            quota = app.catalog.set_quota(
                tenant_id=path["tenant_id"],
                max_mbps=int(_required(body, "max_mbps")),
                max_active_reservations=int(_required(body, "max_active_reservations")),
            )
            return 200, quota_to_json(quota)

        def create_maintenance(body, path, query):
            window, actions = app.catalog.create_maintenance_window(
                link_id=path["link_id"],
                start=parse_instant(str(_required(body, "start"))),
                end=parse_instant(str(_required(body, "end"))),
                kind=_parse_maintenance_kind(str(_required(body, "kind"))),
                available_mbps=int(body.get("available_mbps", 0)),
                window_id=body.get("window_id"),
            )
            return 201, {
                **maintenance_to_json(window),
                "remediation_actions": [action_to_json(a) for a in actions],
            }

        def list_maintenance(body, path, query):
            windows = app.catalog.list_maintenance_windows(query.get("link_id"))
            return 200, {"maintenance_windows": [maintenance_to_json(w) for w in windows]}

        def submit_batch(body, path, query):
            tenant_id = str(_required(body, "tenant_id"))
            batch_id = body.get("batch_id") or app.id_gen.new_id("batch")
            raw_items = _required(body, "items")
            if not isinstance(raw_items, list):
                raise ValidationError("items 必须为数组")
            items = [_parse_item(raw, batch_id, i) for i, raw in enumerate(raw_items)]
            batch = app.admission.submit_batch(
                tenant_id=tenant_id, items=items, batch_id=batch_id
            )
            return (201 if batch.status.value == "CONFIRMED" else 200), batch_to_json(batch)

        def get_batch(body, path, query):
            return 200, batch_to_json(app.admission.get_batch(path["batch_id"]))

        def get_reservation(body, path, query):
            return 200, reservation_to_json(app.admission.get_reservation(path["reservation_id"]))

        def list_reservations(body, path, query):
            reservations = app.admission.list_reservations(query.get("tenant_id"))
            return 200, {"reservations": [reservation_to_json(r) for r in reservations]}

        def ingest_samples(body, path, query):
            raw = _required(body, "samples")
            samples = [_parse_sample(r, correction=False) for r in raw]
            return 200, app.telemetry.ingest_samples(samples)

        def correct_samples(body, path, query):
            raw = _required(body, "corrections")
            corrections = [_parse_sample(r, correction=True) for r in raw]
            return 200, app.telemetry.correct_samples(corrections)

        def list_windows(body, path, query):
            windows = app.telemetry.list_windows(query.get("link_id"))
            return 200, {"breach_windows": [window_to_json(w) for w in windows]}

        def list_actions(body, path, query):
            actions = app.remediation.list_actions(query.get("reservation_id"))
            return 200, {"remediation_actions": [action_to_json(a) for a in actions]}

        def run_settlement(body, path, query):
            return 200, app.settlement.run(path["period"])

        def seal_period(body, path, query):
            return 200, period_to_json(app.settlement.seal(path["period"]))

        def list_periods(body, path, query):
            return 200, {"periods": [period_to_json(p) for p in app.settlement.list_periods()]}

        def list_ledger(body, path, query):
            entries = app.settlement.list_ledger(
                tenant_id=query.get("tenant_id"), period=query.get("period")
            )
            return 200, {"ledger": [entry_to_json(e) for e in entries]}

        def health(body, path, query):
            return 200, {"status": "ok", "service": "industrial_capacity"}

        return [
            _Route("GET", "/health", health),
            _Route("POST", "/links", create_link),
            _Route("GET", "/links", list_links),
            _Route("PUT", "/tenants/{tenant_id}/quota", set_quota),
            _Route("POST", "/links/{link_id}/maintenance-windows", create_maintenance),
            _Route("GET", "/maintenance-windows", list_maintenance),
            _Route("POST", "/reservation-batches", submit_batch),
            _Route("GET", "/reservation-batches/{batch_id}", get_batch),
            _Route("GET", "/reservations/{reservation_id}", get_reservation),
            _Route("GET", "/reservations", list_reservations),
            _Route("POST", "/telemetry/samples", ingest_samples),
            _Route("POST", "/telemetry/corrections", correct_samples),
            _Route("GET", "/breach-windows", list_windows),
            _Route("GET", "/remediation-actions", list_actions),
            _Route("POST", "/settlements/{period}/run", run_settlement),
            _Route("POST", "/settlements/{period}/seal", seal_period),
            _Route("GET", "/settlements", list_periods),
            _Route("GET", "/ledger", list_ledger),
        ]

    return routes()


def make_handler(app) -> type[BaseHTTPRequestHandler]:
    routes = build_routes(app)

    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "IndustrialCapacity/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:  # 静默访问日志
            return

        # ---- 基础工具 ----
        def _send_json(self, status: int, payload: Json) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> Json:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValidationError(f"请求体不是合法 JSON: {exc}") from None
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        def _dispatch(self, method: str) -> None:
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            for route in routes:
                if route.method != method:
                    continue
                match = route.regex.match(parsed.path)
                if not match:
                    continue
                try:
                    body = self._read_body() if method in ("POST", "PUT") else {}
                    status, payload = route.handler(body, match.groupdict(), query)
                except DomainError as exc:
                    status = _error_status(exc)
                    payload = {
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            "details": exc.details,
                        }
                    }
                except (ValueError, TypeError) as exc:
                    status = 400
                    payload = {
                        "error": {"code": "VALIDATION", "message": str(exc), "details": {}}
                    }
                self._send_json(status, payload)
                return
            self._send_json(
                404, {"error": {"code": "NOT_FOUND", "message": "路由不存在", "details": {}}}
            )

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

    return ApiHandler


def _error_status(exc: DomainError) -> int:
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, ConflictError):
        return 409
    if isinstance(exc, ValidationError):
        return 400
    return 400


def make_server(app, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """构建线程化 HTTP 服务;并发预约由应用层锁保证原子判定。"""
    return ThreadingHTTPServer((host, port), make_handler(app))
