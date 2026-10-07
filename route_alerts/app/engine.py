"""规则引擎：收录即求交、双时间轴、方向/时段/班次/人群、矛盾待核、路线评估。

判定原则
--------
1. 局部施工只与真实几何相交的线段绑定（rule_matches），
   同名/包围框重合不命中；bbox 仅作预筛。
2. 规则生效同时检查：方向掩码、绝对窗口、周期时段（含跨夜）。
3. 线段自身的渡轮/摆渡班次与规则分别给出 OUTSIDE_SHIFT 原因。
4. 同段同时存在相互矛盾（block vs allow）的生效规则 → PENDING_CONFLICT，
   不自行拼接成通行保证。
"""
import json

from . import geometry, temporal
from .database import bump, jloads

# 无解/受阻原因码
REASONS = {
    "BLOCKED_CONSTRUCTION": "施工封闭",
    "BLOCKED_CONTROL": "交通管制",
    "CLOSED_BY_RULE": "规则封闭",
    "AGE_TOO_LOW": "年龄不足（儿童限入）",
    "ELDERLY_FORBIDDEN": "老人禁行（健康/安全限制）",
    "WHEELCHAIR_INACCESSIBLE": "轮椅不可达（无电梯/非铺装）",
    "OUTSIDE_SHIFT": "非运营班次时间",
    "DIRECTION_FORBIDDEN": "方向不符（单行道/限行方向）",
    "PENDING_CONFLICT": "来源矛盾·待核（不保证通行）",
    "NO_TOPO_PATH": "路网不连通",
}


# ---------- 几何 ----------

def segment_polyline(conn, seg_key):
    row = conn.execute("SELECT * FROM segments WHERE seg_key=?", (seg_key,)).fetchone()
    if not row:
        return None, None
    nodes = {r["node_id"]: (r["x"], r["y"])
             for r in conn.execute("SELECT node_id,x,y FROM nodes")}
    a, b = nodes[row["from_node"]], nodes[row["to_node"]]
    poly = [a] + [tuple(p) for p in jloads(row["bends"], [])] + [b]
    return row, poly


def compute_matches(conn, rule_id):
    """收录规则时：与全部物理线段做精确求交，写入 rule_matches。"""
    r = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
    geom = jloads(r["geometry"], [])
    conn.execute("DELETE FROM rule_matches WHERE rule_id=?", (rule_id,))
    for seg in conn.execute("SELECT seg_key FROM segments"):
        _, poly = segment_polyline(conn, seg["seg_key"])
        if r["geom_type"] == "polygon":
            hits = geometry.polygon_polyline_hits(geom, poly)
        else:
            hits = geometry.polyline_intersect(geom, poly)
        if hits:
            point_only = all(all(abs(a - b) < 1e-9 for a, b in h["ranges"]) for h in hits)
            # touch 且命中位置恰在共享节点（t=0/1）→ 仅节点接触
            seg_row, poly = segment_polyline(conn, seg["seg_key"])
            contact = point_only and all(
                h["kind"] == "touch" and
                all((abs(a) < 1e-6 or abs(a - 1) < 1e-6)
                    for lo, hi in h["ranges"] for a in (lo, hi))
                for h in hits)
            conn.execute(
                "INSERT INTO rule_matches(rule_id,seg_key,hit,point_only,contact_only)"
                " VALUES(?,?,?,?,?)",
                (rule_id, seg["seg_key"], json.dumps(hits, ensure_ascii=False),
                 1 if point_only else 0, 1 if contact else 0))


def matched_segments(conn, rule_id):
    return [dict(r) for r in conn.execute(
        "SELECT seg_key,hit,point_only,contact_only FROM rule_matches "
        "WHERE rule_id=? ORDER BY seg_key", (rule_id,))]


# ---------- 公告/规则生命周期 ----------

def _next_rule_version(conn, rule_key, ann_id):
    row = conn.execute(
        "SELECT MAX(version) m FROM rules WHERE rule_key=? AND ann_id=?",
        (rule_key, ann_id)).fetchone()
    return (row["m"] or 0) + 1


