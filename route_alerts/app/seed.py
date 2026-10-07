"""演示/验收数据：景区路网 + 8 条公告（含矛盾对、同名陷阱、撤销迟到）。

时间锚点：2026-10-07（周三）。几何为局部平面坐标（米）。
节点：南/北/东/西门、码头、崖下平台、南湖码头、临时桥、东渡轮台、东门枢纽、望海亭。
"""
import json
import os
from datetime import datetime

from .database import DB_PATH, bump, connect

NODES = {
    "G_S":  ("南门", 500, 650),
    "G_N":  ("北门", 500, 80),
    "G_E":  ("东门", 920, 360),
    "G_W":  ("西门", 80, 360),
    "QD":   ("码头", 500, 360),
    "YX":   ("崖下平台", 470, 250),
    "SL":   ("南湖码头", 220, 500),
    "LT":   ("临江步道口", 780, 500),
    "ET":   ("东渡轮台", 780, 240),
    "X":    ("东门枢纽", 700, 360),
    "WHT":  ("望海亭", 700, 180),
}

# 渡轮/接驳：有班次窗口；其它步道默认全天
FERRY = {"days": [0, 1, 2, 3, 4, 5, 6], "start": "08:30", "end": "17:00"}
SHUTTLE = {"days": [0, 1, 2, 3, 4, 5, 6], "start": "08:00", "end": "18:00"}
WHEEL_CHAIR_FRIENDLY = {"wheelchair_ok": True, "elderly_ok": True, "flat": True}
DIRT_ATTRS = {"surface": "碎石", "wheelchair_ok": False,
              "elderly_ok": True, "child_ok": True}

# 物理线段：(key, 名称, from, to, attrs, 班次)
SEGMENTS = [
    ("S0",  "主轴步道",      "G_S", "QD",  {"surface": "石板", **WHEEL_CHAIR_FRIENDLY}, None),
    ("S1",  "主轴步道北段",  "QD",  "YX",  {"surface": "石板", **WHEEL_CHAIR_FRIENDLY}, None),
    ("S2",  "山崖电梯道",    "YX",  "G_N", {"surface": "电梯步道", "wheelchair_ok": True, "elderly_ok": True}, None),
    ("S3",  "跨湖渡轮",      "SL",  "QD",  {"surface": "船",   **WHEEL_CHAIR_FRIENDLY}, FERRY),
    ("S4",  "南湖平路",      "G_S", "SL",  {"surface": "石板", **WHEEL_CHAIR_FRIENDLY}, None),
    ("S5",  "西山专线南段",  "SL",  "YX",  {"surface": "石板", "wheelchair_ok": True, "elderly_ok": True}, None),
    ("S6",  "西山专线北段",  "YX",  "WHT", {"surface": "石阶", "wheelchair_ok": False,
                                            "elderly_ok": False, "child_ok": True,
                                            "min_age": 8}, None),
    ("S7",  "临时桥道路",    "LT",  "X",   {"surface": "石板", **WHEEL_CHAIR_FRIENDLY}, None),
    ("S8",  "东岗道路",      "X",   "ET",  {"surface": "石板", **WHEEL_CHAIR_FRIENDLY}, None),
    ("S9",  "东门摆渡车",    "G_E", "X",   {"surface": "摆渡车", **WHEEL_CHAIR_FRIENDLY}, SHUTTLE),
    ("S10", "临海平路",      "ET",  "G_N", {"surface": "石板", **WHEEL_CHAIR_FRIENDLY}, None),
    ("S11", "临江土路",      "G_S", "LT",  DIRT_ATTRS, None),
    ("S12", "西山无障碍摆渡", "G_W", "SL",  {"surface": "摆渡车", **WHEEL_CHAIR_FRIENDLY},
     {"days": [0, 1, 2, 3, 4], "start": "08:00", "end": "18:00"}),
    ("S13", "东门连廊电梯",  "X",   "WHT", {"surface": "电梯", **WHEEL_CHAIR_FRIENDLY},
     {"days": [0, 1, 2, 3, 4, 5, 6], "start": "08:00", "end": "20:00"}),
    ("S14", "望海亭北段",    "WHT", "G_N", {"surface": "石板", **WHEEL_CHAIR_FRIENDLY}, None),
    ("S15", "南湖支线碎石段", "SL",  "G_W", {"surface": "碎石", "wheelchair_ok": False,
                                             "elderly_ok": True, "child_ok": True}, None),
    ("S16", "西湖南线",      "X",   "QD",  {"surface": "石板", **WHEEL_CHAIR_FRIENDLY}, None),
]

