"""核心逻辑：时空影响计算、双策略规划、无解原因集、冲突待核、版本化确认。"""
from __future__ import annotations

import heapq
import json
import uuid
from datetime import datetime, timedelta

from . import geometry

SPEED_MPS = 1.2  # 步行速度 m/s
STALE_HOURS = 24  # 来源超过此时长未上报视为"缺信息"


# ---------- 时间工具（生效时段参与判断，支持跨日） ----------

def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat()


def parse(t):
    return datetime.fromisoformat(t) if t else None


def hm_of(t):
    return t.strftime("%H:%M")


def in_daily(hms, ds, de):
    """每日窗口，ds>de 表示跨日（如 22:00-06:00）。"""
    if ds <= de:
        return ds <= hms <= de
    return hms >= ds or hms <= de


def rev_active_at(rev, t):
    """修订在时刻 t 是否生效：绝对窗口 + 每日窗口（可跨日）同时满足。"""
    if rev["revoked"]:
        return False
    s, e = parse(rev["starts_at"]), parse(rev["ends_at"])
    if s and t < s:
        return False
    if e and t > e:
        return False
    if rev["daily_start"] and rev["daily_end"]:
        if not in_daily(hm_of(t), rev["daily_start"], rev["daily_end"]):
            return False
    return True


def window_text(rev):
    parts = []
    if rev["starts_at"] or rev["ends_at"]:
        parts.append(f"{rev['starts_at'] or '…'} ~ {rev['ends_at'] or '…'}")
    if rev["daily_start"] and rev["daily_end"]:
        parts.append(f"每日 {rev['daily_start']}-{rev['daily_end']}"
                     + ("（跨日）" if rev["daily_start"] > rev["daily_end"] else ""))
    return "；".join(parts) or "长期"


# ---------- 数据读取 ----------

def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def current_segments(conn):
    """只取各路线的当前版本片段（改线后旧版不参与影响计算）。"""
    return _rows(conn, """
        SELECT s.* FROM segments s
        JOIN routes r ON r.id = s.route_id AND r.version = s.route_version""")


def latest_revisions(conn):
    out = {}
    for r in _rows(conn, "SELECT * FROM alert_revisions ORDER BY rev"):
        out[r["alert_id"]] = r
    return out


def alerts_map(conn):
    return {a["id"]: a for a in _rows(conn, "SELECT * FROM alerts")}


# ---------- 规则与人群约束 ----------

TAGS = [("elderly", "elderly"), ("children", "stroller"), ("wheelchair", "wheelchair")]

LIMITS = {"wheelchair": {"slope": 6, "width": 0.9}, "elderly": {"slope": 8}}
TAG_NAME = {"wheelchair": "轮椅/无障碍", "stroller": "儿童(婴儿车)", "elderly": "老人"}


def party_tags(party):
    party = party or {}
    return [tag for key, tag in TAGS if party.get(key)]


def static_violation(seg, tag):
    """人群-路段静态冲突解释：老人、儿童、无障碍各自的可判依据。"""
    if tag == "wheelchair":
        if not seg["step_free"]:
            return "有台阶，轮椅不可通行"
        if seg["slope_pct"] > LIMITS["wheelchair"]["slope"]:
            return f"坡度{seg['slope_pct']}%超过轮椅上限{LIMITS['wheelchair']['slope']}%"
        if seg["width_m"] < LIMITS["wheelchair"]["width"]:
            return f"净宽{seg['width_m']}m不足轮椅所需{LIMITS['wheelchair']['width']}m"
    elif tag == "elderly":
        if not seg["step_free"]:
            return "有台阶，不适行老人"
        if seg["slope_pct"] > LIMITS["elderly"]["slope"]:
            return f"坡度{seg['slope_pct']}%超过老人上限{LIMITS['elderly']['slope']}%"
    elif tag == "stroller":
        if not seg["step_free"]:
            return "有台阶，婴儿车不可通行"
        if seg["unfenced_water"]:
            return "临水无护栏，儿童不适行"
    return None


def rule_blocks(rule, tags, t):
    """公告规则是否阻断：closed / excludes / 班次服务窗口。"""
    exc = rule.get("excludes", [])
    if rule.get("closed") or "all" in exc:
        return True
    if any(tag in exc for tag in tags):
        return True
    svc = rule.get("service")  # 班次：服务窗口之外视为不可用
    if svc and not in_daily(hm_of(t), svc[0], svc[1]):
        return True
    return False


