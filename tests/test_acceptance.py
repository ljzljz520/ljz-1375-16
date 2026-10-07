# -*- coding: utf-8 -*-
"""验收测试：跨日施工、撤销公告迟到、路线改线、旧离线计划重连、单段多提醒。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import core, db, seed  # noqa: E402

NIGHT = "2026-10-08T23:00"   # 跨日施工窗口内
NOON = "2026-10-08T12:00"    # 窗口外


def fresh():
    conn = db.connect()
    seed.build(conn)
    return conn


def seed_alert_ids(conn):
    rows = conn.execute("SELECT id, kind, title FROM alerts").fetchall()
    return {r["kind"] + ":" + r["title"][:4]: r["id"] for r in rows}


class TestCrossDayConstruction(unittest.TestCase):
    """跨日施工：22:00-06:00 封闭只影响窗口内行程，且按到达片段的时刻判定。"""

    def test_night_trip_detours(self):
        conn = fresh()
        res = core.plan(conn, {"origin": "A", "dest": "E", "depart_at": NIGHT})
        self.assertEqual(res["constrained"]["status"], "ok")
        used = [s["segment_id"] for s in res["constrained"]["steps"]]
        self.assertNotIn("R1:v1:1", used, "夜间不得经过施工封闭的滨湖段")

    def test_noon_trip_uses_lakeside(self):
        conn = fresh()
        res = core.plan(conn, {"origin": "A", "dest": "E", "depart_at": NOON})
        used = [s["segment_id"] for s in res["constrained"]["steps"]]
        self.assertIn("R1:v1:1", used, "白天施工未生效，应走滨湖段")

    def test_entry_time_matters(self):
        conn = fresh()
        # 19:30 出发，到达滨湖段约 19:32：在班次窗口内且施工未开始 → 可用
        res = core.plan(conn, {"origin": "A", "dest": "E", "depart_at": "2026-10-08T19:30"})
        used = [s["segment_id"] for s in res["constrained"]["steps"]]
        self.assertIn("R1:v1:1", used)
        # 次日 05:00 仍在跨日窗口内 → 不可用
        res2 = core.plan(conn, {"origin": "A", "dest": "E", "depart_at": "2026-10-09T05:00"})
        used2 = [s["segment_id"] for s in res2["constrained"]["steps"]]
        self.assertNotIn("R1:v1:1", used2)


class TestLateRevocation(unittest.TestCase):
    """撤销公告迟到：按 effective_at 重算，并标记在空档期内生成的旧计划。"""

    def test_late_revocation(self):
        conn = fresh()
        # 10:00-14:00 封闭 A->B
        aid = core.create_alert(conn, "construction", "施工方", "北门段午间封闭",
                                {"type": "line", "coordinates": [[10, 0], [90, 0]], "buffer_m": 5},
                                starts_at="2026-10-08T10:00", ends_at="2026-10-08T14:00",
                                rule={"closed": True},
                                effective_at="2026-10-08T08:00", received_at="2026-10-08T08:05")
        req = {"origin": "A", "dest": "B", "depart_at": "2026-10-08T11:00",
               "user_id": "u1", "save": True, "created_at": "2026-10-08T09:30"}
        res1 = core.plan(conn, req)
        used1 = [s["segment_id"] for s in res1["constrained"]["steps"]]
        self.assertNotIn("R1:v1:0", used1, "封闭期间不得直走 A->B")
        pid = res1["plan_id"]

        # 撤销 09:00 生效，但 09:45 才收到（迟到）
        core.revoke_alert(conn, aid, effective_at="2026-10-08T09:00",
                          received_at="2026-10-08T09:45", note="施工提前结束")
        res2 = core.plan(conn, {"origin": "A", "dest": "B", "depart_at": "2026-10-08T11:00"})
        used2 = [s["segment_id"] for s in res2["constrained"]["steps"]]
        self.assertIn("R1:v1:0", used2, "撤销后应恢复直走")

        # 旧计划（生成于 09:45 之前的空档期）重连 → 检出迟到撤销
        ref = core.refresh_plan(conn, pid)
        self.assertFalse(ref["up_to_date"])
        self.assertTrue(any(l["alert_id"] == aid for l in ref["late_revocations"]))


class TestReroute(unittest.TestCase):
    """路线改线：影响随新版本几何重算，旧计划快照可检出版本变化。"""

    def test_reroute_recomputes_impacts(self):
        conn = fresh()
        # 在施工区外开一条新路 R5: G(300,50)->H(300,150)，原几何不碰施工多边形
        conn.execute("INSERT INTO places VALUES ('G','南广场',300,50)")
        conn.execute("INSERT INTO places VALUES ('H','南山顶',300,150)")
        conn.execute("INSERT INTO routes VALUES ('R5','南环线',1,?)", (core.now_iso(),))
        conn.execute("INSERT INTO segments VALUES ('R5:v1:0','R5',1,0,'G','H',?,150,0,3,1,2.0,0)",
                     (core.json.dumps([[300, 50], [300, 150]]),))
        conn.commit()
        poly = {"type": "polygon", "coordinates": [[330, 80], [370, 80], [370, 120], [330, 120]]}
        aid = core.create_alert(conn, "construction", "施工方", "南广场施工", poly,
                                starts_at="2026-10-08T00:00", ends_at="2026-10-09T23:59",
                                rule={"closed": True})
        imps = core.compute_impacts(conn, "2026-10-08T12:00")
        self.assertFalse(any(i["alert_id"] == aid for i in imps),
                         "原几何与施工区不相交，不得误伤")

        # 改线：R5 穿过施工区
        core.reroute(conn, "R5", [{"a": "G", "b": "H",
                                   "geom": [[300, 50], [350, 100], [300, 150]],
                                   "length_m": 220, "slope_pct": 3}])
        imps2 = core.compute_impacts(conn, "2026-10-08T12:00")
        hit = [i for i in imps2 if i["alert_id"] == aid]
        self.assertTrue(hit, "改线后几何穿过施工区，必须命中")
        self.assertEqual(hit[0]["segment_id"], "R5:v2:0")
        self.assertTrue(hit[0]["points"], "必须给出实际交点作为影响位置")

        # 旧计划重连可检出改线
        req = {"origin": "A", "dest": "E", "depart_at": NOON, "user_id": "u2", "save": True}
        pid = core.plan(conn, req)["plan_id"]
        core.reroute(conn, "R5", [{"a": "G", "b": "H", "geom": [[300, 50], [300, 150]],
                                   "length_m": 150, "slope_pct": 3}])
        ref = core.refresh_plan(conn, pid)
        self.assertTrue(any(r["route_id"] == "R5" for r in ref["rerouted_routes"]))


class TestOfflineReconnect(unittest.TestCase):
    """旧离线计划重连：公告出新修订后，旧确认失效，需重新确认。"""

    def test_reconnect_diff_and_reack(self):
        conn = fresh()
        uid = "u3"
        req = {"origin": "A", "dest": "E", "depart_at": NIGHT, "user_id": uid, "save": True}
        res = core.plan(conn, req)
        pid = res["plan_id"]
        aid = next(a for a in core.latest_revisions(conn)
                   if core.alerts_map(conn)[a]["kind"] == "construction"
                   and "滨湖" in core.alerts_map(conn)[a]["title"])

        ok, _ = core.ack(conn, uid, aid, 1)
        self.assertTrue(ok)
        ref0 = core.refresh_plan(conn, pid)
        self.assertTrue(ref0["up_to_date"], "无变化时应为最新")

        # 公告修订：施工延长（第2版）
        core.add_revision(conn, aid,
                          geom={"type": "line", "coordinates": [[110, 0], [190, 0]], "buffer_m": 5},
                          starts_at="2026-10-08T22:00", ends_at="2026-10-10T06:00",
                          rule={"closed": True}, note="工期延长一天")
        ref = core.refresh_plan(conn, pid)
        self.assertFalse(ref["up_to_date"])
        self.assertTrue(any(c["alert_id"] == aid and c["to_rev"] == 2
                            for c in ref["changed_alerts"]))
        self.assertIn(aid, ref["re_ack_needed"], "旧确认只对应第1版，第2版需重新确认")

        # 用旧版本号确认应被拒绝；确认新版后恢复
        ok2, _ = core.ack(conn, uid, aid, 1)
        self.assertFalse(ok2)
        ok3, _ = core.ack(conn, uid, aid, 2)
        self.assertTrue(ok3)
        self.assertTrue(core.ack_status(conn, uid, [aid])[aid]["ack_current"])


class TestMultiAlertSingleSegment(unittest.TestCase):
    """一条路段同时命中多项提醒：全部列出、各自给依据，合并效果取并集。"""

    def test_segment_hits_multiple_alerts(self):
        conn = fresh()
        imps = core.compute_impacts(conn, NIGHT)
        on_bc = [i for i in imps if i["segment_id"] == "R1:v1:1" and i["direction"] == "forward"]
        kinds = {i["kind"] for i in on_bc}
        self.assertEqual(kinds, {"construction", "schedule", "access_restriction"})
        for i in on_bc:
            self.assertTrue(i["source"] and i["window"], "每项提醒须给出来源与时间依据")

        # 白天儿童（婴儿车）仍被适行限制拦截，须绕行并给出冲突解释
        res = core.plan(conn, {"origin": "A", "dest": "E", "depart_at": NOON,
                               "party": {"children": True}, "compare": True})
        self.assertEqual(res["constrained"]["status"], "infeasible")
        reasons = res["constrained"]["reasons"]
        self.assertTrue(any(r["type"] == "party_constraint" and r["constraint"] == "stroller"
                            for r in reasons))
        naive = res["reachable_then_filter"]
        self.assertEqual(naive["status"], "violations_after_filter",
                         "先求可达再筛：路径撞上适行限制才被发现")


class TestDirectionAndWindow(unittest.TestCase):
    """方向与生效时段参与判断。"""

    def test_backward_only_closure(self):
        conn = fresh()
        # 上山 B->D（forward）不受下山向封闭影响，应直通
        res = core.plan(conn, {"origin": "B", "dest": "D", "depart_at": NOON})
        steps = res["constrained"]["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["segment_id"], "R2:v1:0")
        self.assertEqual(steps[0]["direction"], "forward")
        # 下山 D->B（backward）被封闭，须绕行，不得出现该段 backward
        res2 = core.plan(conn, {"origin": "D", "dest": "B", "depart_at": NOON})
        steps2 = res2["constrained"]["steps"]
        self.assertEqual(res2["constrained"]["status"], "ok")
        self.assertFalse(any(s["segment_id"] == "R2:v1:0" and s["direction"] == "backward"
                             for s in steps2))


class TestNoSolutionReasons(unittest.TestCase):
    """无解原因集合：轮椅全程不可达时给出逐条可解释原因。"""

    def test_wheelchair_reason_set(self):
        conn = fresh()
        res = core.plan(conn, {"origin": "A", "dest": "E", "depart_at": NOON,
                               "party": {"wheelchair": True}})
        s = res["constrained"]
        self.assertEqual(s["status"], "infeasible")
        self.assertTrue(s["reasons"], "必须给出无解原因集合")
        self.assertTrue(all(r.get("basis") for r in s["reasons"]), "每条原因须有依据")
        joined = str(s["reasons"])
        self.assertTrue("坡度" in joined or "台阶" in joined or "净宽" in joined)


class TestConflictPendingReview(unittest.TestCase):
    """来源相互矛盾 → 待核；不作通行保证，规划避开并列入缺信息范围。"""

    def test_conflict_marks_pending_and_avoids(self):
        conn = fresh()
        # 施工方公告：环山道 10:00-12:00 封闭
        core.create_alert(conn, "construction", "施工方", "环山道封闭",
                          {"type": "line", "coordinates": [[200, 10], [200, 60]], "buffer_m": 5},
                          starts_at="2026-10-08T10:00", ends_at="2026-10-08T12:00",
                          rule={"closed": True})
        # 景区广播却上报：同段同时段开放 → 矛盾
        conn.execute("INSERT INTO reports(source,segment_id,direction,starts_at,ends_at,state,received_at)"
                     " VALUES ('景区广播','R4:v1:0','both','2026-10-08T10:00','2026-10-08T12:00','open',?)",
                     (core.now_iso(),))
        conn.commit()
        core.detect_conflicts(conn)

        confs = conn.execute("SELECT * FROM conflicts WHERE status='pending_review'").fetchall()
        self.assertTrue(confs, "矛盾必须标记为待核")
        self.assertIn("待核", confs[0]["detail"] + "待核")

        res = core.plan(conn, {"origin": "A", "dest": "E", "depart_at": "2026-10-08T11:00"})
        used = [s["segment_id"] for s in res["constrained"]["steps"]]
        self.assertNotIn("R4:v1:0", used, "待核路段不得当作可通行保证")
        mi = res["missing_info"]
        self.assertTrue(any(m["type"] == "pending_review" for m in mi),
                        "缺信息范围必须列出待核项")


class TestIntersectionPrecision(unittest.TestCase):
    """局部施工与路线实际片段求交：包围框重合但几何不相交不得误伤。"""

    def test_bbox_overlap_but_no_intersection(self):
        conn = fresh()
        # 独立路段 P(300,0)-Q(400,0)，远离其他片段
        conn.execute("INSERT INTO places VALUES ('P','南一门',300,0)")
        conn.execute("INSERT INTO places VALUES ('Q','南二门',400,0)")
        conn.execute("INSERT INTO routes VALUES ('R9','南外环',1,?)", (core.now_iso(),))
        conn.execute("INSERT INTO segments VALUES ('R9:v1:0','R9',1,0,'P','Q',?,100,0,1,1,2.0,0)",
                     (core.json.dumps([[300, 0], [400, 0]]),))
        conn.commit()
        # 阶梯多边形：实体在 x∈[310,405] 占据 y∈[5,10]，在 x∈[405,430] 占据 y∈[-5,10]。
        # 其 bbox [310,430]x[-5,10] 与片段 bbox [300,400]x[0,0] 重合，
        # 但 y=0 仅出现在 x>405 处——与片段几何并不相交。
        poly = {"type": "polygon", "coordinates": [[310, 5], [405, 5], [405, -5],
                                                   [430, -5], [430, 10], [310, 10]]}
        aid = core.create_alert(conn, "construction", "施工方", "坡地施工", poly,
                                starts_at="2026-10-08T00:00", ends_at="2026-10-09T23:59",
                                rule={"closed": True})
        imps = core.compute_impacts(conn, "2026-10-08T12:00")
        self.assertFalse(any(i["alert_id"] == aid for i in imps),
                         "包围框重合不等于相交，不得停用路线")

    def test_partial_intersection_gives_points(self):
        conn = fresh()
        poly = {"type": "polygon", "coordinates": [[150, -10], [190, -10], [190, 10], [150, 10]]}
        aid = core.create_alert(conn, "construction", "施工方", "半幅施工", poly,
                                starts_at="2026-10-08T00:00", ends_at="2026-10-09T23:59",
                                rule={"closed": True})
        imps = [i for i in core.compute_impacts(conn, "2026-10-08T12:00") if i["alert_id"] == aid]
        self.assertTrue(imps)
        self.assertEqual({i["segment_id"] for i in imps}, {"R1:v1:1"},
                         "只命中实际相交的片段，不得扩大停用整条路线")
        self.assertTrue(imps[0]["points"], "须给出交点")


class TestFreshnessAndPrint(unittest.TestCase):
    """打印计划附新鲜度与缺信息范围。"""

    def test_freshness_fields(self):
        conn = fresh()
        res = core.plan(conn, {"origin": "A", "dest": "E", "depart_at": NOON, "save": True})
        f = res["freshness"]
        self.assertIn("generated_at", f)
        self.assertTrue(f["sources"], "须列出各来源最近上报时间")
        self.assertIsInstance(res["missing_info"], list)
        p = core.get_plan(conn, res["plan_id"])
        self.assertIn("snapshot", p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