# (route_id, version, name, kind, note, [(seg_key, reversed)])
ROUTES = [
    ("R1", 1, "湖光主环线", "步道",
     "主干游览环线 v1，途经西山崖电梯",
     [("S0", 0), ("S1", 0), ("S2", 0), ("S14", 1), ("S13", 1),
      ("S16", 1), ("S0", 1)]),
    ("R1", 2, "湖光主环线", "步道",
     "v2：崖电梯施工后改走东湖连廊",
     [("S0", 0), ("S16", 0), ("S13", 0), ("S14", 0), ("S1", 1), ("S0", 1)]),
    ("R3", 1, "西山无障碍专线", "无障碍",
     "西门无障碍摆渡—渡轮—崖电梯",
     [("S12", 0), ("S3", 0), ("S1", 0), ("S2", 0)]),
    ("R4", 1, "西山专线", "专线",
     "登高观光线（石阶，年龄限制）",
     [("S4", 0), ("S3", 1), ("S5", 0), ("S6", 0), ("S14", 1)]),
    ("R5", 1, "南湖支线", "步道",
     "南湖码头—西门碎石支线",
     [("S15", 0)]),
    ("R6", 1, "东门观光线", "步道",
     "南门—临江土路—临时桥—东渡轮台—北门",
     [("S11", 0), ("S7", 0), ("S8", 0), ("S10", 0)]),
]

# 规则坐标：取线段几何中点附近，明确为“局部施工”
def line(a, b):
    return [a, b]


def pt(node, dx=0, dy=0):
    n = NODES[node]
    return [n[1] + dx, n[2] + dy]



def _seg_poly(seg_key):
    a, b = SEG_BY_KEY[seg_key][2], SEG_BY_KEY[seg_key][3]
    return [pt(a)] + [list(p) for p in BENDS.get(seg_key, [])] + [pt(b)]


def _point_at(poly, t):
    import math
    seg_lens = [math.hypot(poly[i + 1][0] - poly[i][0], poly[i + 1][1] - poly[i][1])
                for i in range(len(poly) - 1)]
    total = sum(seg_lens)
    d = t * total
    for i, L in enumerate(seg_lens):
        if d <= L or i == len(seg_lens) - 1:
            u = 0 if L == 0 else min(1.0, d / L)
            return [poly[i][0] + (poly[i + 1][0] - poly[i][0]) * u,
                    poly[i][1] + (poly[i + 1][1] - poly[i][1]) * u]
        d -= L


def _subline(seg_key, t0, t1, off=0.0):
    """取折线参数 [t0,t1] 子段（沿各点重采样），off=0 时与原线真实重叠。"""
    poly = _seg_poly(seg_key)
    ts = [t0]
    n = len(poly) - 1
    for i in range(1, n):
        # 计算折线顶点处的累计 t
        import math
        lens = [math.hypot(poly[k + 1][0] - poly[k][0], poly[k + 1][1] - poly[k][1])
                for k in range(n)]
        tot = sum(lens)
        acc = sum(lens[:i]) / tot
        if t0 < acc < t1:
            ts.append(acc)
    ts.append(t1)
    return [_point_at(poly, t) for t in ts]


def _box_around(seg_key, t0, t1, pad=18.0):
    """沿折线子段生成包围多边形（覆盖中心线，保证真实相交而非仅 bbox）。"""
    import math
    poly = _seg_poly(seg_key)
    p0, p1 = _point_at(poly, t0), _point_at(poly, t1)
    # 取中点处切线法向
    pm = _point_at(poly, (t0 + t1) / 2)
    pn = _point_at(poly, min(1.0, (t0 + t1) / 2 + 0.01))
    dx, dy = pn[0] - pm[0], pn[1] - pm[1]
    L = math.hypot(dx, dy) or 1
    nx, ny = -dy / L * pad, dx / L * pad
    # 沿子段两端额外做切向延伸
    tx0, ty0 = p0[0] - pn[0], p0[1] - pn[1]
    return [[p0[0] + nx, p0[1] + ny], [p1[0] + nx, p1[1] + ny],
            [p1[0] - nx, p1[1] - ny], [p0[0] - nx, p0[1] - ny]]