def dir_match(rule_dir, travel_dir):
    return rule_dir in (None, "both", travel_dir)


# ---------- 影响计算：几何求交 + 方向 + 时段 ----------

def map_revs_to_segments(revs, segs):
    """每条有效修订 → 实际相交的片段。返回 (seg_id→[rev], (alert,seg)→交点)。"""
    by_seg, points = {}, {}
    for rev in revs.values():
        if rev["revoked"] or not rev["geom"]:
            continue
        geom = json.loads(rev["geom"])
        for seg in segs:
            ok, pts = geometry.intersect(geom, json.loads(seg["geom"]))
            if ok:
                by_seg.setdefault(seg["id"], []).append(rev)
                points[(rev["alert_id"], seg["id"])] = pts
    return by_seg, points


def compute_impacts(conn, at=None):
    """服务API：时空影响。逐修订与当前片段求交，方向与时段参与判断。"""
    segs = current_segments(conn)
    revs = latest_revisions(conn)
    alerts = alerts_map(conn)
    by_seg, points = map_revs_to_segments(revs, segs)
    t = parse(at) if at else None
    out = []
    for seg in segs:
        for rev in by_seg.get(seg["id"], []):
            if t and not rev_active_at(rev, t):
                continue
            a = alerts[rev["alert_id"]]
            dirs = ["forward", "backward"] if rev["direction"] == "both" else [rev["direction"]]
            if seg["oneway"]:
                dirs = [d for d in dirs if d == "forward"]
            for d in dirs:
                out.append({
                    "alert_id": a["id"], "rev": rev["rev"], "kind": a["kind"],
                    "title": a["title"], "source": a["source"],
                    "segment_id": seg["id"], "route_id": seg["route_id"],
                    "direction": d, "window": window_text(rev),
                    "rule": json.loads(rev["rule"] or "{}"), "note": rev["note"],
                    "points": points.get((a["id"], seg["id"]), []),
                })
    return out


# ---------- 来源矛盾 → 待核 ----------

def _overlap(a_s, a_e, b_s, b_e):
    if a_s and b_e and a_s > b_e:
        return None
    if b_s and a_e and b_s > a_e:
        return None
    s = max([x for x in (a_s, b_s) if x], default=None)
    e = min([x for x in (a_e, b_e) if x], default=None)
    return s, e


def detect_conflicts(conn):
    """报告与公告/报告之间相互矛盾 → 记为待核，不拼接成通行保证。"""
    stmts = []  # (segment, direction, starts, ends, state, source)
    for r in _rows(conn, "SELECT * FROM reports"):
        stmts.append((r["segment_id"], r["direction"], r["starts_at"], r["ends_at"],
                      r["state"], r["source"]))
    alerts = alerts_map(conn)
    for imp in compute_impacts(conn):
        rule = imp["rule"]
        if rule.get("closed") or "all" in rule.get("excludes", []):
            rev = latest_revisions(conn)[imp["alert_id"]]
            stmts.append((imp["segment_id"], imp["direction"], rev["starts_at"],
                          rev["ends_at"], "closed", alerts[imp["alert_id"]]["source"]))
    added = 0
    for i in range(len(stmts)):
        for j in range(i + 1, len(stmts)):
            s1, s2 = stmts[i], stmts[j]
            if s1[0] != s2[0] or s1[5] == s2[5]:
                continue
            if not (dir_match(s1[1], s2[1]) or dir_match(s2[1], s1[1])):
                continue
            if {s1[4], s2[4]} != {"open", "closed"}:
                continue
            ov = _overlap(s1[2], s1[3], s2[2], s2[3])
            if ov is None:
                continue
            dup = conn.execute(
                "SELECT 1 FROM conflicts WHERE segment_id=? AND starts_at IS ? AND ends_at IS ? AND status='pending_review'",
                (s1[0], ov[0], ov[1])).fetchone()
            if dup:
                continue
            conn.execute(
                "INSERT INTO conflicts(segment_id,direction,starts_at,ends_at,sources,detail,status,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (s1[0], "both", ov[0], ov[1], json.dumps(sorted({s1[5], s2[5]}), ensure_ascii=False),
                 f"来源矛盾：{s1[5]} 称 {s1[4]}，{s2[5]} 称 {s2[4]}；待人工核实，不作通行保证",
                 "pending_review", now_iso()))
            added += 1
    conn.commit()
    return added


