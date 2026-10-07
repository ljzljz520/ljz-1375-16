"""验收测试：跨日施工、撤销迟到、路线改线、旧离线计划重连、
同段多提醒、同名陷阱、方向/班次、人群冲突、矛盾待核、两策略对比、
无解原因集合、确认版本绑定、打印新鲜度/缺信息。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, planner, support
from app.database import connect, jloads
from app.seed import seed

T_DAY = "2026-10-07T10:00"      # 周三 白天
T_NIGHT = "2026-10-07T23:30"    # 跨夜施工当晚
T_NEXT_EARLY = "2026-10-08T03:00"
T_NEXT_DAY = "2026-10-08T10:00"
T_ENDED = "2026-10-09T10:00"


class Base(unittest.TestCase):
    def setUp(self):
        self.c = connect(":memory:")
        seed(self.c)


class TestGeometryIntersection(Base):
    def test_same_name_trap_only_hits_real_segment(self):
        """ANN8 名称含“南湖”，但只与 S11 几何相交：R5(S15) 不得停用。"""
        rid = self.c.execute(
            "SELECT id FROM rules WHERE ann_id='ANN8'").fetchone()["id"]
        ms = [m["seg_key"] for m in engine.matched_segments(self.c, rid)]
        self.assertEqual(ms, ["S11"])
        # 白天 S15（R5 唯一线段）依然开放；S11 封闭
        es15 = engine.edge_state(self.c, "S15", False, T_DAY, {})
        es11 = engine.edge_state(self.c, "S11", False, T_DAY, {})
        self.assertEqual(es15["state"], "open")
        self.assertEqual(es11["state"], "blocked")

    def test_matches_are_not_bbox_or_name(self):
        pairs = {"ANN1": ["S2"], "ANN2": ["S5"], "ANN4": ["S7"],
                 "ANN5": ["S13"], "ANN7": ["S3"]}
        for ann, expect in pairs.items():
            rid = self.c.execute(
                "SELECT id FROM rules WHERE ann_id=?", (ann,)).fetchone()["id"]
            self.assertEqual(sorted(m["seg_key"] for m in
                                    engine.matched_segments(self.c, rid)), expect)

    def test_point_only_contact_does_not_block(self):
        """规则折线在共享节点与 S1 点接触(point-only)，不据此封闭 S1。"""
        es = engine.edge_state(self.c, "S1", False, T_NIGHT, {})
        codes = [r["code"] for r in es["reasons"]]
        self.assertNotIn("BLOCKED_CONTROL", codes)


class TestCrossDay(Base):
    def test_crossday_window_active_before_and_after_midnight(self):
        es = engine.edge_state(self.c, "S2", False, T_NIGHT, {})
        self.assertEqual(es["state"], "blocked")
        self.assertEqual(es["reasons"][0]["code"], "BLOCKED_CONSTRUCTION")
        es2 = engine.edge_state(self.c, "S2", False, T_NEXT_EARLY, {})
        self.assertEqual(es2["state"], "blocked")

    def test_crossday_window_ends(self):
        es = engine.edge_state(self.c, "S2", False, T_NEXT_DAY, {})
        self.assertNotIn("BLOCKED_CONSTRUCTION",
                         [r["code"] for r in es["reasons"]])

    def test_weekly_cross_night_shift_and_closure(self):
        # S3 渡轮 08:30-17:00
        self.assertEqual(engine.edge_state(self.c, "S3", False, T_NIGHT, {})["state"],
                         "blocked")
        self.assertEqual(engine.edge_state(self.c, "S3", False, T_DAY, {})["state"],
                         "open")
        # 跨夜规则 22:30-05:00：23:30 与次日 03:00 都生效，白天失效
        self.assertIn("BLOCKED_CONSTRUCTION",
                      [r["code"] for r in
                       engine.edge_state(self.c, "S5", False, T_NIGHT, {})["reasons"]])
        self.assertIn("BLOCKED_CONSTRUCTION",
                      [r["code"] for r in
                       engine.edge_state(self.c, "S5", False, T_NEXT_EARLY, {})["reasons"]])
        es_day = engine.edge_state(self.c, "S5", False, T_DAY, {})
        self.assertEqual(es_day["state"], "open")


class TestDirectionAndMultipleHits(Base):
    def test_direction_specific_rule(self):
        # ANN2 direction=F（数字化 SL->YX）：南行封、北行不适用
        south = engine.edge_state(self.c, "S5", False, T_NIGHT, {})
        north = engine.edge_state(self.c, "S5", True, T_NIGHT, {})
        self.assertIn("BLOCKED_CONSTRUCTION",
                      [r["code"] for r in south["reasons"]])
        self.assertNotIn("BLOCKED_CONSTRUCTION",
                         [r["code"] for r in north["reasons"]])

    def test_same_segment_multiple_alerts(self):
        """S5 在 23:30 同时命中 ANN2(抢修) 与 ANN3(夜间全封闭)。"""
        es = engine.edge_state(self.c, "S5", False, T_NIGHT, {})
        anns = {r["ann_id"] for r in es["reasons"]}
        self.assertIn("ANN2", anns)
        self.assertIn("ANN3", anns)


class TestConflictPending(Base):
    def test_contradicting_sources_become_pending_not_guarantee(self):
        es = engine.edge_state(self.c, "S13", False, T_DAY, {"wheelchair": True})
        self.assertEqual(es["state"], "pending")
        self.assertTrue(es["pending"])
        # 信息来源必须同时呈现
        srcs = {p["a"] for p in es["pending"]} | {p["b"] for p in es["pending"]}
        self.assertEqual(srcs, {"ANN5", "ANN6"})

    def test_pending_is_not_guaranteed(self):
        res = planner.plan_prune(self.c, "G_S", "G_N", T_DAY, {"wheelchair": True})
        # 存在路径（S13 待核不武断封闭），但任何含待核边的方案不发通行保证
        # 若最优方案不含待核边则 guaranteed=True
        for f in res["feasible"]:
            if any(p["pending"] for p in f["pending"]):
                self.assertFalse(f["guaranteed"])


class TestLateRevocation(Base):
    def test_late_revocation_bitemporal(self):
        # 10-07 08:00 撤销生效，但撤文 09:30 才收录
        engine.revoke_announcement(self.c, "ANN4", "2026-10-07T08:00",
                                   "2026-10-07T09:30")
        # 09:00 的查询者（撤文尚未收录）仍应看到 ANN4
        vis = {r["ann_id"] for r in
               engine.visible_rules(self.c, "2026-10-07T07:30", "2026-10-07T09:00")}
        self.assertIn("ANN4", vis)
        # 10:00 的查询者已收到撤文：ANN4 消失，S7 恢复
        vis2 = {r["ann_id"] for r in
                engine.visible_rules(self.c, T_DAY, T_DAY)}
        self.assertNotIn("ANN4", vis2)
        es = engine.edge_state(self.c, "S7", False, T_DAY, {})
        self.assertEqual(es["state"], "open")

    def test_revocation_after_window_is_recorded_but_history_intact(self):
        # 撤文 10-09 才收录（极晚）：10-08 白天查询者尚不知情 → 仍见 ANN4
        engine.revoke_announcement(self.c, "ANN4", "2026-10-07T20:00",
                                   "2026-10-09T12:00")
        vis_late = {r["ann_id"] for r in
                    engine.visible_rules(self.c, T_NEXT_DAY, T_NEXT_DAY)}
        self.assertIn("ANN4", vis_late)
        # 10-09 撤文收录后：撤销生效点之后的查询不再见
        vis_after = {r["ann_id"] for r in
                     engine.visible_rules(self.c, T_ENDED, T_ENDED)}
        self.assertNotIn("ANN4", vis_after)
        # 回看撤销生效点之前（10-06）的历史，规则依然在（撤销不回改历史）
        vis_hist = {r["ann_id"] for r in
                    engine.visible_rules(self.c, "2026-10-06T12:00", T_ENDED)}
        self.assertIn("ANN4", vis_hist)


class TestReroute(Base):
    def test_reroute_version_and_diff(self):
        v1 = engine.evaluate_route(self.c, "R1", T_DAY, {}, version=1)
        v2 = engine.evaluate_route(self.c, "R1", T_DAY, {}, version=2)
        self.assertEqual(v1["state"], "blocked")
        self.assertTrue(any(e["seg_key"] == "S2" and e["state"] == "blocked"
                            for e in v1["edges"]))
        self.assertEqual(v2["state"], "open")
        self.assertFalse(any(e["seg_key"] == "S2" for e in v2["edges"]))

    def test_add_new_route_version(self):
        v = support.add_route_version(
            self.c, "R1", "湖光主环线", "步道", "v3 测试改线",
            [{"seg_key": "S0"}, {"seg_key": "S16"}, {"seg_key": "S8"},
             {"seg_key": "S10"}], T_NEXT_DAY)
        self.assertEqual(v, 3)
        cur = self.c.execute(
            "SELECT version FROM routes WHERE route_id='R1' AND current=1"
        ).fetchone()["version"]
        self.assertEqual(cur, 3)


class TestPopulationConflictExplanations(Base):
    def test_wheelchair_blocked_by_steps_and_elevator_works(self):
        es = engine.edge_state(self.c, "S6", False, T_DAY, {"wheelchair": True})
        self.assertEqual(es["reasons"][0]["code"], "WHEELCHAIR_INACCESSIBLE")
        # R3 无障碍专线白天：S2 被施工封
        r3 = engine.evaluate_route(self.c, "R3", T_DAY, {"wheelchair": True})
        keys = {(e["seg_key"], e["state"]) for e in r3["edges"]}
        self.assertIn(("S2", "blocked"), keys)

    def test_elderly_forbidden_explained_on_s6(self):
        es = engine.edge_state(self.c, "S6", False, T_DAY, {"elderly": True})
        self.assertIn("ELDERLY_FORBIDDEN",
                      [r["code"] for r in es["reasons"]])
        # 老人走其它普通石板路不受影响
        self.assertEqual(engine.edge_state(
            self.c, "S0", False, T_DAY, {"elderly": True})["state"], "open")

    def test_child_age_limit_local_not_global(self):
        young = engine.edge_state(self.c, "S6", False, T_DAY, {"child_age": 6})
        old = engine.edge_state(self.c, "S6", False, T_DAY, {"child_age": 10})
        self.assertIn("AGE_TOO_LOW", [r["code"] for r in young["reasons"]])
        self.assertEqual(old["state"], "open")
        # 儿童在 S0 上不受年龄限制（冲突解释定位到具体线段）
        self.assertEqual(engine.edge_state(
            self.c, "S0", False, T_DAY, {"child_age": 3})["state"], "open")


class TestPlannerComparisonAndNoSolution(Base):
    def test_filter_then_screen_finds_same_route_but_evalutes_more_edges(self):
        f = planner.plan_filter(self.c, "G_S", "G_N", T_DAY, {})
        p = planner.plan_prune(self.c, "G_S", "G_N", T_DAY, {})
        self.assertTrue(f["feasible"])
        self.assertTrue(p["feasible"])
        # 先枚举后筛选：评估了大量死路边；直接搜索评估更少
        self.assertGreater(f["metrics"]["edge_checks"],
                           p["metrics"]["edge_checks"])
        self.assertGreater(f["metrics"]["candidates"], 0)
        self.assertEqual(p["metrics"]["candidates"], 0)
        # filter 模式附带被拒路径与原因
        self.assertTrue(any(x["reasons"] for x in f["rejected"]))

    def test_no_solution_reason_set_night(self):
        # 夜间：西侧施工+夜间封闭，渡轮停班；东侧通道仍开放
        p_before = planner.plan_prune(self.c, "G_S", "G_N", T_NIGHT, {})
        self.assertTrue(p_before["feasible"])
        # 再封南门全部出口（S0/S4/S11）→ 无解
        import json as _json
        for i, seg in enumerate(["S0", "S4", "S11"]):
            _, poly = engine.segment_polyline(self.c, seg)
            self.c.execute(
                "INSERT INTO rules(rule_key,ann_id,version,status,effect,rule_type,"
                "title,geom_type,geometry,direction,weekly,observed_at,valid_from)"
                " VALUES(?,'ANNSG',1,'active','block','control',?,'polyline',?,'B',"
                "?,?,?)",
                (f"SG{i}", seg + "出口封闭", _json.dumps(poly),
                 _json.dumps({"days": [0, 1, 2, 3, 4, 5, 6],
                              "start": "00:00", "end": "23:59"}),
                 T_NIGHT, T_NIGHT))
            rid = self.c.execute(
                "SELECT id FROM rules WHERE rule_key=?", (f"SG{i}",)).fetchone()["id"]
            engine.compute_matches(self.c, rid)
        res = planner.plan_prune(self.c, "G_S", "G_N", T_NIGHT, {})
        self.assertFalse(res["feasible"])
        codes = {r["code"] for r in res["no_solution"]["reasons"]}
        self.assertIn("BLOCKED_CONTROL", codes)
        self.assertIn("control", res["no_solution"]["critical"])

    def test_filter_mode_explains_rejected_paths(self):
        f = planner.plan_filter(self.c, "G_S", "G_N", T_NIGHT, {})
        self.assertTrue(any(
            any(r["code"] in ("BLOCKED_CONSTRUCTION", "BLOCKED_CONTROL",
                              "OUTSIDE_SHIFT") for r in x["reasons"])
            for x in f["rejected"]))


class TestOfflineReconnect(Base):
    def _snapshot(self, at, person):
        plan = planner.plan_prune(self.c, "G_S", "G_N", at, person)
        routes = [{"route_id": r["route_id"], "version": r["version"]}
                  for r in self.c.execute(
                      "SELECT route_id,version FROM routes WHERE current=1")]
        rules = [{"rule_key": r["rule_key"], "version": r["version"],
                  "ann_id": r["ann_id"], "title": r["title"]}
                 for r in engine.active_rules(self.c, at)]
        payload = {"start": "G_S", "goal": "G_N", "routes_snapshot": routes,
                   "rules_snapshot": rules, "plan": plan}
        return support.save_offline_plan(self.c, "u1", at, payload)

    def test_reconnect_detects_changes_and_warns(self):
        pid = self._snapshot("2026-10-06T20:00", {"wheelchair": True})
        # 之后：ANN4 撤销（迟到）、ANN1 被修订、新增 ANN9
        engine.revoke_announcement(self.c, "ANN4", "2026-10-07T08:00",
                                   "2026-10-07T09:30")
        import json as _json
        _, poly2 = engine.segment_polyline(self.c, "S2")
        engine.ingest_announcement(self.c, {
            "ann_id": "ANN9", "source": "景区物业", "source_level": "景区",
            "title": "新增夜间提示", "observed_at": "2026-10-07T21:00",
            "rule": {"rule_key": "R_NEW", "effect": "info",
                     "rule_type": "info", "title": "新增夜间提示",
                     "geometry": poly2}})
        out = support.reconnect(self.c, pid, "2026-10-08T10:00",
                                {"wheelchair": True})
        self.assertIn("旧计划不得继续作为通行保证", out["warning"])
        rev_anns = {x["ann_id"] for x in out["revoked"]}
        self.assertIn("ANN4", rev_anns)
        new_anns = {x["ann_id"] for x in out["new_or_changed"]}
        self.assertIn("ANN9", new_anns)
        # R1 当前 v2，快照 v2（种子当前版本即 v2）→ 验证 reroutes 类型可用
        self.assertIn("current_plan", out)


class TestAcknowledgementVersion(Base):
    def test_ack_binds_to_version(self):
        rid = self.c.execute(
            "SELECT id FROM rules WHERE ann_id='ANN1' AND status='active'"
        ).fetchone()["id"]
        support.acknowledge(self.c, "u1", rid, "2026-10-05T22:30")
        st = support.ack_status(self.c, "u1", rid)
        self.assertTrue(st["acknowledged"])
        self.assertFalse(st["has_new_version"])
        # 提交 ANN1 修订 → 新版本出现，旧确认不隐藏新变化
        import json as _json
        _, poly = engine.segment_polyline(self.c, "S2")
        engine.ingest_announcement(self.c, {
            "ann_id": "ANN1", "source": "西山市政", "source_level": "市级",
            "title": "崖电梯施工修订", "observed_at": "2026-10-07T12:00",
            "rule": {"rule_key": "R_CLIFF", "effect": "block",
                     "rule_type": "construction", "title": "夜间施工(修订)",
                     "geometry": poly,
                     "weekly": {"days": [0, 1, 2, 3, 4, 5, 6],
                                "start": "20:00", "end": "06:00",
                                "cross_day": True}}})
        st2 = support.ack_status(self.c, "u1", rid)
        self.assertTrue(st2["has_new_version"])


class TestPrint(Base):
    def test_print_contains_freshness_missing_and_reasons(self):
        res = planner.plan_prune(self.c, "G_S", "G_N", T_NIGHT, {})
        out = support.print_plan(self.c, res, T_NIGHT, {})
        self.assertIn("数据新鲜度", out["text"])
        self.assertIn("缺信息范围", out["text"])
        self.assertIn(out["missing"]["coverage_note"], out["text"])
        # 无保障解的打印
        res2 = planner.plan_prune(self.c, "G_S", "G_N", T_NIGHT, {"wheelchair": True})
        out2 = support.print_plan(self.c, res2, T_NIGHT, {"wheelchair": True})
        self.assertTrue("通行保证" in out2["text"] or "无可行路线" in out2["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