def ingest_announcement(conn, payload):
    """录入或修订公告；返回 (ann_row, rule_row)。修订会把旧版本置 superseded。"""
    ann_id = payload["ann_id"]
    existing = conn.execute("SELECT * FROM announcements WHERE ann_id=?", (ann_id,)).fetchone()
    revision_of = payload.get("revision_of")
    old_rule = None
    if revision_of:
        old_ann = conn.execute(
            "SELECT * FROM announcements WHERE ann_id=?", (revision_of,)).fetchone()
        if old_ann:
            conn.execute(
                "UPDATE announcements SET status='superseded' WHERE ann_id=?",
                (revision_of,))
            old_rule = conn.execute(
                "SELECT * FROM rules WHERE ann_id=? AND status='active'",
                (revision_of,)).fetchone()
            if old_rule:
                conn.execute("UPDATE rules SET status='superseded', superseded_at=? "
                             "WHERE id=?", (payload["observed_at"], old_rule["id"]))
    if existing:
        # 同一 ann_id 再次提交也按修订处理
        conn.execute("UPDATE announcements SET source=?,source_level=?,title=?,"
                     "body=?,observed_at=?,revision_of=COALESCE(?,revision_of) "
                     "WHERE ann_id=?",
                     (payload["source"], payload.get("source_level", "景区物业"),
                      payload["title"], payload.get("body", ""),
                      payload["observed_at"], revision_of, ann_id))
        old_rule = conn.execute(
            "SELECT * FROM rules WHERE ann_id=? AND status='active'", (ann_id,)).fetchone()
        if old_rule:
            conn.execute("UPDATE rules SET status='superseded', superseded_at=? WHERE id=?",
                         (payload["observed_at"], old_rule["id"]))
    else:
        conn.execute(
            "INSERT INTO announcements(ann_id,source,source_level,title,status,observed_at,"
            "revision_of,body) VALUES(?,?,?,?, 'active', ?,?,?)",
            (ann_id, payload["source"], payload.get("source_level", "景区物业"),
             payload["title"], payload["observed_at"], revision_of, payload.get("body", "")))
    rl = payload["rule"]
    version = _next_rule_version(conn, rl["rule_key"], ann_id)
    cur = conn.execute(
        "INSERT INTO rules(rule_key,ann_id,version,status,effect,rule_type,title,"
        "geom_type,geometry,direction,windows,weekly,min_age,population,note,"
        "valid_from,observed_at) VALUES(?,?,?, 'active', ?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rl["rule_key"], ann_id, version, rl["effect"], rl["rule_type"], rl["title"],
         rl.get("geom_type", "polyline"),
         json.dumps(rl["geometry"], ensure_ascii=False), rl.get("direction", "B"),
         json.dumps(rl.get("windows"), ensure_ascii=False) if rl.get("windows") else None,
         json.dumps(rl.get("weekly"), ensure_ascii=False) if rl.get("weekly") else None,
         rl.get("min_age"), rl.get("population"), rl.get("title"),
         payload.get("valid_from", payload["observed_at"]), payload["observed_at"]))
    rule_id = cur.lastrowid
    compute_matches(conn, rule_id)
    bump(conn)
    conn.commit()
    return (conn.execute("SELECT * FROM announcements WHERE ann_id=?", (ann_id,)).fetchone(),
            conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone())


def revoke_announcement(conn, ann_id, revoked_at, observed_at):
    """迟到撤销：记录双时间轴；历史时点的查询仍能看到当时的规则。"""
    conn.execute(
        "UPDATE announcements SET status='revoked', revoked_at=?, revoke_observed_at=? "
        "WHERE ann_id=?", (revoked_at, observed_at, ann_id))
    conn.execute("UPDATE rules SET status='revoked' WHERE ann_id=? AND status='active'",
                 (ann_id,))
    bump(conn)
    conn.commit()


def active_rules(conn, at):
    """现实世界 at 时点处于生效状态的规则版本（active，且时间窗口命中）。

    撤销以规则现实生效轴判断：revoked_at 之后不再生效；
    但收录迟到（revoke_observed_at 晚）不影响 at 之前的历史可见性。
    """
    out = []
    for r in conn.execute("SELECT * FROM rules WHERE status IN ('active')"):
        if not _rule_time_active(r, at):
            continue
        out.append(dict(r))
    return out


