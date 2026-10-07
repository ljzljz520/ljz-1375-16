"""HTTP 服务：管理端/用户端 API + 静态页面。仅依赖标准库。

启动：python3 -m server.app [--port 8000] [--db data/app.db]
"""
from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core, db as dbmod, seed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("ALERTS_DB", os.path.join(ROOT, "data", "app.db"))


def get_conn():
    if not os.path.exists(DB_PATH):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = dbmod.connect(DB_PATH)
        seed.build(conn)
    return dbmod.connect(DB_PATH)


class Handler(BaseHTTPRequestHandler):
    server_version = "RouteAlertOrchestrator/1.0"

    # ---------- 工具 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in q.items()}

    def log_message(self, *a):  # 静默
        pass

    # ---------- 路由 ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        q = self._query()
        conn = get_conn()
        try:
            if path == "/api/graph":
                return self._json({
                    "places": [dict(r) for r in conn.execute("SELECT * FROM places")],
                    "routes": [dict(r) for r in conn.execute("SELECT * FROM routes")],
                    "segments": core.current_segments(conn),
                })
            if path == "/api/places":
                return self._json([dict(r) for r in conn.execute("SELECT * FROM places")])
            if path == "/api/alerts":
                revs = core.latest_revisions(conn)
                alerts = core.alerts_map(conn)
                out = []
                for aid, a in alerts.items():
                    r = revs[aid]
                    out.append({**a, "rev": r["rev"], "revoked": bool(r["revoked"]),
                                "direction": r["direction"], "window": core.window_text(r),
                                "rule": json.loads(r["rule"] or "{}"), "note": r["note"],
                                "effective_at": r["effective_at"], "received_at": r["received_at"]})
                return self._json(out)
            if path == "/api/impacts":
                return self._json(core.compute_impacts(conn, q.get("at")))
            if path == "/api/conflicts":
                return self._json([dict(r) for r in conn.execute(
                    "SELECT * FROM conflicts ORDER BY id DESC")])
            m = re.fullmatch(r"/api/plans/([\w]+)/refresh", path)
            if m:
                res = core.refresh_plan(conn, m.group(1))
                return self._json(res or {"error": "plan not found"}, 200 if res else 404)
            m = re.fullmatch(r"/api/plans/([\w]+)", path)
            if m:
                p = core.get_plan(conn, m.group(1))
                if not p:
                    return self._json({"error": "plan not found"}, 404)
                return self._json({**p, "freshness": core.freshness(conn),
                                   "missing_info": core.missing_info(conn),
                                   "acks": core.ack_status(conn, p["user_id"])})
            return self._static(path)
        finally:
            conn.close()

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        conn = get_conn()
        try:
            if path == "/api/plan":
                res = core.plan(conn, body)
                return self._json(res)
            if path == "/api/alerts":
                aid = core.create_alert(
                    conn, body.get("kind", "construction"), body.get("source", "管理端"),
                    body.get("title", "未命名"), body.get("geom"),
                    body.get("direction", "both"), body.get("starts_at"), body.get("ends_at"),
                    body.get("daily_start"), body.get("daily_end"),
                    body.get("rule") or {}, body.get("note", ""),
                    body.get("effective_at"), body.get("received_at"))
                return self._json({"alert_id": aid})
            m = re.fullmatch(r"/api/alerts/([\w]+)/revisions", path)
            if m:
                rev = core.add_revision(
                    conn, m.group(1), body.get("geom"), body.get("direction", "both"),
                    body.get("starts_at"), body.get("ends_at"), body.get("daily_start"),
                    body.get("daily_end"), body.get("rule") or {}, body.get("note", ""),
                    body.get("effective_at"), body.get("received_at"),
                    bool(body.get("revoked")))
                core.detect_conflicts(conn)
                return self._json({"rev": rev})
            m = re.fullmatch(r"/api/alerts/([\w]+)/revoke", path)
            if m:
                rev = core.revoke_alert(conn, m.group(1), body.get("effective_at") or core.now_iso(),
                                        body.get("received_at"), body.get("note", "撤销"))
                return self._json({"rev": rev})
            m = re.fullmatch(r"/api/alerts/([\w]+)/ack", path)
            if m:
                ok, msg = core.ack(conn, body.get("user_id", "anon"), m.group(1),
                                   int(body.get("rev", 0)))
                return self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            if path == "/api/reports":
                conn.execute(
                    "INSERT INTO reports(source,segment_id,direction,starts_at,ends_at,state,received_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (body.get("source", "未知来源"), body["segment_id"],
                     body.get("direction", "both"), body.get("starts_at"), body.get("ends_at"),
                     body["state"], body.get("received_at") or core.now_iso()))
                conn.commit()
                added = core.detect_conflicts(conn)
                return self._json({"ok": True, "new_conflicts": added})
            m = re.fullmatch(r"/api/routes/([\w]+)/reroute", path)
            if m:
                v = core.reroute(conn, m.group(1), body["segments"])
                return self._json({"version": v})
            return self._json({"error": "not found"}, 404)
        except KeyError as e:
            return self._json({"error": f"缺少字段 {e}"}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 500)
        finally:
            conn.close()

    def _static(self, path):
        if path in ("/", ""):
            path = "/planner.html"
        fp = os.path.normpath(os.path.join(ROOT, path.lstrip("/")))
        if not fp.startswith(ROOT) or not os.path.isfile(fp):
            self.send_error(404)
            return
        ctype = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
                 ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml"
                 }.get(os.path.splitext(fp)[1], "application/octet-stream")
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    port = 8000
    args = sys.argv[1:]
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    if "--db" in args:
        global DB_PATH
        DB_PATH = args[args.index("--db") + 1]
    get_conn().close()  # 确保建库+种子
    print(f"路线提醒编排器已启动: http://127.0.0.1:{port}/  (管理端 /admin.html)")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()





if __name__ == "__main__":
    main()
