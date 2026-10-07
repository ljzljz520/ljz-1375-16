"""演示数据：小型景区路网 + 施工/班次/适行限制公告。"""
from __future__ import annotations

from . import core

PLACES = [
    ("A", "北门", 0, 0), ("B", "湖畔", 100, 0), ("C", "东门", 200, 0),
    ("D", "山腰", 100, 80), ("E", "山顶", 200, 70), ("F", "竹林", 0, 80),
]

ROUTES = [("R1", "滨湖步道"), ("R2", "山脊线"), ("R3", "竹林径"), ("R4", "环山道")]

# route, seq, a, b, geom, len, oneway, slope, step_free, width, unfenced_water
SEGMENTS = [
    ("R1", 0, "A", "B", [[0, 0], [100, 0]], 100, 0, 1, 1, 2.0, 0),
    ("R1", 1, "B", "C", [[100, 0], [200, 0]], 100, 0, 1, 1, 2.0, 1),  # 临水无护栏
    ("R2", 0, "B", "D", [[100, 0], [100, 80]], 80, 0, 10, 1, 1.5, 0),
    ("R2", 1, "D", "E", [[100, 80], [200, 70]], 100, 0, 15, 0, 1.5, 0),  # 台阶
    ("R3", 0, "A", "F", [[0, 0], [0, 80]], 80, 0, 2, 1, 1.5, 0),
    ("R3", 1, "F", "D", [[0, 80], [100, 80]], 100, 0, 2, 1, 0.8, 0),  # 净宽不足
    ("R4", 0, "C", "E", [[200, 0], [200, 70]], 70, 0, 7, 1, 1.5, 0),
]


def build(conn):
    for p in PLACES:
        conn.execute("INSERT OR REPLACE INTO places VALUES (?,?,?,?)", p)
    for rid, name in ROUTES:
        conn.execute("INSERT OR REPLACE INTO routes VALUES (?,?,1,?)",
                     (rid, name, core.now_iso()))
    for rid, seq, a, b, geom, ln, ow, slope, sf, w, uw in SEGMENTS:
        conn.execute(
            "INSERT OR REPLACE INTO segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{rid}:v1:{seq}", rid, 1, seq, a, b, core.json.dumps(geom),
             ln, ow, slope, sf, w, uw))
    conn.commit()

    seg_bc = {"type": "line", "coordinates": [[110, 0], [190, 0]], "buffer_m": 5}
    seg_bd = {"type": "line", "coordinates": [[100, 10], [100, 70]], "buffer_m": 5}

    # 施工：滨湖段夜间封闭（跨日 22:00-06:00）
    core.create_alert(conn, "construction", "施工方", "滨湖段夜间施工封闭",
                      seg_bc, starts_at="2026-10-08T22:00", ends_at="2026-10-09T06:00",
                      rule={"closed": True}, note="夜间铺装维修",
                      effective_at="2026-10-08T08:00", received_at="2026-10-08T08:05")
    # 班次：滨湖段服务窗口 07:00-20:00，窗口外停驶
    core.create_alert(conn, "schedule", "调度中心", "滨湖段班次 07:00-20:00",
                      seg_bc, rule={"service": ["07:00", "20:00"]},
                      note="窗口外无班次", effective_at="2026-10-01T00:00",
                      received_at="2026-10-01T00:10")
    # 适行限制：滨湖段婴儿车不适行
    core.create_alert(conn, "access_restriction", "景区管理", "滨湖段婴儿车不适行",
                      seg_bc, rule={"excludes": ["stroller"]},
                      note="临水无护栏", effective_at="2026-10-01T00:00",
                      received_at="2026-10-01T00:10")
    # 方向性施工：仅下山方向（D->B，相对片段为 backward）封闭
    core.create_alert(conn, "construction", "施工方", "山脊线下山向临时封闭",
                      seg_bd, direction="backward",
                      starts_at="2026-10-08T00:00", ends_at="2026-10-09T23:59",
                      rule={"closed": True}, note="单向封闭，上山不受影响",
                      effective_at="2026-10-07T12:00", received_at="2026-10-07T12:05")
    return conn
