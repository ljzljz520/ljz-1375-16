"""出行规划：两种策略 + 无解原因集合（含关键割诊断）。

filter  —— “先求可达路线，再筛人群与约束”：
          先在纯拓扑上枚举全部简单路径，再对每条做规则/人群/班次过滤。
          优点：解释完整；缺点：会展开大量注定被封的路径，边评估次数高。
prune   —— “直接进入搜索”：
          A* 搜索时即时调用 edge_state，命中封闭即刻剪枝。
          优点：边评估少、不枚举死路；无解原因由关键割诊断补齐。
"""
import heapq
import math
from collections import defaultdict

from . import engine, temporal
from .database import jloads

REASON_CLASSES = {
    "BLOCKED_CONSTRUCTION": "construction",
    "BLOCKED_CONTROL": "control",
    "CLOSED_BY_RULE": "control",
    "AGE_TOO_LOW": "age",
    "ELDERLY_FORBIDDEN": "elderly",
    "WHEELCHAIR_INACCESSIBLE": "wheelchair",
    "OUTSIDE_SHIFT": "shift",
    "DIRECTION_FORBIDDEN": "direction",
    "PENDING_CONFLICT": "conflict",
}


def build_graph(conn):
    nodes = {r["node_id"]: (r["x"], r["y"]) for r in conn.execute("SELECT * FROM nodes")}
    adj = defaultdict(list)
    for seg in conn.execute("SELECT * FROM segments"):
        a, b = seg["from_node"], seg["to_node"]
        adj[a].append((b, seg["seg_key"], 0))
        adj[b].append((a, seg["seg_key"], 1))
    return nodes, adj


def _dist(nodes, a, b):
    return math.hypot(nodes[a][0] - nodes[b][0], nodes[a][1] - nodes[b][1])


def _edge(conn, adj_entry, at, person, rules):
    to, seg_key, rev = adj_entry
    es = engine.edge_state(conn, seg_key, bool(rev), at, person, rules)
    return to, seg_key, bool(rev), es


def plan_filter(conn, start, goal, at, person, max_paths=40):
    """先可达、后筛选。返回可行路径 + 每条被拒路径的原因。"""
    nodes, adj = build_graph(conn)
    rules = engine.active_rules(conn, at)
    candidates = _enumerate_simple(nodes, adj, start, goal, max_paths)
    feasible, rejected, edge_checks = [], [], 0
    for path_nodes, path_edges in candidates:
        blocked, pending = [], []
        for (frm, to, seg_key, rev) in path_edges:
            es = engine.edge_state(conn, seg_key, rev, at, person, rules)
            edge_checks += 1
            if es["state"] == "blocked":
                blocked.extend(es["reasons"])
            elif es["state"] == "pending":
                pending.append({"seg_key": seg_key, "pending": es["pending"]})
        leg = _leg(path_edges)
        if blocked:
            rejected.append({"path_nodes": path_nodes, "leg": leg,
                             "reasons": _dedup_reasons(blocked), "pending": pending})
        else:
            feasible.append({"path_nodes": path_nodes, "leg": leg,
                             "guaranteed": not pending, "pending": pending})
    feasible.sort(key=lambda x: (not x["guaranteed"], len(x["path_nodes"])))
    no_solution = diagnose(conn, start, goal, at, person, rules,
                           rejected_edges=edge_checks)
    return {"mode": "filter", "feasible": feasible, "rejected": rejected,
            "metrics": {"candidates": len(candidates), "edge_checks": edge_checks,
                        "nodes_expanded": len(candidates)},
            "no_solution": no_solution}


def _leg(path_edges):
    return [{"from": f, "to": t, "seg_key": k, "reversed": bool(r)}
            for f, t, k, r in path_edges]


def _enumerate_simple(nodes, adj, start, goal, cap):
    results = []

    def dfs(u, path_n, path_e, visited, depth=0):
        if len(results) >= cap or depth > 12:
            return
        if u == goal:
            results.append((list(path_n), list(path_e)))
            return
        # 以直线距离排序，先产出短路径
        nbrs = sorted(adj[u], key=lambda e: _dist(nodes, e[0], goal))
        for (v, key, rev) in nbrs:
            if v in visited:
                continue
            visited.add(v)
            path_n.append(v)
            path_e.append((u, v, key, rev))
            dfs(v, path_n, path_e, visited, depth + 1)
            path_n.pop()
            path_e.pop()
            visited.remove(v)

    dfs(start, [start], [], {start})
    return results


