"""HTTP API + 静态页。仅依赖 Python 标准库。"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from . import engine, planner, seed, support
from .database import connect, version_bump, jloads

ROOT = os.path.dirname(os.path.dirname(__file__))
WEB = os.path.join(ROOT, "web")
conn = connect()
if conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"] == 0:
    seed.seed(conn)

DEFAULT_NOW = "2026-10-07T10:00"


def person_from(q):
    return {"wheelchair": q.get("wheelchair") in ("1", "true", "on"),
            "elderly": q.get("elderly") in ("1", "true", "on"),
            "child_age": int(q["child_age"]) if q.get("child_age") else None}


def seg_payload(conn, key):
    row, poly = engine.segment_polyline(conn, key)
    return {"seg_key": key, "name": row["name"], "from": row["from_node"],
            "to": row["to_node"], "geometry": poly,
            "attrs": jloads(row["attrs"], {}),
            "service_weekly": jloads(row["service_weekly"], None)}


def rule_payload(conn, r):
    return {"id": r["id"], "rule_id": r["id"], "rule_key": r["rule_key"], "ann_id": r["ann_id"],
            "version": r["version"], "status": r["status"], "effect": r["effect"],
            "rule_type": r["rule_type"], "title": r["title"],
            "geom_type": r["geom_type"], "geometry": jloads(r["geometry"], []),
            "direction": r["direction"], "windows": jloads(r["windows"], None),
            "weekly": jloads(r["weekly"], None), "min_age": r["min_age"],
            "population": r["population"], "observed_at": r["observed_at"],
            "matches": [{"seg_key": m["seg_key"], "point_only": bool(m["point_only"]),
                         "hit": jloads(m["hit"], [])}
                        for m in conn.execute(
                            "SELECT * FROM rule_matches WHERE rule_id=?",
                            (r["id"],))]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype="application/json; charset=utf-8"):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if p == "/" or p == "/index.html":
                return self._static("index.html", "text/html; charset=utf-8")
            if p.startswith("/web/"):
                return self._static(p[5:])
            if p == "/api/state":
                return self._state()
            if p == "/api/impact":
                return self._impact(q)
            if p == "/api/plan":
                return self._plan(q)
            if p == "/api/freshness":
                return self._send(support.freshness(conn, q.get("at", DEFAULT_NOW)))
            if p == "/api/missing":
                return self._send(support.missing_info(conn, q.get("at", DEFAULT_NOW)))
            if p == "/api/ack":
                return self._send(support.ack_status(
                    conn, q.get("user", "u1"), int(q["rule_id"])))
            if p == "/api/offline":
                return self._send(support.get_offline_plan(conn, q["plan_id"]) or {},
                                  200)
            if p == "/api/print":
                return self._print(q)
            self._send({"error": "not found"}, 404)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self._send({"error": str(ex)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0))
        except json.JSONDecodeError:
            body = {}
        try:
            if u.path == "/api/announce":
                ann, rl = engine.ingest_announcement(conn, body)
                return self._send({"announcement": dict(ann),
                                   "rule": rule_payload(conn, rl)})
            if u.path == "/api/revoke":
                engine.revoke_announcement(conn, body["ann_id"],
                                           body["revoked_at"], body["observed_at"])
                return self._send({"ok": True})
            if u.path == "/api/reroute":
                v = support.add_route_version(
                    conn, body["route_id"], body["name"], body.get("kind", "步道"),
                    body.get("note", ""), body["members"], body["valid_from"])
                return self._send({"ok": True, "version": v})
            if u.path == "/api/ack":
                return self._send(support.acknowledge(
                    conn, body.get("user", "u1"), int(body["rule_id"]),
                    body.get("at", DEFAULT_NOW)))
            if u.path == "/api/offline":
                plan = self._offline_snapshot(body)
                return self._send(plan)
            if u.path == "/api/reconnect":
                res = support.reconnect(conn, body["plan_id"], body["at"],
                                        body.get("person", {}))
                return self._send(res or {"error": "plan not found"},
                                  200 if res else 404)
            if u.path == "/api/reset":
                reset_server()
                return self._send({"ok": True})
            self._send({"error": "not found"}, 404)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self._send({"error": str(ex)}, 500)

    def _static(self, name, ctype=None):
        path = os.path.normpath(os.path.join(WEB, name))
        if not path.startswith(WEB) or not os.path.isfile(path):
            return self._send({"error": "not found"}, 404)
        ctype = ctype or ("application/javascript; charset=utf-8"
                          if name.endswith(".js") else
                          "text/css; charset=utf-8" if name.endswith(".css")
                          else "text/plain; charset=utf-8")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _state(self):
        nodes = [dict(r) for r in conn.execute("SELECT * FROM nodes")]
        segs = [seg_payload(conn, r["seg_key"])
                for r in conn.execute("SELECT DISTINCT seg_key FROM segments")]
        routes = []
        for r in conn.execute("SELECT * FROM routes ORDER BY route_id,version"):
            members = [dict(m) for m in conn.execute(
                "SELECT * FROM route_members WHERE route_id=? AND version=? ORDER BY seq",
                (r["route_id"], r["version"]))]
            d = dict(r)
            d["members"] = members
            routes.append(d)
        rules = [rule_payload(conn, r)
                 for r in conn.execute("SELECT * FROM rules ORDER BY id")]
        anns = [dict(a) for a in conn.execute("SELECT * FROM announcements")]
        return self._send({"nodes": nodes, "segments": segs, "routes": routes,
                           "rules": rules, "announcements": anns,
                           "version": version_bump(conn)})

    def _impact(self, q):
        at = q.get("at", DEFAULT_NOW)
        oa = q.get("observed_at", at)
        person = person_from(q)
        version = int(q["version"]) if q.get("version") else None
        res = engine.evaluate_route(conn, q["route_id"], at, person,
                                    version=version, observed_at=oa)
        seg_meta = {s["seg_key"]: s for s in
                    [seg_payload(conn, m["seg_key"])
                     for m in conn.execute("SELECT DISTINCT seg_key FROM segments")]}
        for e in res["edges"]:
            e["name"] = seg_meta[e["seg_key"]]["name"]
            e["service"] = engine.edge_state(
                conn, e["seg_key"], e["reversed"], at, person,
                engine.visible_rules(conn, at, oa))["service"]
            for r in e["reasons"]:
                if r.get("ann_id"):
                    a = conn.execute("SELECT * FROM announcements WHERE ann_id=?",
                                     (r["ann_id"],)).fetchone()
                    r["source"] = a["source"]
                    r["source_level"] = a["source_level"]
        return self._send(res)

    def _plan(self, q):
        at = q.get("at", DEFAULT_NOW)
        person = person_from(q)
        mode = q.get("mode", "prune")
        if mode == "filter":
            res = planner.plan_filter(conn, q["start"], q["goal"], at, person)
        else:
            res = planner.plan_prune(conn, q["start"], q["goal"], at, person)
        return self._send(res)

    def _offline_snapshot(self, body):
        at = body["at"]
        person = body.get("person", {})
        result = planner.plan_prune(conn, body["start"], body["goal"], at, person)
        routes = [{"route_id": r["route_id"], "version": r["version"]}
                  for r in conn.execute(
                      "SELECT route_id,version FROM routes WHERE current=1")]
        rules = [{"rule_key": r["rule_key"], "version": r["version"],
                  "ann_id": r["ann_id"], "title": r["title"]}
                 for r in engine.active_rules(conn, at)]
        payload = {"start": body["start"], "goal": body["goal"],
                   "person": person, "routes_snapshot": routes,
                   "rules_snapshot": rules, "plan": result,
                   "freshness": support.freshness(conn, at),
                   "missing": support.missing_info(conn, at)}
        pid = support.save_offline_plan(conn, body.get("user", "u1"), at, payload)
        return {"plan_id": pid, "payload": payload}

    def _print(self, q):
        at = q.get("at", DEFAULT_NOW)
        person = person_from(q)
        if q.get("plan_id"):
            plan = support.get_offline_plan(conn, q["plan_id"])
            result = plan["payload"]["plan"]
        elif q.get("mode") == "filter":
            result = planner.plan_filter(conn, q["start"], q["goal"], at, person)
        else:
            result = planner.plan_prune(conn, q["start"], q["goal"], at, person)
        out = support.print_plan(conn, result, at, person)
        if q.get("format") == "text":
            return self._send(out["text"], 200, "text/plain; charset=utf-8")
        return self._send(out)


def reset_server():
    global conn
    conn = _reseed()


def _reseed():
    conn = connect()
    conn.executescript(
        "DELETE FROM acknowledgements;DELETE FROM offline_plans;DELETE FROM rule_matches;"
        "DELETE FROM rules;DELETE FROM announcements;DELETE FROM route_members;"
        "DELETE FROM routes;DELETE FROM segments;DELETE FROM nodes;")
    conn.commit()
    seed.seed(conn)
    return conn


def main(port=8080):
    httpd = HTTPServer(("0.0.0.0", port), Handler)
    print(f"路线提醒编排器: http://localhost:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8080)
