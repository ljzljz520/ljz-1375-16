"""新鲜度与缺信息、已查看确认（版本绑定）、离线计划与重连、打印件。"""
import json
import uuid
from datetime import datetime

from . import engine, planner, temporal
from .database import bump, jloads

SOURCE_SLA_HOURS = {"市级": 24, "景区物业": 12, "运营方": 12, "景区": 6}


def freshness(conn, at):
    """逐条公告/规则给出新鲜度；超过来源 SLA 标记 stale。"""
    at = temporal.parse_dt(at)
    out = []
    anns = {a["ann_id"]: a for a in conn.execute("SELECT * FROM announcements")}
    for r in conn.execute(
            "SELECT * FROM rules WHERE status='active' AND id IN "
            "(SELECT MAX(id) FROM rules WHERE status='active' GROUP BY rule_key, ann_id)"):
        a = anns[r["ann_id"]]
        obs = temporal.parse_dt(r["observed_at"])
        age_h = (at - obs).total_seconds() / 3600
        sla = SOURCE_SLA_HOURS.get(a["source_level"], 24)
        out.append({"rule_id": r["id"], "ann_id": r["ann_id"],
                    "title": r["title"], "source": a["source"],
                    "observed_at": r["observed_at"], "age_hours": round(age_h, 1),
                    "sla_hours": sla,
                    "stale": age_h > sla,
                    "status": a["status"]})
    return {"at": at.isoformat(), "items": out,
            "stale_count": sum(1 for x in out if x["stale"])}


def missing_info(conn, at):
    """缺信息范围：生效规则缺时段、规则几何未命中任何线段、
    运营类线段班次缺失（无法判断是否可通行）。"""
    items = []
    for seg in conn.execute("SELECT * FROM segments"):
        attrs = jloads(seg["attrs"], {})
        if attrs.get("surface") in ("船", "摆渡车", "电梯") and not seg["service_weekly"]:
            items.append({"severity": "high", "kind": "SEGMENT_NO_SHIFT",
                          "seg_key": seg["seg_key"],
                          "detail": f"{seg['name']} 缺少运营班次，无法判断可通行时间"})
    for r in conn.execute("SELECT * FROM rules WHERE status='active'"):
        windows = jloads(r["windows"], None)
        weekly = jloads(r["weekly"], None)
        if r["effect"] in ("block", "allow") and windows is None and weekly is None:
            items.append({"severity": "high", "kind": "RULE_NO_TIMERANGE",
                          "rule_id": r["id"], "ann_id": r["ann_id"],
                          "detail": f"规则《{r['title']}》缺少生效时段，作用范围时间不确定"})
        n = conn.execute("SELECT COUNT(*) c FROM rule_matches WHERE rule_id=?",
                         (r["id"],)).fetchone()["c"]
        if n == 0:
            items.append({"severity": "medium", "kind": "RULE_UNMATCHED",
                          "rule_id": r["id"], "ann_id": r["ann_id"],
                          "detail": f"规则《{r['title']}》几何未与任何已知线段相交，"
                                    "影响位置无法落到具体线段"})
    return {"at": at if isinstance(at, str) else at.isoformat(),
            "items": items,
            "coverage_note": "结论仅覆盖已收录公告命中的线段；未公告区域无信息，"
                             "不应解读为通行保证"}


# ---------- 已查看确认：只对应当时版本 ----------

def acknowledge(conn, user_id, rule_id, at):
    r = conn.execute("SELECT version FROM rules WHERE id=?", (rule_id,)).fetchone()
    conn.execute(
        "INSERT INTO acknowledgements(user_id,rule_id,rule_version,acknowledged_at)"
        " VALUES(?,?,?,?) ON CONFLICT(user_id,rule_id,rule_version) "
        "DO UPDATE SET acknowledged_at=excluded.acknowledged_at",
        (user_id, rule_id, r["version"], at))
    conn.commit()
    return ack_status(conn, user_id, rule_id)


