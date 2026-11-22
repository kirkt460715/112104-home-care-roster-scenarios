"""业务口径测试：可见性、完整工时、资质组合、外援与培训。"""
import datetime as dt
import unittest

from planner import backtest, decide, forecast, review, roster
from planner.data import Dataset

DAY = dt.date


class VisibilityTest(unittest.TestCase):
    """排班日之后才登记的事实不能倒灌进当天的判断。"""

    @classmethod
    def setUpClass(cls):
        cls.dataset = Dataset.load()

    def test_late_return_not_counted_at_planning_time(self):
        # V00010 的返回时间 2026-06-04 才登记，排班日 2026-06-01 看不到
        day = DAY(2026, 6, 1)
        before = self.dataset.view(day)
        after = self.dataset.view(DAY(2026, 6, 4))
        early = {r.request for r, _ in before.completed_visits()}
        later = {r.request for r, _ in after.completed_visits()}
        self.assertNotIn("V00010", early)
        self.assertIn("V00010", later)
        trace = review.trace_request(before, self.dataset.request("V00010"))
        self.assertFalse(trace.visible.complete)
        self.assertEqual(trace.visible.kind, "open")
        self.assertTrue(trace.actual.complete)

    def test_only_started_and_returned_give_minutes(self):
        view = self.dataset.view(DAY(2026, 6, 4))
        completed = {r.request: minutes for r, minutes in view.completed_visits()}
        events = view.event_map("V00010")
        self.assertIn("started", events)
        self.assertIn("returned", events)
        self.assertEqual(completed["V00010"],
                         events["returned"].end_minute - events["started"].start_minute)
        # 明确没人接、真实没人申请都不产生工时
        for request_id in ("V00000",):
            self.assertNotIn(request_id, completed)
        self.assertEqual(view.event_map("V00000")["unassigned"].kind, "unassigned")

    def test_unassigned_and_canceled_are_different(self):
        view = self.dataset.view(dt.date.max)
        kinds = {event.kind for events in view.events.values() for event in events}
        self.assertIn("unassigned", kinds)
        self.assertIn("canceled", kinds)
        # 两者都拿不到工时：unassigned 是有人却没接上，canceled 是申请本身撤了
        working = {r.request for r, _ in view.completed_visits()}
        self.assertFalse(any(kind in ("unassigned",)
                             for request_id, events in view.events.items()
                             for kind in events
                             if request_id in working))

    def test_canceled_agreement_releases_only_after_record(self):
        # A006 在 2026-10-07 才被正式撤销，10-06 仍应视为占用
        before = self.dataset.view(DAY(2026, 10, 6))
        after = self.dataset.view(DAY(2026, 10, 7))
        self.assertIn("A006", {a.event for a in before.active_agreements("loan")})
        self.assertEqual(after.agreements["A006"].state, "cancelled")
        self.assertNotIn("A006", {a.event for a in after.active_agreements("loan")})


class QualificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = Dataset.load()
        cls.view = cls.dataset.view(DAY(2026, 10, 3))

    def test_certificate_not_valid_before_issue_date(self):
        # W4 参加 T1，最早 2026-10-13 才发证
        context = roster.make_context(self.view, self.dataset, DAY(2026, 10, 12), [])
        self.assertNotIn("B", context.workers["W4"].credentials)
        context = roster.make_context(self.view, self.dataset, DAY(2026, 10, 13), [])
        self.assertIn("B", context.workers["W4"].credentials)

    def test_training_absence_blocks_service(self):
        context = roster.make_context(self.view, self.dataset, DAY(2026, 10, 7), [])
        self.assertTrue(context.workers["W4"].absences)

    def test_two_person_service_cannot_use_same_worker_twice(self):
        visit = roster.Visit("X", "W", "bath", 480, 480, 75)
        context = roster.make_context(self.view, self.dataset, DAY(2026, 10, 9), [])
        for option in context.role_options(visit):
            self.assertEqual(len(set(option)), 2)
        # W5 一人持 B/H，但 W 站当天只有 W5/W6/W8，凑不出合规两人
        self.assertNotIn(("W5", "W5"), context.role_options(visit))

    def test_one_person_two_credentials_not_two_people(self):
        demand = {"B": 60, "H": 60}
        capacity = {"W5": {"total": 60}}
        eligible = {"W5": {"B", "H"}}
        self.assertEqual(roster.max_flow(demand, capacity, eligible), 60)

    def test_firm_credentials_exclude_pending_certificate(self):
        visit = roster.Visit("C", "S", "bath", 490, 490, 75,
                             committed=True, family_confirmed=True,
                             fixed_workers=("W4", "W3"), fixed_start=490)
        context = roster.make_context(self.view, self.dataset, DAY(2026, 10, 12),
                                      [], reserve_commitments=False)
        result = roster.solve(context)
        self.assertFalse(result.served_keys & {"C"})


class LoanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = Dataset.load()

    def test_loan_slot_not_double_counted_between_sites(self):
        with self.assertRaises(ValueError):
            roster.make_context(self.dataset.view(DAY(2026, 10, 9)), self.dataset,
                                DAY(2026, 10, 9), [],
                                activations=[("L1", DAY(2026, 10, 9), "N"),
                                             ("L1", DAY(2026, 10, 9), "S")])

    def test_confirmed_loan_cannot_be_silently_moved(self):
        # 10-06 那天 A006 还视为占用：L2 已确认到 W
        view = self.dataset.view(DAY(2026, 10, 6))
        context = roster.make_context(view, self.dataset, DAY(2026, 10, 9), [],
                                      activations=[("L2", DAY(2026, 10, 9), "S")])
        self.assertIn("W", context.workers["W8"].windows)
        self.assertNotIn("S", context.workers["W8"].windows)

    def test_option_leaving_one_site_blocked_before_booking(self):
        view = self.dataset.view(DAY(2026, 10, 6))
        option = decide.Option("改派", activations=frozenset(
            {("L2", DAY(2026, 10, 9), "S")}))
        admission = decide.check_option(self.dataset, view, option,
                                        DAY(2026, 10, 5), None)
        self.assertFalse(admission.ok)


class ForecastTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = Dataset.load()
        cls.result = forecast.forecast_round(cls.dataset, "R4", scenarios=120)

    def test_known_requests_are_a_floor(self):
        day = DAY(2026, 10, 9)
        cell = self.result.day(day).cells_by_key[("N", "bath")]
        self.assertGreaterEqual(cell.minutes.p10, 1)
        self.assertGreaterEqual(cell.visits.p10, 1)

    def test_bands_partition_the_day(self):
        # 时段是整天的不重叠切分：各段均值的和应等于整日均值
        for day in self.result.days:
            for cell in day.cells:
                band_mean = sum(band.mean for band in cell.bands)
                self.assertAlmostEqual(band_mean, cell.minutes.mean, places=6)
                self.assertLessEqual(band_mean, cell.minutes.p90 + 1e-9)

    def test_stopped_site_excluded(self):
        day = DAY(2026, 10, 10)
        self.assertEqual(self.result.day(day).stopped_sites, ("S",))
        self.assertNotIn("S", {c.site for c in self.result.day(day).cells})

    def test_joint_overload_uses_same_scenario(self):
        view = self.dataset.view(self.result.as_of)
        capacity = {d: decide.capacity_for(view, self.dataset, d)
                    for d in self.result.round.days()}
        risks = forecast.band_risks(self.result, capacity)
        self.assertTrue(risks)
        for item in risks:
            self.assertLessEqual(item.joint_probability, 1.0)
            self.assertGreaterEqual(item.joint_probability, 0.0)

    def test_calendar_effects_only_use_prior_records(self):
        # 10-10 S 的停办在 09-29 登记，10-03 排班时应已可见
        view = self.dataset.view(DAY(2026, 10, 3))
        self.assertTrue(view.stopped(DAY(2026, 10, 10), "S"))

    def test_history_uses_only_complete_visits(self):
        view = self.dataset.view(DAY(2026, 9, 14))
        history = forecast.build_history(view, limits=(DAY(2026, 6, 1), DAY(2026, 9, 13)))
        for day in history.comparable:
            for _, _, minutes in day.durations:
                self.assertGreater(minutes, 0)


class ReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = Dataset.load()

    def test_round_review_separates_visible_and_actual(self):
        item = review.review_round(self.dataset, "R4")
        visible = item.outcome_counts(actual=False)
        actual = item.outcome_counts(actual=True)
        self.assertIn("open", visible)
        self.assertTrue(any(k != "open" for k in actual))
        self.assertTrue(item.notes)

    def test_review_lists_three_kinds_of_windows(self):
        item = review.review_round(self.dataset, "R4")
        self.assertTrue(item.comparable_days())
        self.assertTrue(item.stopped_days())
        # 排班时可见、后来也结清工时的日子，才允许拿来和当初的建议对照
        for day in item.comparable_days():
            self.assertFalse(day.stopped_sites)
            self.assertTrue(all(t.actual.kind in ("completed", "canceled", "unassigned")
                                for t in day.visible_traces))
        # 停办窗口单列，不混进可比样本
        stopped = {d.day for d in item.stopped_days()}
        self.assertFalse(stopped & {d.day for d in item.comparable_days()})
        # 低支撑组合要能被点名
        self.assertTrue(review.review_round(self.dataset, "R1").low_support_combos())

    def test_fulfilment_splits_estimate_and_execution(self):
        item = review.review_round(self.dataset, "R4")
        recommended = review.recommended_plan(self.dataset, "R4")
        rows = review.compare_fulfilment(item, recommended)
        self.assertTrue(rows)
        settled = [row for row in rows if row.shortage or "全部完成" in row.note]
        self.assertTrue(settled)
        for row in rows:
            self.assertGreaterEqual(row.estimate_share, 0.0)
            self.assertGreaterEqual(row.execution_share, 0.0)
            if row.estimate_share + row.execution_share > 0:
                self.assertAlmostEqual(row.estimate_share + row.execution_share, 1.0)
            if "尚无履约结论" in row.note:
                self.assertEqual(row.estimate_share + row.execution_share, 0.0)

    def test_recommendation_comes_from_visible_facts_only(self):
        plan = self.dataset.round("R4")
        recommended = review.recommended_plan(self.dataset, "R4")
        view = self.dataset.view(plan.as_of)
        visible = {r.request for r in view.requests}
        for day, requests in recommended.items():
            self.assertLessEqual(requests, visible)


class BacktestTest(unittest.TestCase):
    def test_coverage_reports_both_tails(self):
        dataset = Dataset.load()
        item = backtest.backtest_round(dataset, "R2", scenarios=80, days=8)
        self.assertTrue(item.points)
        self.assertGreaterEqual(item.coverage, 0.0)
        self.assertLessEqual(item.coverage, 1.0)
        self.assertGreaterEqual(item.above_p90, 0.0)
        self.assertGreaterEqual(item.below_p10, 0.0)
        self.assertTrue(any("标称" in note for note in item.notes))


if __name__ == "__main__":
    unittest.main()


class DispatchTest(unittest.TestCase):
    """方案确认之后仍要按真实时段复核，不能整包照发。"""

    def setUp(self):
        from planner.data import Agreement
        self.dataset = Dataset.load()
        self.dataset.agreements.append(Agreement(
            "A099", "loan", "L2", "W8", "W", DAY(2026, 10, 12),
            480, 690, "occupied", DAY(2026, 10, 8)))

    def test_slot_taken_elsewhere_only_that_day_is_blocked(self):
        from planner import dispatch
        option = decide.Option("借 L2 到 S", activations=frozenset({
            ("L2", DAY(2026, 10, 12), "S")}))
        items = dispatch.build_dispatches(self.dataset.view(DAY(2026, 10, 3)), option)
        self.assertTrue(all(item.status == "ready" for item in items))
        later = dispatch.recheck(self.dataset, DAY(2026, 10, 8), items, option)
        blocked = [item for item in later if item.status != "ready"]
        self.assertEqual({(item.day, item.site) for item in blocked},
                         {(DAY(2026, 10, 12), "S")})
        self.assertIn("W", blocked[0].reason)

    def test_effects_are_scoped_to_real_bands(self):
        from planner import dispatch
        option = decide.Option("借 L2 到 S", activations=frozenset({
            ("L2", DAY(2026, 10, 12), "S")}))
        items = dispatch.build_dispatches(self.dataset.view(DAY(2026, 10, 3)), option)
        later = dispatch.recheck(self.dataset, DAY(2026, 10, 8), items, option)
        changed = [item for item in later if item.status != "ready"]
        spots = dispatch.affected_bands(items, changed)
        self.assertEqual(spots, ["2026-10-12 S"])

    def test_training_already_started_cannot_be_dropped(self):
        from planner import dispatch
        option = decide.Option("报 T1", trainings=frozenset({("T1", "W4")}))
        items = dispatch.build_dispatches(self.dataset.view(DAY(2026, 10, 3)), option)
        later = dispatch.recheck(self.dataset, DAY(2026, 10, 7), items, option)
        self.assertEqual(later[0].status, "locked")
        self.assertIn("脱岗", later[0].reason)
        after = dispatch.recheck(self.dataset, DAY(2026, 10, 14), items, option)
        self.assertEqual(after[0].status, "superseded")
