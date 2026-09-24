"""本地 HTTP API：仅依赖标准库，把 JSON 请求路由到应用服务。

启动方式：
    python3 -m industrial_capacity.interfaces.api --port 8080

接口前缀：
    /admin/*        链路、租户、维护窗口、账期、测试时钟
    /reservations/* 批量原子准入与查询
    /telemetry/*    观测接入、更正与版本审计
    /ops/*          故障/维护/退化事件与处置
    /settlement/*   结算、重放、更正重算
    /ledger         账本查询
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..application.errors import AppError
from ..application.platform import Platform
from . import serializers as sz

DEFAULT_TIME = 1_756_000_000  # 固定基准时间，便于确定性演示（2025-08-23 附近 UTC）


def _error_status(code: str) -> int:
    return {
        "NOT_FOUND": 404,
        "CONFLICT": 409,
        "PERIOD_CLOSED": 409,
        "BAD_REQUEST": 400,
        "WINDOW_INVALID": 400,
    }.get(code, 422)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "IndustrialCapacity/1.0"

    # silence default noisy logging; tests may pass log=False
    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "http_logging", False):
            super().log_message(fmt, *args)

    @property
    def platform(self) -> Platform:
        return self.server.platform  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AppError("BAD_REQUEST", "请求体不是合法 JSON")
        if not isinstance(body, dict):
            raise AppError("BAD_REQUEST", "请求体必须是 JSON 对象")
        return body

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, exc: AppError) -> None:
        self._send({"error": exc.to_dict()}, _error_status(exc.code))

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            route = urlsplit(self.path).path.rstrip("/") or "/"
            self._route_get(route)
        except AppError as exc:
            self._send_error(exc)
        except (KeyError, ValueError, TypeError) as exc:
            self._send_error(AppError("BAD_REQUEST", f"请求参数非法：{exc}"))

    def do_POST(self) -> None:  # noqa: N802
        try:
            route = urlsplit(self.path).path.rstrip("/") or "/"
            self._route_post(route)
        except AppError as exc:
            self._send_error(exc)
        except (KeyError, ValueError, TypeError) as exc:
            self._send_error(AppError("BAD_REQUEST", f"请求参数非法：{exc}"))

    # ------------------------------------------------------------------
    def _route_get(self, route: str) -> None:
        p = self.platform
        parts = [x for x in route.split("/") if x]

        if route == "/health":
            self._send({"status": "ok", "now": p.clock.now()})
        elif route == "/admin/links":
            self._send([sz.link_dict(l) for l in p.repo.list_links()])
        elif len(parts) == 3 and parts[:2] == ["admin", "links"]:
            link = p.repo.get_link(parts[2])
            if not link:
                raise AppError("NOT_FOUND", f"链路 {parts[2]} 不存在")
            self._send(sz.link_dict(link))
        elif route == "/admin/tenants":
            self._send([sz.tenant_dict(t) for t in p.repo.list_tenants()])
        elif route == "/admin/maintenance":
            link = self._query().get("link")
            self._send(
                [sz.maintenance_dict(w) for w in p.repo.list_maintenance(link)]
            )
        elif route == "/admin/periods":
            self._send([sz.period_dict(x) for x in p.repo.list_periods()])
        elif route == "/reservations":
            tenant = self._query().get("tenant")
            self._send(
                [sz.reservation_dict(r) for r in p.repo.list_reservations(tenant)]
            )
        elif len(parts) == 3 and parts[:2] == ["reservations", "batches"]:
            self._send(sz.batch_dict(p.admission.get_batch(parts[2])))
        elif len(parts) == 3 and parts[0] == "telemetry" and parts[2] == "versions":
            q = self._query()
            bucket = int(q["bucket_ts"]) if "bucket_ts" in q else None
            if bucket is None:
                raise AppError("BAD_REQUEST", "缺少 bucket_ts 查询参数")
            self._send(
                [sz.telemetry_dict(s) for s in p.telemetry.list_versions(parts[1], bucket)]
            )
        elif len(parts) == 2 and parts[0] == "telemetry":
            self._send([sz.telemetry_dict(s) for s in p.telemetry.list_current(parts[1])])
        elif route == "/ops/incidents":
            self._send([sz.incident_dict(i) for i in p.repo.list_incidents()])
        elif route == "/ops/actions":
            q = self._query()
            self._send(
                [
                    sz.action_dict(a)
                    for a in p.repo.list_actions(
                        q.get("incident"), q.get("reservation")
                    )
                ]
            )
        elif route == "/ledger":
            q = self._query()
            self._send(p.settlement.ledger_report(q.get("period"), q.get("tenant")))
        elif route == "/settlement/compensations":
            self._send(
                [
                    sz.compensation_dict(c)
                    for c in (
                        p.repo.list_compensations(self._query().get("incident"))
                    )
                ]
            )
        else:
            raise AppError("NOT_FOUND", f"无此接口：GET {route}")

    def _route_post(self, route: str) -> None:
        p = self.platform
        body = self._read_json()
        parts = [x for x in route.split("/") if x]

        if route == "/admin/links":
            link = p.catalog.register_link(
                code=body["code"],
                name=body.get("name", body["code"]),
                total_capacity_mbps=float(body["total_capacity_mbps"]),
                supported_latency=body["supported_latency"],
                reliability_target=float(body["reliability_target"]),
                alternative_codes=body.get("alternative_codes", []),
            )
            self._send(sz.link_dict(link), 201)
        elif len(parts) == 4 and parts[:2] == ["admin", "links"] and parts[3] == "status":
            link = p.catalog.set_link_status(parts[2], body["status"])
            self._send(sz.link_dict(link))
        elif route == "/admin/tenants":
            tenant = p.catalog.register_tenant(
                code=body["code"],
                name=body.get("name", body["code"]),
                quota_mbps=float(body["quota_mbps"]),
            )
            self._send(sz.tenant_dict(tenant), 201)
        elif route == "/admin/maintenance":
            window = p.catalog.schedule_maintenance(
                link_code=body["link_code"],
                starts_at=int(body["starts_at"]),
                ends_at=int(body["ends_at"]),
                note=body.get("note", ""),
            )
            self._send(sz.maintenance_dict(window), 201)
        elif route == "/admin/periods/close":
            period = p.catalog.close_period(body["period"])
            self._send(sz.period_dict(period))
        elif route == "/admin/clock/advance":
            now = p.clock.advance(int(body.get("seconds", 0)))
            self._send({"now": now})
        elif route == "/admin/clock/set":
            p.clock.set(int(body["ts"]))
            self._send({"now": p.clock.now()})
        elif route == "/reservations/batches":
            result = p.admission.submit_batch(body["items"])
            status = 201 if result.status == "ACCEPTED" else 409
            self._send(sz.batch_dict(result), status)
        elif len(parts) == 2 and parts[0] == "telemetry":
            sample = p.telemetry.ingest(
                link_code=parts[1],
                bucket_ts=int(body["bucket_ts"]),
                observed_latency_ms=body.get("observed_latency_ms"),
                observed_reliability=body.get("observed_reliability"),
            )
            self._send(sz.telemetry_dict(sample), 201)
        elif len(parts) == 3 and parts[0] == "telemetry" and parts[2] == "correct":
            sample = p.telemetry.correct(
                link_code=parts[1],
                bucket_ts=int(body["bucket_ts"]),
                observed_latency_ms=body.get("observed_latency_ms"),
                observed_reliability=body.get("observed_reliability"),
            )
            self._send(sz.telemetry_dict(sample))
        elif route == "/ops/failures":
            incident = p.operations.report_failure(
                link_code=body["link_code"],
                starts_at=int(body["starts_at"]),
                ends_at=body.get("ends_at"),
                note=body.get("note", ""),
            )
            self._send(sz.incident_dict(incident), 201)
        elif route == "/ops/maintenance/activate":
            incidents = p.operations.activate_due_maintenance(
                int(body["at"]) if "at" in body else None
            )
            self._send([sz.incident_dict(i) for i in incidents], 201)
        elif route == "/ops/degradation/detect":
            out = p.operations.detect_degradation(
                link_code=body["link_code"],
                window_start=int(body["window_start"]),
                window_end=int(body["window_end"]),
            )
            self._send(out, 201)
        elif len(parts) == 4 and parts[:2] == ["ops", "incidents"] and parts[3] == "close":
            incident = p.operations.close_incident(
                parts[2], int(body["ends_at"]) if "ends_at" in body else None
            )
            self._send(sz.incident_dict(incident))
        elif route == "/ops/expire":
            codes = p.operations.expire_reservations(
                int(body["now"]) if "now" in body else None
            )
            self._send({"completed": codes})
        elif (
            len(parts) == 4
            and parts[:2] == ["settlement", "incidents"]
            and parts[3] == "settle"
        ):
            self._send(p.settlement.settle_incident(parts[2]))
        elif route == "/settlement/settle-all":
            self._send(p.settlement.settle_all())
        elif route == "/settlement/replay":
            self._send(p.settlement.replay())
        elif route == "/settlement/recompute":
            out = p.settlement.recompute_after_correction(
                link_code=body["link_code"],
                window_start=int(body["window_start"]),
                window_end=int(body["window_end"]),
            )
            self._send(out)
        else:
            raise AppError("NOT_FOUND", f"无此接口：POST {route}")


def create_server(host: str = "127.0.0.1", port: int = 0, platform: Platform | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.platform = platform or Platform(DEFAULT_TIME)  # type: ignore[attr-defined]
    server.http_logging = False  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="工业专网能力预约与违约归因平台 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = create_server(args.host, args.port)
    host, port = server.server_address[:2]
    print(f"平台 API 已启动：http://{host}:{port} （基准时钟 {DEFAULT_TIME}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