def visible_rules(conn, valid_at, observed_at):
    """双时间轴：valid_at 时点现实生效、且已在 observed_at 前收录的规则。

    用于"撤销公告迟到"验收：若撤文在 observed_at 之后才收录，
    则当时看到的仍然是旧规则（revoked_at 之前）。
    """
    out = []
    va = temporal.parse_dt(valid_at)
    oa = temporal.parse_dt(observed_at)
    anns = {a["ann_id"]: a for a in conn.execute("SELECT * FROM announcements")}
    for r in conn.execute("SELECT * FROM rules"):
        a = anns.get(r["ann_id"])
        if not a or r["observed_at"] > observed_at:
            continue
        if r["status"] == "superseded" and r["superseded_at"] \
                and temporal.parse_dt(r["superseded_at"]) <= oa:
            continue
        # 撤销（双时间轴）：
        # - 撤文尚未收录（revoke_observed_at > 查询者现在）→ 仍见旧规则
        # - 撤文已收录，且查询时点已到/过撤销生效点 → 不再生效
        # - 撤销生效点之前的历史查询 → 规则仍在（撤销不回改历史）
        if a["status"] == "revoked" and a["revoked_at"]:
            rev_obs = temporal.parse_dt(a["revoke_observed_at"]) if a["revoke_observed_at"] else None
            rev_at = temporal.parse_dt(a["revoked_at"])
            if rev_obs is not None and rev_obs <= oa and va >= rev_at:
                continue
        if not _rule_time_active(r, valid_at):
            continue
        out.append(dict(r))
    return out


def _rule_time_active(rule, at):
    windows = jloads(rule["windows"], None)
    weekly = jloads(rule["weekly"], None)
    if windows is None and weekly is None:
        return True  # 信息类/无时段：一直生效
    if windows is not None and not temporal.window_active(windows, at):
        return False
    if weekly is not None and temporal.weekly_active(weekly, at) is False:
        return False
    return True


# ---------- 规则适用 + 边状态 ----------

def population_applies(rule, person):
    pop = rule.get("population")
    if not pop or pop == "none":
        return True
    if pop == "wheelchair":
        return person.get("wheelchair")
    if pop == "elderly":
        return person.get("elderly")
    if pop == "child":
        return person.get("child_age") is not None
    return True


def rule_applies_on(rule, seg_key, reverse, at, person):
    if not temporal.direction_allows(rule.get("direction", "B"), reverse):
        return False, None
    if not _rule_time_active(rule, at):
        return False, None
    if not population_applies(rule, person):
        return False, None
    if rule.get("min_age") is not None and person.get("child_age") is not None:
        if person["child_age"] < rule["min_age"]:
            return True, "AGE_TOO_LOW"
    return True, None


def _rule_dict(row):
    return dict(row) if row is not None else None