def pending_conflicts(conn):
    out = {}
    for c in _rows(conn, "SELECT * FROM conflicts WHERE status='pending_review'"):
        out.setdefault(c["segment_id"], []).append(c)
    return out


def _conflict_at(c, t):
    s, e = parse(c["starts_at"]), parse(c["ends_at"])
    return not ((s and t < s) or (e and t > e))


# ---------- 图搜索（两种策略对比） ----------

def build_edges(segs):
    edges = []
    for s in segs:
        edges.append({"seg": s, "u": s["a"], "v": s["b"], "dir": "forward", "len": s["length_m"]})
        if not s["oneway"]:
            edges.append({"seg": s, "u": s["b"], "v": s["a"], "dir": "backward", "len": s["length_m"]})
    return edges


def _dijkstra(edges, start, goal, depart, edge_filter):
    adj = {}
    for e in edges:
        adj.setdefault(e["u"], []).append(e)
    dist = {start: 0.0}
    prev = {}
    pq = [(0.0, start)]
    expanded = 0
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")) + 1e-9:
            continue
        expanded += 1
        if u == goal:
            break
        t_u = depart + timedelta(seconds=d / SPEED_MPS)
        for e in adj.get(u, []):
            ok, _why = edge_filter(e, t_u)
            if not ok:
                continue
            nd = d + e["len"]
            if nd < dist.get(e["v"], float("inf")) - 1e-9:
                dist[e["v"]] = nd
                prev[e["v"]] = (u, e)
                heapq.heappush(pq, (nd, e["v"]))
    if goal not in dist:
        return None, expanded
    path, cur = [], goal
    while cur != start:
        u, e = prev[cur]
        path.append(e)
        cur = u
    path.reverse()
    return path, expanded