SEG_BY_KEY = {seg[0]: seg for seg in SEGMENTS}

# 少量弯曲（米）：打破长对角线共线，避免与无关线段产生伪交点
BENDS = {
    
    "S3":  [(330, 440), (420, 400)],
    "S4":  [(380, 575), (450, 560)],
    "S6":  [(600, 230), (660, 210)],
    "S8":  [(760, 320), (770, 280)],
    "S10": [(760, 200), (700, 150), (600, 120)],
    "S12": [(140, 450), (170, 470)],
    "S13": [(680, 300), (690, 240)],
    "S14": [(620, 160), (560, 120)],
    "S15": [(150, 470), (130, 440)],
    "S16": [(640, 380), (560, 375)],
}



# 每条公告：(ann_id, 来源, 级别, 标题, observed_at, status, 修订, 正文, 规则dict)
ANNOUNCEMENTS = [
    ("ANN1", "西山市政", "市级", "西山崖电梯道路施工（跨日）",
     "2026-10-05T09:00", "active", None,
     "西山崖电梯道（崖下平台至北门段）全封闭施工，10月5日22:00至8日06:00。",
     dict(rule_key="R_CLIFF", effect="block", rule_type="construction",
          title="崖电梯道封闭施工", geom_type="polyline",
          geometry=_subline("S2", 0.05, 0.95, 0),
          direction="B",
          windows=[{"start": "2026-10-05T22:00", "end": "2026-10-08T06:00"}])),
    ("ANN2", "景区物业", "景区", "西山专线南段夜间紧急抢修",
     "2026-10-06T15:00", "active", None,
     "每日 22:30–次日05:00 夜间抢修，仅封闭南行（崖下平台→南湖码头方向）。",
     dict(rule_key="R_S5_NIGHT", effect="block", rule_type="construction",
          title="西山南段夜间抢修（南行封闭）", geom_type="polyline",
          geometry=_subline("S5", 0.06, 0.94, 0),
          direction="F",
          weekly={"days": [0, 1, 2, 3, 4, 5, 6], "start": "22:30",
                  "end": "05:00", "cross_day": True})),
    ("ANN3", "景区物业", "景区", "西岗夜间全封闭（施工+管控双提醒）",
     "2026-10-06T16:00", "active", None,
     "西岗通道（南湖码头—崖下平台—望海亭）每日22:00至次日06:00全封闭。",
     dict(rule_key="R_WEST_NIGHT", effect="block", rule_type="control",
          title="西岗夜间全封闭", geom_type="polyline",
          geometry=_subline("S5", 0.04, 0.96, 0) + [pt("YX")] + _subline("S6", 0.05, 0.96, 0),
          direction="B",
          weekly={"days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00",
                  "end": "06:00", "cross_day": True})),
    ("ANN4", "交警支队", "市级", "临时桥交通管制（撤文迟到）",
     "2026-10-04T10:00", "active", None,
     "临时桥道路 10月6日00:00–10月8日23:59 双向交通管制，行人禁行。",
     dict(rule_key="R_BRIDGE", effect="block", rule_type="control",
          title="临时桥双向管制", geom_type="polyline",
          geometry=_subline("S7", 0.05, 0.95, 0),
          direction="B",
          windows=[{"start": "2026-10-06T00:00", "end": "2026-10-08T23:59"}])),
    ("ANN5", "电梯维保单位", "运营方", "东门连廊电梯例行维护",
     "2026-10-06T09:00", "active", None,
     "东门连廊电梯10月7日08:00–18:00停运维护，轮椅不可通行。",
     dict(rule_key="R_ELEV", effect="block", rule_type="accessibility",
          title="连廊电梯维护停运", geom_type="polyline",
          geometry=_subline("S13", 0.05, 0.95, 0),
          direction="B", population="wheelchair",
          windows=[{"start": "2026-10-07T08:00", "end": "2026-10-07T18:00"}])),
    ("ANN6", "景区客服", "景区", "东门电梯正常运行（来源矛盾）",
     "2026-10-07T08:40", "active", None,
     "游客咨询回复称东门连廊电梯今日正常开放，无需绕行。",
     dict(rule_key="R_ELEV", effect="allow", rule_type="accessibility",
          title="客服称电梯正常", geom_type="polyline",
          geometry=_subline("S13", 0.1, 0.9, 0),
          direction="B", population="wheelchair",
          windows=[{"start": "2026-10-07T08:00", "end": "2026-10-07T18:00"}])),
    ("ANN7", "水文站", "运营方", "南湖码头水位提示（信息类）",
     "2026-10-07T07:00", "active", None,
     "南湖码头水位偏高，上下船请注意；不构成封航。",
     dict(rule_key="R_WATER", effect="info", rule_type="info",
          title="南湖水位提示", geom_type="polyline",
          geometry=_subline("S3", 0.3, 0.7, 0),
          direction="B")),
    ("ANN8", "景区物业", "景区", "南湖景区维修（同名陷阱：只覆盖局部土路）",
     "2026-10-06T11:00", "active", None,
     "‘南湖景区’临江土路局部维修；公告名称与多条‘南湖’路线同名，但实际只封闭该土路中段。",
     dict(rule_key="R_NANHU_NAME", effect="block", rule_type="construction",
          title="临江土路中段维修", geom_type="polygon",
          geometry=_box_around("S11", 0.30, 0.70, 16),
          direction="B",
          windows=[{"start": "2026-10-06T00:00", "end": "2026-10-09T23:59"}])),
]