def plan_prune(conn, start, goal, at, person):
    """约束直接进入 A* 搜索；封闭边不展开，待核边不保证通行。"""
    nodes, adj = build_graph(conn)
    rules = engine.active_rules(conn, at)
    PENDING_PENALTY = 1e6
    # (排序键 f, 实际长度 g, 节点, 节点路径, 待核记录)；待核边给巨大惩罚
    pq = [(0, 0, start, [start], [])]
    best_guaranteed, best_any, seen, edge_checks, expanded = None, None, {}, 0, 0
    while pq:
        f, g, u, pn, pe = heapq.heappop(pq)
        expanded += 1
        if u == goal:
            if all(not p for p in pe):
                if best_guaranteed is None:
                    best_guaranteed = (pn, pe)
            elif best_any is None:
                best_any = (pn, pe)
            if best_guaranteed:
                break
            continue
        for entry in adj[u]:
            v, key, rev, es = _edge(conn, entry, at, person, rules)
            edge_checks += 1
            if es["state"] == "blocked":
                continue
            step = _dist(nodes, u, v)
            pen = PENDING_PENALTY if es["state"] == "pending" else 0
            ng = g + step + pen
            n_pend = pe + ([{"seg_key": key, "pending": es["pending"]}]
                           if es["state"] == "pending" else [])
            has_pend = bool(n_pend)
            state_key = (v, has_pend)
            if state_key in seen and seen[state_key] <= ng:
                continue
            seen[state_key] = ng
            heapq.heappush(pq, (ng + _dist(nodes, v, goal), ng, v,
                                pn + [v], n_pend))
    feasible = []
    for best in (best_guaranteed, best_any):
        if best is None:
            continue
        pn, pe = best
        pending = [p for p in pe if p]
        feasible.append({"path_nodes": pn,
                         "leg": [{"from": pn[i], "to": pn[i + 1]}
                                 for i in range(len(pn) - 1)],
                         "guaranteed": not pending, "pending": pending})
    no_solution = diagnose(conn, start, goal, at, person, rules,
                           rejected_edges=edge_checks)
    return {"mode": "prune", "feasible": feasible, "rejected": [],
            "metrics": {"candidates": 0, "edge_checks": edge_checks,
                        "nodes_expanded": expanded},
            "no_solution": no_solution}


def _dedup_reasons(reasons):
    out, seen = [], set()
    for r in reasons:
        k = (r["code"], r["seg_key"], r.get("rule_id"))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


# ---------- 无解原因集合 + 关键割 ----------

def diagnose(conn, start, goal, at, person, rules=None, rejected_edges=0):
    """无解时给出完整原因集合，并标记“关键割”原因。

    关键割：单独放松这一类约束后，拓扑+剩余约束下 start→goal 即恢复连通。
    """
    if rules is None:
        rules = engine.active_rules(conn, at)
    nodes, adj = build_graph(conn)

    def reachable(relax_classes, relax_segments=None):
        relax_segments = relax_segments or set()
        seen = {start}
        stack = [start]
        while stack:
            u = stack.pop()
            if u == goal:
                return True
            for (v, key, rev) in adj[u]:
                if v in seen:
                    continue
                es = engine.edge_state(conn, key, bool(rev), at, person, rules)
                codes = [x["code"] for x in es["reasons"]]
                remaining = [c for c in codes
                             if REASON_CLASSES.get(c) not in relax_classes]
                if not remaining or key in relax_segments:
                    seen.add(v)
                    stack.append(v)
        return False

    if reachable(set()):
        return {"has_solution": True, "reasons": [], "critical": []}

    # 收集从起点可达边界上的全部封闭原因
    reasons = {}
    seen = {start}
    stack = [start]
    while stack:
        u = stack.pop()
        for (v, key, rev) in adj[u]:
            es = engine.edge_state(conn, key, bool(rev), at, person, rules)
            if es["state"] != "blocked":
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
                continue
            for r in es["reasons"]:
                k = (r["code"], key)
                reasons.setdefault(k, {
                    "code": r["code"], "reason": engine.REASONS[r["code"]],
                    "class": REASON_CLASSES.get(r["code"], "other"),
                    "segments": set(), "ann_ids": set()})
                reasons[k]["segments"].add(key)
                if r.get("ann_id"):
                    reasons[k]["ann_ids"].add(r["ann_id"])
            # 检查是否纯待核（不保证，但并非硬性封闭）
    out = []
    classes_present = {v["class"] for v in reasons.values()}
    for v in reasons.values():
        out.append({"code": v["code"], "reason": v["reason"], "class": v["class"],
                    "segments": sorted(v["segments"]),
                    "ann_ids": sorted(v["ann_ids"])})
    critical = []
    for cls in sorted(classes_present):
        others = classes_present - {cls}
        if reachable({cls}):
            critical.append(cls)
    out.sort(key=lambda x: (x["class"], x["code"]))
    return {"has_solution": False, "reasons": out, "critical": critical,
            "note": "关键割=单独放松该类约束即可恢复通行" if critical else
                    "需同时放松多类约束才能恢复通行"}