def _dijkstra_dist(edges, start, depart, edge_filter):
    """从 start 到所有节点的最短距离（用于原因集枚举）。"""
    adj = {}
    for e in edges:
        adj.setdefault(e["u"], []).append(e)
    dist = {start: 0.0}
    pq = [(0.0, start)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")) + 1e-9:
            continue
        t_u = depart + timedelta(seconds=d / SPEED_MPS)
        for e in adj.get(u, []):
            ok, _ = edge_filter(e, t_u)
            if not ok:
                continue
            nd = d + e["len"]
            if nd < dist.get(e["v"], float("inf")) - 1e-9:
                dist[e["v"]] = nd
                heapq.heappush(pq, (nd, e["v"]))
    return dist


def _reversed(edges):
    return [{"seg": e["seg"], "u": e["v"], "v": e["u"], "dir": e["dir"], "len": e["len"]}
            for e in edges]


def make_filter(tags, by_seg, conflicts, apply_party, apply_alerts, apply_conflicts):
    def f(e, t):
        if apply_party:
            for tag in tags:
                v = static_violation(e["seg"], tag)
                if v:
                    return False, ("party", tag, v)
        if apply_alerts:
            for rev in by_seg.get(e["seg"]["id"], []):
                if not dir_match(rev["direction"], e["dir"]):
                    continue
                if not rev_active_at(rev, t):
                    continue
                if rule_blocks(json.loads(rev["rule"] or "{}"), tags, t):
                    return False, ("alert", rev["alert_id"])
        if apply_conflicts:  # 待核路段：默认避开，绝不当作可通行保证
            for c in conflicts.get(e["seg"]["id"], []):
                if dir_match(c["direction"], e["dir"]) and _conflict_at(c, t):
                    return False, ("unverified", c["id"])
        return True, None
    return f


def _edge_violations(e, t, tags, by_seg, conflicts):
    out = []
    for tag in tags:
        v = static_violation(e["seg"], tag)
        if v:
            out.append({"type": "party_constraint", "constraint": tag,
                        "constraint_name": TAG_NAME[tag], "segment_id": e["seg"]["id"],
                        "route_id": e["seg"]["route_id"], "detail": v})
    for rev in by_seg.get(e["seg"]["id"], []):
        if dir_match(rev["direction"], e["dir"]) and rev_active_at(rev, t):
            if rule_blocks(json.loads(rev["rule"] or "{}"), tags, t):
                out.append({"type": "alert", "alert_id": rev["alert_id"], "rev": rev["rev"],
                            "segment_id": e["seg"]["id"], "window": window_text(rev)})
    for c in conflicts.get(e["seg"]["id"], []):
        if dir_match(c["direction"], e["dir"]) and _conflict_at(c, t):
            out.append({"type": "unverified", "conflict_id": c["id"], "segment_id": e["seg"]["id"]})
    return out


def reason_set(edges, origin, dest, depart, tags, by_seg, alerts, conflicts):
    """无解原因集合：逐层放松约束，枚举所有位于可行放松路径上的阻断因素。"""
    # 第一层：放松人群约束（保留公告与待核）→ 收集全部人群冲突边
    f1 = make_filter(tags, by_seg, conflicts, False, True, True)
    d1f, d1b = _dijkstra_dist(edges, origin, depart, f1), _dijkstra_dist(_reversed(edges), dest, depart, f1)
    if dest in d1f:
        reasons = []
        for e in edges:
            if e["u"] in d1f and e["v"] in d1b and f1(e, depart + timedelta(seconds=d1f[e["u"]] / SPEED_MPS))[0]:
                for tag in tags:
                    v = static_violation(e["seg"], tag)
                    if v:
                        reasons.append({"type": "party_constraint", "constraint": tag,
                                        "constraint_name": TAG_NAME[tag],
                                        "segment_id": e["seg"]["id"], "route_id": e["seg"]["route_id"],
                                        "detail": v, "basis": "路段属性"})
        if reasons:
            return _dedupe_reasons(reasons)
    # 第二层：再放松公告（保留待核）→ 收集全部阻断公告
    f2 = make_filter(tags, by_seg, conflicts, True, False, True)
    d2f, d2b = _dijkstra_dist(edges, origin, depart, f2), _dijkstra_dist(_reversed(edges), dest, depart, f2)
    if dest in d2f:
        reasons = []
        for e in edges:
            if e["u"] not in d2f or e["v"] not in d2b:
                continue
            t = depart + timedelta(seconds=d2f[e["u"]] / SPEED_MPS)
            if not f2(e, t)[0]:
                continue
            for rev in by_seg.get(e["seg"]["id"], []):
                if dir_match(rev["direction"], e["dir"]) and rev_active_at(rev, t):
                    rule = json.loads(rev["rule"] or "{}")
                    if rule_blocks(rule, tags, t):
                        a = alerts[rev["alert_id"]]
                        reasons.append({"type": "alert", "alert_id": a["id"], "rev": rev["rev"],
                                        "title": a["title"], "source": a["source"],
                                        "kind": a["kind"], "segment_id": e["seg"]["id"],
                                        "window": window_text(rev),
                                        "basis": f"{a['source']} 第{rev['rev']}版公告"})
        if reasons:
            return _dedupe_reasons(reasons)
    # 第三层：再放松待核 → 收集全部待核冲突
    f3 = make_filter(tags, by_seg, conflicts, True, True, False)
    d3f, d3b = _dijkstra_dist(edges, origin, depart, f3), _dijkstra_dist(_reversed(edges), dest, depart, f3)
    if dest in d3f:
        reasons, seen = [], set()
        for e in edges:
            if e["u"] not in d3f or e["v"] not in d3b:
                continue
            for c in conflicts.get(e["seg"]["id"], []):
                if c["id"] in seen or not dir_match(c["direction"], e["dir"]):
                    continue
                t = depart + timedelta(seconds=d3f[e["u"]] / SPEED_MPS)
                if _conflict_at(c, t):
                    seen.add(c["id"])
                    reasons.append({"type": "unverified_conflict", "conflict_id": c["id"],
                                    "segment_id": e["seg"]["id"],
                                    "sources": json.loads(c["sources"]),
                                    "window": f"{c['starts_at'] or '…'} ~ {c['ends_at'] or '…'}",
                                    "basis": "来源相互矛盾，待核，不作通行保证"})
        if reasons:
            return reasons
        return [{"type": "combined", "detail": "单项放松均可通行，组合约束下无解",
                 "basis": "约束组合"}]
    return [{"type": "disconnected", "detail": "起终点之间路网不连通", "basis": "路线几何"}]


def _dedupe_reasons(reasons):
    seen, out = set(), []
    for r in reasons:
        key = json.dumps(r, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def describe_path(path, depart, by_seg, alerts, conflicts, tags, points):
    steps, t, total = [], depart, 0.0
    for e in path:
        entry = t
        t += timedelta(seconds=e["len"] / SPEED_MPS)
        total += e["len"]
        seg_alerts = []
        for rev in by_seg.get(e["seg"]["id"], []):
            if dir_match(rev["direction"], e["dir"]) and rev_active_at(rev, entry):
                a = alerts[rev["alert_id"]]
                seg_alerts.append({
                    "alert_id": a["id"], "rev": rev["rev"], "kind": a["kind"],
                    "title": a["title"], "source": a["source"],
                    "window": window_text(rev), "note": rev["note"],
                    "rule": json.loads(rev["rule"] or "{}"),
                    "points": points.get((a["id"], e["seg"]["id"]), []),
                    "basis": f"{a['source']}《{a['title']}》第{rev['rev']}版",
                })
        steps.append({
            "segment_id": e["seg"]["id"], "route_id": e["seg"]["route_id"],
            "from": e["u"], "to": e["v"], "direction": e["dir"],
            "length_m": round(e["len"], 1),
            "enter_at": entry.isoformat(), "leave_at": t.isoformat(),
            "alerts": seg_alerts,
        })
    return {"steps": steps, "total_m": round(total, 1),
            "arrive_at": t.isoformat(),
            "eta_min": round(total / SPEED_MPS / 60, 1)}


def evaluate_path(path, depart, tags, by_seg, conflicts):
    """策略A后筛：对已达路径逐边检查人群/公告/待核冲突。"""
    viol, t = [], depart
    for e in path:
        viol.extend(_edge_violations(e, t, tags, by_seg, conflicts))
        t += timedelta(seconds=e["len"] / SPEED_MPS)
    return viol


# ---------- 新鲜度与缺信息范围 ----------

def freshness(conn, now=None):
    now = now or datetime.utcnow()
    sources = {}
    for r in _rows(conn, "SELECT source, MAX(received_at) AS last FROM ("
                         "SELECT source, received_at FROM alert_revisions ar "
                         "JOIN alerts a ON a.id=ar.alert_id "
                         "UNION ALL SELECT source, received_at FROM reports) GROUP BY source"):
        sources[r["source"]] = r["last"]
    out = []
    for src, last in sorted(sources.items()):
        age_h = (now - parse(last)).total_seconds() / 3600 if last else None
        out.append({"source": src, "last_received_at": last,
                    "age_hours": round(age_h, 1) if age_h is not None else None,
                    "stale": age_h is None or age_h > STALE_HOURS})
    return {"generated_at": now.replace(microsecond=0).isoformat(), "sources": out}


def missing_info(conn, now=None):
    now = now or datetime.utcnow()
    out = []
    for c in _rows(conn, "SELECT * FROM conflicts WHERE status='pending_review'"):
        out.append({"type": "pending_review", "segment_id": c["segment_id"],
                    "window": f"{c['starts_at'] or '…'} ~ {c['ends_at'] or '…'}",
                    "sources": json.loads(c["sources"]), "detail": c["detail"]})
    for s in freshness(conn, now)["sources"]:
        if s["stale"]:
            out.append({"type": "stale_source", "source": s["source"],
                        "detail": f"来源 {s['source']} 超过{STALE_HOURS}小时未更新，覆盖范围信息可能缺失"})
    return out


# ---------- 规划主入口 ----------

def plan(conn, req):
    origin, dest = req["origin"], req["dest"]
    depart = parse(req.get("depart_at")) or datetime.utcnow()
    party = req.get("party") or {}
    tags = party_tags(party)
    segs = current_segments(conn)
    edges = build_edges(segs)
    revs = latest_revisions(conn)
    alerts = alerts_map(conn)
    by_seg, points = map_revs_to_segments(revs, segs)
    conflicts = pending_conflicts(conn)

    result = {"origin": origin, "dest": dest, "depart_at": depart.isoformat(),
              "party": party, "tags": tags}

    # 策略B：人群与约束直接进入搜索
    fB = make_filter(tags, by_seg, conflicts, True, True, True)
    pathB, expB = _dijkstra(edges, origin, dest, depart, fB)
    sB = {"strategy": "constrained_search", "nodes_expanded": expB}
    if pathB:
        sB["status"] = "ok"
        sB.update(describe_path(pathB, depart, by_seg, alerts, conflicts, tags, points))
    else:
        sB["status"] = "infeasible"
        sB["reasons"] = reason_set(edges, origin, dest, depart, tags, by_seg, alerts, conflicts)
    result["constrained"] = sB

    # 策略A（对比）：先求可达路线，再筛人群与约束
    if req.get("compare"):
        fA = make_filter(tags, by_seg, conflicts, False, False, False)
        pathA, expA = _dijkstra(edges, origin, dest, depart, fA)
        sA = {"strategy": "reachable_then_filter", "nodes_expanded": expA}
        if not pathA:
            sA["status"] = "infeasible"
            sA["reasons"] = [{"type": "disconnected", "detail": "起终点不连通", "basis": "路线几何"}]
        else:
            viol = evaluate_path(pathA, depart, tags, by_seg, conflicts)
            if viol:
                sA["status"] = "violations_after_filter"
                sA["violations"] = viol
                sA["note"] = "先求可达再筛：得到的路径违反约束，需人工或枚举重试"
            else:
                sA["status"] = "ok"
            sA.update(describe_path(pathA, depart, by_seg, alerts, conflicts, tags, points))
        result["reachable_then_filter"] = sA

    result["freshness"] = freshness(conn)
    result["missing_info"] = missing_info(conn)

    if req.get("user_id"):
        result["acks"] = ack_status(conn, req["user_id"],
                                    [st["alert_id"] for st in
                                     (sB.get("steps") or []) for st in st["alerts"]])
    if req.get("save"):
        result["plan_id"] = save_plan(conn, req, result)
    return result


# ---------- 公告/修订/确认/改线/计划 ----------

def create_alert(conn, kind, source, title, geom, direction="both",
                 starts_at=None, ends_at=None, daily_start=None, daily_end=None,
                 rule=None, note="", effective_at=None, received_at=None):
    aid = "AL" + uuid.uuid4().hex[:6]
    conn.execute("INSERT INTO alerts VALUES (?,?,?,?,?)", (aid, kind, source, title, now_iso()))
    add_revision(conn, aid, geom, direction, starts_at, ends_at, daily_start, daily_end,
                 rule or {}, note, effective_at, received_at)
    detect_conflicts(conn)
    return aid


def add_revision(conn, alert_id, geom=None, direction="both", starts_at=None, ends_at=None,
                 daily_start=None, daily_end=None, rule=None, note="",
                 effective_at=None, received_at=None, revoked=False):
    cur = conn.execute("SELECT MAX(rev) AS m FROM alert_revisions WHERE alert_id=?",
                       (alert_id,)).fetchone()["m"] or 0
    rev = cur + 1
    conn.execute(
        "INSERT INTO alert_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (alert_id, rev, json.dumps(geom, ensure_ascii=False) if geom else None,
         direction, starts_at, ends_at, daily_start, daily_end,
         json.dumps(rule or {}, ensure_ascii=False), note,
         effective_at or now_iso(), received_at or now_iso(), 1 if revoked else 0))
    conn.commit()
    return rev


def revoke_alert(conn, alert_id, effective_at, received_at=None, note="撤销"):
    """撤销也是一版修订；received_at 晚于 effective_at 即"迟到撤销"。"""
    return add_revision(conn, alert_id, revoked=True, note=note,
                        effective_at=effective_at, received_at=received_at)


def ack(conn, user_id, alert_id, rev):
    """确认只对应当时版本：rev 必须等于当前版，否则拒绝。"""
    cur = latest_revisions(conn).get(alert_id)
    if not cur or cur["rev"] != rev:
        return False, "公告已有新版本，需查看最新版后重新确认"
    conn.execute("INSERT OR REPLACE INTO acks VALUES (?,?,?,?)",
                 (user_id, alert_id, rev, now_iso()))
    conn.commit()
    return True, "ok"


def ack_status(conn, user_id, alert_ids=None):
    revs = latest_revisions(conn)
    out = {}
    ids = alert_ids or list(revs.keys())
    for aid in ids:
        cur = revs.get(aid)
        if not cur:
            continue
        row = conn.execute("SELECT rev, acked_at FROM acks WHERE user_id=? AND alert_id=?",
                           (user_id, aid)).fetchone()
        acked = row["rev"] if row else None
        out[aid] = {"current_rev": cur["rev"], "acked_rev": acked,
                    "ack_current": acked == cur["rev"],
                    "acked_at": row["acked_at"] if row else None}
    return out


def reroute(conn, route_id, segments):
    """改线：路线版本+1，旧版片段退出影响计算，旧计划快照可检出。"""
    row = conn.execute("SELECT version FROM routes WHERE id=?", (route_id,)).fetchone()
    if not row:
        raise ValueError("route not found")
    v = row["version"] + 1
    conn.execute("UPDATE routes SET version=?, updated_at=? WHERE id=?", (v, now_iso(), route_id))
    for i, s in enumerate(segments):
        sid = f"{route_id}:v{v}:{i}"
        conn.execute(
            "INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, route_id, v, i, s["a"], s["b"], json.dumps(s["geom"]),
             s["length_m"], int(s.get("oneway", 0)), s.get("slope_pct", 0),
             int(s.get("step_free", 1)), s.get("width_m", 2.0), int(s.get("unfenced_water", 0))))
    conn.commit()
    return v