def seed(conn=None):
    own = conn is None
    if own:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        conn = connect()
    cur = conn.cursor()
    for nid, (name, x, y) in NODES.items():
        cur.execute("INSERT INTO nodes VALUES(?,?,?,?)", (nid, name, x, y))
    for key, name, a, b, attrs, weekly in SEGMENTS:
        cur.execute(
            "INSERT INTO segments(seg_key,name,from_node,to_node,bends,attrs,service_weekly)"
            " VALUES(?,?,?,?,?,?,?)",
            (key, name, a, b, json.dumps(BENDS.get(key, []), ensure_ascii=False),
             json.dumps(attrs, ensure_ascii=False),
             json.dumps(weekly) if weekly else None))
    for rid, ver, name, kind, note, members in ROUTES:
        v = ver if isinstance(ver, int) else 1
        cur.execute(
            "INSERT INTO routes(route_id,version,name,kind,note,current,valid_from)"
            " VALUES(?,?,?,?,?,?,?)",
            (rid, v, name, kind, note, 1 if (rid == "R1" and v == 2) else
             (1 if rid != "R1" else 0),
             "2026-10-01T00:00" if v == 1 else "2026-10-06T12:00"))
        for i, (seg, rev) in enumerate(members):
            cur.execute("INSERT INTO route_members VALUES(?,?,?,?,?)",
                        (rid, v, i, seg, rev))
    for ann in ANNOUNCEMENTS:
        _insert_announcement(conn, ann, base_time="2026-10-01T00:00")
    from .engine import compute_matches
    for r in conn.execute("SELECT id FROM rules"):
        compute_matches(conn, r["id"])
    bump(conn)
    if own:
        conn.commit()
        conn.close()


def _insert_announcement(conn, ann, base_time):
    (ann_id, source, level, title, observed, status, revision_of, body, rule) = ann
    conn.execute(
        "INSERT INTO announcements(ann_id,source,source_level,title,status,observed_at,"
        "revision_of,body) VALUES(?,?,?,?,?,?,?,?)",
        (ann_id, source, level, title, status, observed, revision_of, body))
    conn.execute(
        "INSERT INTO rules(rule_key,ann_id,version,status,effect,rule_type,title,"
        "geom_type,geometry,direction,windows,weekly,min_age,population,note,"
        "valid_from,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rule["rule_key"], ann_id, 1, "active", rule["effect"], rule["rule_type"],
         rule["title"], rule.get("geom_type", "polyline"),
         json.dumps(rule["geometry"], ensure_ascii=False),
         rule.get("direction", "B"),
         json.dumps(rule.get("windows"), ensure_ascii=False) if rule.get("windows") else None,
         json.dumps(rule.get("weekly"), ensure_ascii=False) if rule.get("weekly") else None,
         rule.get("min_age"), rule.get("population"), rule.get("title"),
         base_time, observed))


if __name__ == "__main__":
    seed()
    print("seeded")