def ack_status(conn, user_id, rule_id):
    """查看确认只绑定规则版本：新版本出现后，旧确认不隐藏新变化。"""
    cur = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
    current = conn.execute(
        "SELECT * FROM rules WHERE rule_key=? AND ann_id=? AND status='active' "
        "ORDER BY version DESC LIMIT 1",
        (cur["rule_key"], cur["ann_id"])).fetchone()
    ack = conn.execute(
        "SELECT * FROM acknowledgements WHERE user_id=? AND rule_id=? "
        "ORDER BY acknowledged_at DESC LIMIT 1", (user_id, rule_id)).fetchone()
    if not ack:
        return {"rule_id": rule_id, "acknowledged": False, "has_new_version": False}
    return {"rule_id": rule_id, "acknowledged": True,
            "ack_version": ack["rule_version"],
            "current_version": current["version"],
            "has_new_version": ack["rule_version"] != current["version"],
            "acknowledged_at": ack["acknowledged_at"],
            "note": "已查看确认只对应当时版本；规则修订后需重新确认，旧确认不会隐藏新变化"}


def user_ack_map(conn, user_id):
    return {(a["rule_id"], a["rule_version"]): a["acknowledged_at"]
            for a in conn.execute(
                "SELECT rule_id,rule_version,acknowledged_at FROM acknowledgements "
                "WHERE user_id=?", (user_id,))}


# ---------- 离线计划与重连 ----------

def save_offline_plan(conn, user_id, query_time, payload):
    plan_id = "PLAN-" + uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO offline_plans(plan_id,user_id,saved_at,query_time,payload)"
        " VALUES(?,?,?,?,?)",
        (plan_id, user_id, datetime.now().isoformat(timespec="seconds"),
         query_time, json.dumps(payload, ensure_ascii=False)))
    conn.commit()
    return plan_id


def get_offline_plan(conn, plan_id):
    row = conn.execute("SELECT * FROM offline_plans WHERE plan_id=?",
                       (plan_id,)).fetchone()
    if not row:
        return None
    return {"plan_id": plan_id, "user_id": row["user_id"],
            "saved_at": row["saved_at"], "query_time": row["query_time"],
            "payload": jloads(row["payload"], {})}


def reconnect(conn, plan_id, now, person):
    """旧离线计划重连：对比保存时与当前的规则/路线/撤销；
    用当前数据重算；明确警告旧计划不得当作通行保证。"""
    plan = get_offline_plan(conn, plan_id)
    if not plan:
        return None
    saved = plan["payload"].get("rules_snapshot", [])
    saved_keys = {(r["rule_key"], r["version"], r["ann_id"]): r for r in saved}
    current = []
    for r in engine.active_rules(conn, now):
        current.append({"rule_key": r["rule_key"], "version": r["version"],
                        "ann_id": r["ann_id"], "title": r["title"],
                        "effect": r["effect"], "status": r["status"]})
    new_or_changed, revoked = [], []
    cur_keys = {(r["rule_key"], r["version"], r["ann_id"]) for r in current}
    for k, r in saved_keys.items():
        if k not in cur_keys:
            ann = conn.execute("SELECT * FROM announcements WHERE ann_id=?",
                               (k[2],)).fetchone()
            if ann and ann["status"] == "revoked":
                revoked.append({"ann_id": k[2], "title": r["title"],
                                "revoked_at": ann["revoked_at"],
                                "revoke_observed_at": ann["revoke_observed_at"],
                                "late": ann["revoke_observed_at"] and
                                ann["revoked_at"] and
                                temporal.parse_dt(ann["revoke_observed_at"]) >
                                temporal.parse_dt(ann["revoked_at"])})
            else:
                new_or_changed.append({"ann_id": k[2], "title": r["title"],
                                       "reason": "规则被修订/新版本"})
    for r in current:
        k = (r["rule_key"], r["version"], r["ann_id"])
        if k not in saved_keys:
            new_or_changed.append({"ann_id": r["ann_id"], "title": r["title"],
                                   "reason": "保存后新发布"})
    # 路线改线
    reroutes = []
    saved_routes = plan["payload"].get("routes_snapshot", [])
    for rv in saved_routes:
        cur = conn.execute(
            "SELECT version FROM routes WHERE route_id=? AND current=1",
            (rv["route_id"],)).fetchone()
        if cur and cur["version"] != rv["version"]:
            reroutes.append({"route_id": rv["route_id"],
                             "old_version": rv["version"],
                             "new_version": cur["version"]})
    # 用当前数据重算
    qt = plan["query_time"]
    replan = planner.plan_prune(conn, plan["payload"]["start"],
                                plan["payload"]["goal"], now, person)
    return {"plan_id": plan_id, "saved_at": plan["saved_at"],
            "query_time": qt, "reconnect_at": now,
            "new_or_changed": new_or_changed, "revoked": revoked,
            "reroutes": reroutes,
            "warning": "离线计划仅代表保存当时数据；重连后已按当前数据重算，"
                       "旧计划不得继续作为通行保证",
            "current_plan": replan,
            "freshness": freshness(conn, now),
            "missing": missing_info(conn, now)}