def save_plan(conn, req, result):
    pid = "P" + uuid.uuid4().hex[:8]
    snap = {"alert_revs": {a: r["rev"] for a, r in latest_revisions(conn).items()},
            "route_versions": {r["id"]: r["version"] for r in _rows(conn, "SELECT * FROM routes")}}
    created = req.get("created_at") or now_iso()  # 离线计划可携带自身创建时间
    conn.execute("INSERT INTO plans VALUES (?,?,?,?,?,?)",
                 (pid, req.get("user_id", "anon"),
                  json.dumps(req, ensure_ascii=False), json.dumps(result, ensure_ascii=False),
                  json.dumps(snap), created))
    conn.commit()
    return pid


def get_plan(conn, pid):
    row = conn.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "user_id": row["user_id"], "req": json.loads(row["req"]),
            "result": json.loads(row["result"]), "snapshot": json.loads(row["snapshot"]),
            "created_at": row["created_at"]}


def refresh_plan(conn, pid):
    """旧离线计划重连：比对快照与当前版本，列出变化与需重新确认项。"""
    p = get_plan(conn, pid)
    if not p:
        return None
    snap = p["snapshot"]
    cur_revs = latest_revisions(conn)
    changed, late_revoked = [], []
    for aid, rev in cur_revs.items():
        old = snap["alert_revs"].get(aid)
        if old != rev["rev"]:
            changed.append({"alert_id": aid, "from_rev": old, "to_rev": rev["rev"],
                            "revoked": bool(rev["revoked"])})
        if rev["revoked"] and rev["received_at"] > p["created_at"] \
                and (rev["effective_at"] or "") <= p["created_at"]:
            late_revoked.append({"alert_id": aid, "effective_at": rev["effective_at"],
                                 "received_at": rev["received_at"],
                                 "detail": "撤销公告迟到：计划生成于撤销生效之后、系统收到之前"})
    rerouted = []
    for r in _rows(conn, "SELECT id, version FROM routes"):
        old = snap["route_versions"].get(r["id"])
        if old != r["version"]:
            rerouted.append({"route_id": r["id"], "from_version": old, "to_version": r["version"]})
    # 只要求重新确认"该计划沿途实际会遇到"且版本已变化的公告
    plan_alerts = {al["alert_id"] for st in
                   (p["result"].get("constrained", {}).get("steps") or [])
                   for al in st.get("alerts", [])}
    relevant = [c["alert_id"] for c in changed if c["alert_id"] in plan_alerts]
    acks = ack_status(conn, p["user_id"], relevant)
    return {"plan_id": pid, "created_at": p["created_at"], "checked_at": now_iso(),
            "changed_alerts": changed, "rerouted_routes": rerouted,
            "late_revocations": late_revoked,
            "re_ack_needed": [a for a, s in acks.items() if not s["ack_current"]],
            "up_to_date": not (changed or rerouted or late_revoked)}
