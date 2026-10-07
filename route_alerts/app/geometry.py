"""2D 几何引擎：线段/折线/多边形求交。

关键原则：提醒（规则）命中路线，必须以 *真实几何相交* 为准，
不接受"景区同名"或"包围框（bbox）重合"就停用整条路线。
bbox 仅用于快速预筛；最终判定一律走精确求交。
"""
import math

EPS = 1e-9


def bbox_of(coords):
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_overlap(a, b):
    return not (a[2] < b[0] - EPS or b[2] < a[0] - EPS or
                a[3] < b[1] - EPS or b[3] < a[1] - EPS)


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _on_segment(p, a, b):
    return (abs(_cross(a, b, p)) <= EPS and
            min(a[0], b[0]) - EPS <= p[0] <= max(a[0], b[0]) + EPS and
            min(a[1], b[1]) - EPS <= p[1] <= max(a[1], b[1]) + EPS)


def seg_intersect(a, b, c, d):
    """两线段是否相交（含端点相接；共线重叠返回 'overlap'）。"""
    if not bbox_overlap(bbox_of([a, b]), bbox_of([c, d])):
        return None
    c1, c2 = _cross(a, b, c), _cross(a, b, d)
    c3, c4 = _cross(c, d, a), _cross(c, d, b)

    # 共线情况
    if abs(c1) <= EPS and abs(c2) <= EPS and abs(c3) <= EPS and abs(c4) <= EPS:
        def t_of(p):
            if abs(b[0] - a[0]) > EPS:
                return (p[0] - a[0]) / (b[0] - a[0])
            return (p[1] - a[1]) / (b[1] - a[1])
        ts = sorted(t_of(p) for p in (c, d))
        lo = max(0.0, ts[0])
        hi = min(1.0, ts[1])
        if hi < lo - EPS:
            return None
        if hi - lo <= EPS:  # 仅端点接触
            return {"type": "touch", "ranges": [(lo, lo)]}
        return {"type": "overlap", "ranges": [(lo, hi)]}

    if ((c1 > EPS and c2 < -EPS) or (c1 < -EPS and c2 > EPS)) and \
       ((c3 > EPS and c4 < -EPS) or (c3 < -EPS and c4 > EPS)):
        t = _intersection_param(a, b, c, d)
        return {"type": "cross", "ranges": [(t, t)]}

    # 端点落在线段上
    hits = []
    if _on_segment(c, a, b):
        hits.append(_param(a, b, c))
    if _on_segment(d, a, b):
        hits.append(_param(a, b, d))
    if _on_segment(a, c, d):
        hits.append(0.0)
    if _on_segment(b, c, d):
        hits.append(1.0)
    if hits:
        t = min(max(hits[0], 0.0), 1.0)
        return {"type": "touch", "ranges": [(t, t)]}
    return None


def _param(a, b, p):
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) > EPS:
        return (p[0] - a[0]) / dx
    return (p[1] - a[1]) / dy if abs(dy) > EPS else 0.0


def _intersection_param(a, b, c, d):
    """返回交点在 ab 上的参数 t。"""
    x1, y1, x2, y2 = a[0], a[1], b[0], b[1]
    x3, y3, x4, y4 = c[0], c[1], d[0], d[1]
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) <= EPS:
        return _param(a, b, c)
    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / den
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / den
    return _param(a, b, (px, py))


def merge_ranges(ranges):
    if not ranges:
        return []
    rs = sorted((max(0.0, lo), min(1.0, hi)) for lo, hi in ranges)
    out = [list(rs[0])]
    for lo, hi in rs[1:]:
        if lo <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def polyline_intersect(poly_a, poly_b):
    """折线 A（规则几何）与折线 B（路段）求交。

    返回命中区间列表：[{"segment": j, "ranges": [(t1,t2)...], "kind": ...}]
    表示 B 的第 j 个边段上被命中的参数区间。仅在端点接触也算命中
    （真实几何相交），但会标记 kind=touch。
    """
    if not bbox_overlap(bbox_of(poly_a), bbox_of(poly_b)):
        return []
    hits = {}
    for i in range(len(poly_a) - 1):
        a, b = poly_a[i], poly_a[i + 1]
        for j in range(len(poly_b) - 1):
            c, d = poly_b[j], poly_b[j + 1]
            r = seg_intersect(a, b, c, d)
            if r:
                e = hits.setdefault(j, {"segment": j, "ranges": [],
                                        "kind": r["type"]})
                e["ranges"].extend(r["ranges"])
                if r["type"] == "overlap":
                    e["kind"] = "overlap"
                elif r["type"] == "cross" and e["kind"] == "touch":
                    e["kind"] = "cross"
    result = []
    for j, e in hits.items():
        e["ranges"] = merge_ranges(e["ranges"])
        result.append(e)
    return sorted(result, key=lambda x: x["segment"])


def point_in_polygon(px, py, ring):
    # 边界点视为内部（施工区覆盖路面中心线）
    for i in range(len(ring)):
        if _on_segment((px, py), ring[i], ring[(i + 1) % len(ring)]):
            return True
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / (yj - yi + (EPS * 0)) + xi):
            inside = not inside
        j = i
    return inside


def polygon_polyline_hits(ring, poly):
    """多边形（施工区域）与折线（路段）求交。

    路段边与多边形边界相交，或路段端点落入多边形内，均算真实命中。
    返回 {"segment": j, "ranges": [...], "kind": 'cross'|'inside'|'overlap'}。
    """
    if not bbox_overlap(bbox_of(ring), bbox_of(poly)):
        return []
    hits = {}

    def add(j, t1, t2, kind):
        e = hits.setdefault(j, {"segment": j, "ranges": [], "kind": kind})
        e["ranges"].append((t1, t2))
        rank = {"touch": 0, "cross": 1, "inside": 2, "overlap": 3}
        if rank[kind] > rank[e["kind"]]:
            e["kind"] = kind

    for j in range(len(poly) - 1):
        a, b = poly[j], poly[j + 1]
        a_in = point_in_polygon(a[0], a[1], ring)
        b_in = point_in_polygon(b[0], b[1], ring)
        if a_in and b_in:
            add(j, 0.0, 1.0, "inside")
            continue
        for i in range(len(ring)):
            c0 = ring[i]
            c1 = ring[(i + 1) % len(ring)]
            r = seg_intersect(a, b, c0, c1)
            if r:
                kind = "overlap" if r["type"] == "overlap" else "cross"
                for lo, hi in r["ranges"]:
                    add(j, lo, hi, kind)
        # 单侧在内部 → 端点到交点的区间
        if a_in != b_in:
            e = hits.get(j)
            if e:
                t = e["ranges"][0][0]
                e["ranges"] = [(0.0, t)] if a_in else [(t, 1.0)]
    result = []
    for e in hits.values():
        e["ranges"] = merge_ranges(e["ranges"])
        result.append(e)
    return sorted(result, key=lambda x: x["segment"])


def polyline_length(poly):
    return sum(math.hypot(poly[i + 1][0] - poly[i][0],
                          poly[i + 1][1] - poly[i][1])
               for i in range(len(poly) - 1))
