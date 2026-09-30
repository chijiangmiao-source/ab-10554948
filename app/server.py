"""星载参数库 MVCC 可串行化审计 HTTP 服务（Python 标准库，零第三方依赖）。

路由：
  GET  /                     审计页面（草稿编辑器 + 冻结结论展示）
  GET  /healthz              健康检查
  POST /api/audits           提交/重传审计载荷，冻结结论
  GET  /api/audits/<id>      读取已冻结结论
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from . import __version__, core
from .store import FrozenStore

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "audits.db"))
MAX_BODY = 4 * 1024 * 1024

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

_store: FrozenStore | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "ParamMVCCAudit/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        thread = threading.current_thread().name
        print(f"[{thread}] {self.address_string()} {fmt % args}", flush=True)

    # -- helpers ----------------------------------------------------------
    def _send_json(self, obj: object, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_html_file(self, filename: str) -> None:
        path = os.path.join(STATIC_DIR, filename)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json({"error": "页面缺失"}, status=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- GET --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/":
            self._send_html_file("index.html")
        elif parts.path == "/healthz":
            self._send_json({"status": "ok", "service": "param-mvcc-audit", "version": __version__})
        elif parts.path.startswith("/api/audits/"):
            audit_id = unquote(parts.path[len("/api/audits/"):])
            if not audit_id or "/" in audit_id:
                self._send_json({"error": "审计标识不合法"}, status=400)
                return
            assert _store is not None
            conclusion = _store.get(audit_id)
            if conclusion is None:
                self._send_json({"error": "未找到该审计标识的冻结记录", "audit_id": audit_id}, status=404)
            else:
                self._send_json({"frozen": True, "outcome": "stored", "conclusion": conclusion})
        else:
            self._send_json({"error": "未知路径", "path": parts.path}, status=404)

    # -- POST -------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path != "/api/audits":
            self._send_json({"error": "未知路径", "path": parts.path}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json({"error": "请求体为空或超过 4MiB 限制"}, status=413)
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json({"error": f"请求体不是合法 JSON：{exc}"}, status=400)
            return

        try:
            conclusion = core.analyze(payload)
        except core.PayloadShapeError as exc:
            # 结构错误属于无法冻结的输入错误，不写存储
            self._send_json({"error": str(exc)}, status=400)
            return

        assert _store is not None
        outcome, stored = _store.submit(conclusion["audit_id"], payload, conclusion)
        if outcome == "conflict":
            self._send_json(
                {
                    "error": "该审计标识已冻结且本次载荷与原载荷不同，拒绝覆盖；如需重新审计请使用新的审计标识。",
                    "audit_id": conclusion["audit_id"],
                    "frozen_record_hint": f"/api/audits/{conclusion['audit_id']}",
                },
                status=409,
            )
            return
        self._send_json(
            {"frozen": True, "outcome": outcome, "conclusion": stored},
            status=200,
        )


def main() -> None:
    global _store
    _store = FrozenStore(DB_PATH)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"星载参数库 MVCC 审计服务监听 http://{HOST}:{PORT} ，数据库 {DB_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _store.close()


if __name__ == "__main__":
    main()
