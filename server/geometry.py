"""几何求交：施工/限制范围必须与路线实际片段求交。

明确不做两件事：
1. 不按景区/地点同名匹配——几何之外不看名称；
2. 不用包围框重合近似——必须精确求交，交点用于页面展示"影响位置"。
"""
from __future__ import annotations

import math


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def seg_seg_intersect(p1, p2, p3, p4):
    """线段-线段求交，返回交点或 None。"""
    d1 = _sub(p2, p1)
    d2 = _sub(p4, p3)
    den = _cross(d1, d2)
    if abs(den) < 1e-12:
        return None
    t = _cross(_sub(p3, p1), d2) / den
    u = _cross(_sub(p3, p1), d1) / den
    if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
        return (p1[0] + t * d1[0], p1[1] + t * d1[1])
    return None


def point_in_poly(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if xin > x:
                inside = not inside
    return inside


def dist_point_seg(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def polyline_length(line):
    return sum(
        math.hypot(line[i + 1][0] - line[i][0], line[i + 1][1] - line[i][1])
        for i in range(len(line) - 1)
    )


def _dedupe(pts):
    out = []
    for p in pts:
        if all(math.hypot(p[0] - q[0], p[1] - q[1]) > 1e-6 for q in out):
            out.append(p)
    return out


def intersect(geom, line):
    """告警几何与路线片段（折线）求交。

    geom: {'type':'polygon','coordinates':[[x,y]...]}
        | {'type':'line','coordinates':[...], 'buffer_m':r}
        | {'type':'point','coordinates':[x,y], 'buffer_m':r}
    返回 (是否相交, 交点列表)。
    """
    line = [tuple(p) for p in line]
    pts = []
    gtype = geom.get("type")
    if gtype == "polygon":
        poly = [tuple(p) for p in geom["coordinates"]]
        for p in line:
            if point_in_poly(p, poly):
                pts.append(p)
        for i in range(len(line) - 1):
            for j in range(len(poly)):
                q = seg_seg_intersect(line[i], line[i + 1], poly[j], poly[(j + 1) % len(poly)])
                if q:
                    pts.append(q)
    elif gtype == "line":
        gl = [tuple(p) for p in geom["coordinates"]]
        buf = geom.get("buffer_m") or 0
        for i in range(len(line) - 1):
            for j in range(len(gl) - 1):
                q = seg_seg_intersect(line[i], line[i + 1], gl[j], gl[j + 1])
                if q:
                    pts.append(q)
        if buf > 0:
            samples = list(line)
            for i in range(len(line) - 1):
                samples.append(((line[i][0] + line[i + 1][0]) / 2, (line[i][1] + line[i + 1][1]) / 2))
            for p in samples:
                if min(dist_point_seg(p, gl[j], gl[j + 1]) for j in range(len(gl) - 1)) <= buf:
                    pts.append(p)
    elif gtype == "point":
        c = tuple(geom["coordinates"])
        r = geom.get("buffer_m") or 0
        for i in range(len(line) - 1):
            if dist_point_seg(c, line[i], line[i + 1]) <= r:
                pts.append(line[i])
    pts = _dedupe(pts)
    return (len(pts) > 0, pts)