# ---------- 打印件 ----------

def print_plan(conn, result, at, person, title="出行计划"):
    lines = []
    lines.append(f"【{title}】 打印时间：{at}")
    lines.append("人群：" + _person_cn(person))
    lines.append("=" * 46)
    fr = freshness(conn, at)
    lines.append(f"数据新鲜度：已收录 {len(fr['items'])} 条，"
                 f"超 SLA {fr['stale_count']} 条")
    for it in fr["items"]:
        if it["stale"] or it["status"] != "active":
            lines.append(f"  · [{it['status']}] {it['title']} 来源={it['source']} "
                         f"收录={it['observed_at']}（{it['age_hours']}h > "
                         f"SLA {it['sla_hours']}h）")
    miss = missing_info(conn, at)
    lines.append("-" * 46)
    lines.append(f"缺信息范围：{len(miss['items'])} 项")
    for m in miss["items"]:
        lines.append(f"  · [{m['severity']}] {m['detail']}")
    lines.append("-" * 46)
    if result.get("feasible"):
        for i, f in enumerate(result["feasible"][:3], 1):
            tag = "通行保证" if f["guaranteed"] else "存在待核·不保证通行"
            lines.append(f"方案{i}（{tag}）：" + " → ".join(f["path_nodes"]))
            for p in f.get("pending", []):
                lines.append(f"    ⚠ 待核路段 {p['seg_key']}：来源矛盾，需人工核实")
    else:
        lines.append("无可行路线。无解原因集合：")
        for r in result["no_solution"]["reasons"]:
            crit = "（关键割）" if r["class"] in result["no_solution"]["critical"] else ""
            lines.append(f"  · {r['reason']}{crit} 路段={','.join(r['segments'])} "
                         f"依据={','.join(r['ann_ids']) or '路网固有属性/班次'}")
    lines.append("-" * 46)
    lines.append(miss["coverage_note"])
    text = "\n".join(lines)
    return {"title": title, "text": text,
            "freshness": fr, "missing": miss}


def _person_cn(p):
    tags = []
    if p.get("wheelchair"):
        tags.append("轮椅/无障碍")
    if p.get("elderly"):
        tags.append("老人")
    if p.get("child_age") is not None:
        tags.append(f"儿童({p['child_age']}岁)")
    return "、".join(tags) if tags else "普通成人"


def add_route_version(conn, route_id, name, kind, note, members, valid_from):
    """管理端发布改线：新版本几何入库，旧版本置 superseded。"""
    row = conn.execute("SELECT COALESCE(MAX(version),0)+1 v FROM routes "
                       "WHERE route_id=?", (route_id,)).fetchone()
    v = row["v"]
    conn.execute("UPDATE routes SET current=0, superseded_at=? WHERE route_id=? "
                 "AND current=1", (valid_from, route_id))
    conn.execute(
        "INSERT INTO routes(route_id,version,name,kind,note,current,valid_from)"
        " VALUES(?,?,?,?,?,1,?)", (route_id, v, name, kind, note, valid_from))
    for i, m in enumerate(members):
        key = m["seg_key"] if isinstance(m, dict) else m
        rev = m.get("reversed", 0) if isinstance(m, dict) else 0
        conn.execute("INSERT INTO route_members VALUES(?,?,?,?,?)",
                     (route_id, v, i, key, rev))
    bump(conn)
    conn.commit()
    return v