def edge_state(conn, seg_key, reverse, at, person, rules=None):
    """评估一条物理线段在某方向/时间/人群下的状态。

    返回 dict: state=open|blocked|pending, reasons[...], hits, rules, pending[]
    """
    seg_row, _ = segment_polyline(conn, seg_key)
    attrs = jloads(seg_row["attrs"], {})
    weekly = jloads(seg_row["service_weekly"], None)
    reasons, info, blocking, allowing, pending = [], [], [], [], []

    if rules is None:
        rules = active_rules(conn, at)
    matched = {m["rule_id"]: bool(m["contact_only"]) for m in conn.execute(
        "SELECT rule_id,contact_only FROM rule_matches WHERE seg_key=?", (seg_key,))}
    for r in rules:
        if r["id"] not in matched:
            continue
        point_only = matched[r["id"]]
        if point_only and r["effect"] in ("block", "allow"):
            # 仅共享节点接触不代表规则作用于该路段，不据此封闭/放行
            continue
        applies, why = rule_applies_on(r, seg_key, reverse, at, person)
        if not applies:
            if why == "DIRECTION_FORBIDDEN":
                blocking.append((r, "DIRECTION_FORBIDDEN"))
            continue
        if why == "AGE_TOO_LOW":
            blocking.append((r, "AGE_TOO_LOW"))
            continue
        if r["effect"] == "block":
            code = {
                "construction": "BLOCKED_CONSTRUCTION",
                "control": "BLOCKED_CONTROL",
                "accessibility": "WHEELCHAIR_INACCESSIBLE",
                "age": "AGE_TOO_LOW",
            }.get(r["rule_type"], "CLOSED_BY_RULE")
            blocking.append((r, code))
        elif r["effect"] == "allow":
            allowing.append(r)
        else:
            info.append(r)

    # 线段固有属性
    if person.get("wheelchair") and attrs.get("wheelchair_ok") is False:
        blocking.append((None, "WHEELCHAIR_INACCESSIBLE"))
    if person.get("elderly") and attrs.get("elderly_ok") is False:
        blocking.append((None, "ELDERLY_FORBIDDEN"))
    age = person.get("child_age")
    if age is not None and attrs.get("min_age") is not None and age < attrs["min_age"]:
        blocking.append((None, "AGE_TOO_LOW"))
    # 班次
    if weekly is not None and temporal.weekly_active(weekly, at) is False:
        blocking.append((None, "OUTSIDE_SHIFT"))

    # 矛盾：同段同时存在生效的 block 与 allow（不同来源）→ 待核
    if blocking and allowing:
        for br, code in blocking:
            for ar in allowing:
                if br is not None and br.get("ann_id") != ar.get("ann_id"):
                    pending.append({"a": br["ann_id"], "b": ar["ann_id"],
                                    "seg_key": seg_key})

    def ref(r, code):
        return {"code": code, "seg_key": seg_key,
                "rule_id": r["id"] if r else None,
                "rule_key": r["rule_key"] if r else None,
                "ann_id": r["ann_id"] if r else None,
                "title": r["title"] if r else REASONS[code],
                "source": None}  # filled by caller if needed

    refs = []
    for br, code in blocking:
        refs.append(ref(br, code))
    for r in info:
        reasons.append({"code": "INFO", "seg_key": seg_key, "rule_id": r["id"],
                        "ann_id": r["ann_id"], "title": r["title"]})

    state = "open"
    if blocking:
        state = "blocked"
    if pending:
        # 存在相互矛盾的生效来源 → 待核：不下达封闭结论，也不保证通行
        state = "pending"
    return {"seg_key": seg_key, "state": state, "reasons": refs, "info": reasons,
            "blocking": [(r["id"] if r else None, c) for r, c in blocking],
            "allowing": [r["id"] for r in allowing],
            "pending": pending,
            "attrs": attrs,
            "service": temporal.describe_weekly(weekly)}


def evaluate_route(conn, route_id, at, person, version=None, observed_at=None,
                   rules=None):
    """整条路线评估：逐成员边（考虑路线方向 reversed），返回局部命中依据。"""
    if version is None:
        version = conn.execute(
            "SELECT version FROM routes WHERE route_id=? AND current=1",
            (route_id,)).fetchone()["version"]
    members = conn.execute(
        "SELECT * FROM route_members WHERE route_id=? AND version=? ORDER BY seq",
        (route_id, version)).fetchall()
    if rules is None:
        rules = visible_rules(conn, at, observed_at or at) if observed_at \
            else active_rules(conn, at)
    edges, state = [], "open"
    for m in members:
        es = edge_state(conn, m["seg_key"], bool(m["reversed"]), at, person, rules)
        hits = [mm for mm in matched_segments(conn, -1)] if False else \
            [x for r in rules for x in matched_segments(conn, r["id"])
             if x["seg_key"] == m["seg_key"]]
        es["reversed"] = bool(m["reversed"])
        es["hits"] = hits
        edges.append(es)
        if es["state"] == "blocked":
            state = "blocked"
        elif es["state"] == "pending" and state != "blocked":
            state = "pending"
    return {"route_id": route_id, "version": version, "state": state,
            "guaranteed": state == "open", "edges": edges}
