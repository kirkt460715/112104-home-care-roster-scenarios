"""备班方案：先比较，再决定哪些现在就要定。

一个方案由三样东西组成：

- ``trainings``：现在报名的培训（开课日、名额、费用、准入条件都按资料核对）；
- ``activations``：现在预留的外援时段（同一借班时段只能落一站，按日期占用）；
- ``fills``：具体补哪个班。留到当天需求明朗再定，比较时不提前锁死。

比较时所有方案共用同一组未来工时情景、同一批共享人员和不可移动的预约。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .data import Dataset, Policy, View, hhmm
from .forecast import Forecast, Scenario
from .forecast import BandCapacity
from .roster import (BANDS, Visit, course_enrollments, make_context, solve)

UNCHANGED = "现状"


@dataclass(frozen=True)
class Option:
    name: str
    trainings: frozenset[tuple[str, str]] = frozenset()   # (course, worker)
    activations: frozenset[tuple[str, dt.date, str]] = frozenset()  # (capacity, day, site)
    note: str = ""

    def describe(self) -> str:
        parts = []
        if self.trainings:
            parts.append("培训 " + "、".join(f"{c}→{w}" for c, w in sorted(self.trainings)))
        if self.activations:
            grouped: dict[tuple[str, str], list[dt.date]] = {}
            for capacity, day, site in self.activations:
                grouped.setdefault((capacity, site), []).append(day)
            parts.append("借班 " + "；".join(
                f"{capacity}→{site} " + "、".join(d.isoformat() for d in sorted(days))
                for (capacity, site), days in sorted(grouped.items())))
        return "；".join(parts) if parts else self.note or "不动用培训与外援"


@dataclass
class Admission:
    """准入核对的结果：阻挡项不能绕过，后果项要在方案比较里讲清。"""

    blockers: list[str] = field(default_factory=list)
    consequences: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def render(self) -> list[str]:
        return [f"不可行：{item}" for item in self.blockers] + \
               [f"代价：{item}" for item in self.consequences]


@dataclass
class OptionResult:
    option: Option
    scenarios: int
    satisfied_mean: float
    satisfied_p10: float
    satisfied_p90: float
    family_gap_mean: float
    gap_reasons: dict[str, int]
    by_day: dict[dt.date, tuple[float, float]]  # 满足/家庭缺口 均值
    cost: int
    admission: Admission = field(default_factory=Admission)

    def as_row(self) -> dict[str, object]:
        return {
            "方案": self.option.name,
            "内容": self.option.describe(),
            "满足班段(均值)": round(self.satisfied_mean, 1),
            "满足班段(P10-P90)": f"{self.satisfied_p10:.0f}-{self.satisfied_p90:.0f}",
            "家庭缺口(均值)": round(self.family_gap_mean, 2),
            "主要缺口": "、".join(
                f"{reason}×{count}" for reason, count
                in sorted(self.gap_reasons.items(), key=lambda kv: -kv[1])[:3]),
            "费用": self.cost,
        }


# 可用容量 --------------------------------------------------------------------


def capacity_for(view: View, dataset: Dataset, day: dt.date,
                 activations: Iterable[tuple[str, dt.date, str]] = (),
                 training: Iterable[tuple[str, str]] = ()
                 ) -> dict[str, dict[int, BandCapacity]]:
    """当天各站各时段的可用人证，已扣除培训脱岗、固定预约与借班冲突。"""
    context = make_context(view, dataset, day, activations=activations,
                           extra_training=training, reserve_commitments=True)
    from .roster import free_minutes
    result: dict[str, dict[int, dict[str, int]]] = {}
    eligible: dict[str, dict[int, dict[str, set[str]]]] = {}
    for worker, state in context.workers.items():
        for site in state.windows:
            for index, (band_start, band_end) in enumerate(BANDS):
                minutes = free_minutes(context, worker, site, band_start, band_end)
                if minutes <= 0:
                    continue
                result.setdefault(site, {}).setdefault(index, {})[worker] = int(minutes)
                eligible.setdefault(site, {}).setdefault(index, {})[worker] = \
                    set(state.credentials)
    return {site: {index: BandCapacity(result[site][index], eligible[site][index])
                   for index in result[site]}
            for site in result}


def policy_for(dataset: Dataset, round_name: str) -> Policy | None:
    return dataset.policy.get(round_name)


def check_option(dataset: Dataset, view: View, option: Option,
                 plan_start: dt.date, policy: Policy | None) -> Admission:
    """核对准入：开课日、名额、费用上限、报名条件、借班日期与站点唯一性。

    区分"根本不能这么定"（阻挡）与"能定但要付代价"（后果）。例如培训开课晚于
    班期开始并不阻挡报名，但证书生效前不能用于上门，这段缺口要算进方案。
    """
    result = Admission()
    enrolled_now = {(c, w) for c, w in course_enrollments(view)}
    for course_id, worker in sorted(option.trainings):
        course = view.courses.get(course_id)
        if course is None:
            result.blockers.append(f"{course_id} 不在可见课程表里")
            continue
        if worker not in course.eligible:
            result.blockers.append(f"{course_id} 本期报名条件不含 {worker}")
        if course.starts < view.as_of:
            result.blockers.append(f"{course_id} 已于 {course.starts} 开课，不能再报名")
        booked = [w for c, w in option.trainings if c == course_id]
        if len(booked) > course.seats:
            result.blockers.append(
                f"{course_id} 只有 {course.seats} 个名额，方案占 {len(booked)} 个")
        if course.cost > (policy.max_course_cost if policy else course.cost):
            result.blockers.append(
                f"{course_id} 费用 {course.cost} 超过额度 {policy.max_course_cost}")
        if (course_id, worker) in enrolled_now:
            result.consequences.append(f"{worker} 已经在本期 {course_id} 名单里，不重复占用名额")
        if course.earliest_cert > course.ends:
            result.consequences.append(
                f"{course_id} 结业 {course.ends}，最早 {course.earliest_cert} 才能拿证；"
                "这之前不能用于上门")
        if course.starts < plan_start:
            result.consequences.append(
                f"{course_id} 开课 {course.starts} 早于班期开始 {plan_start}，"
                "开课到拿证之间学员脱岗")
    if policy:
        total_cost = sum(view.courses[c].cost for c, _ in option.trainings
                         if c in view.courses)
        if total_cost > policy.max_course_cost:
            result.blockers.append(
                f"培训费用合计 {total_cost} 超过额度 {policy.max_course_cost}")
    by_capacity: dict[str, set[dt.date]] = {}
    by_day: dict[tuple[str, dt.date], str] = {}
    for capacity_id, day, site in sorted(option.activations):
        by_capacity.setdefault(capacity_id, set()).add(day)
        current = by_day.get((capacity_id, day))
        if current is not None and current != site:
            result.blockers.append(f"{capacity_id} 在 {day} 同时被派到 {current} 与 {site}")
        by_day[(capacity_id, day)] = site
        available = [a for a in view.availability
                     if a.capacity_id == capacity_id and a.site == site]
        if not available or not any(a.covers(day) for a in available):
            result.blockers.append(f"{capacity_id} 在 {day} 没有到 {site} 的可用时段")
        confirmed = [a for a in view.active_agreements("loan")
                     if a.subject == capacity_id and a.starts == day and a.site != site]
        if confirmed:
            result.blockers.append(
                f"{capacity_id} 在 {day} 已确认给 {confirmed[0].site}，"
                f"改派 {site} 需先正式撤销原借班")
        pending = [a for a in view.active_agreements("loan")
                   if a.subject == capacity_id and a.starts == day and a.site == site]
        if pending:
            result.consequences.append(
                f"{capacity_id} 在 {day} 已确认到 {site}，本次预留与既有借班相同")
    if policy:
        for capacity_id, days in by_capacity.items():
            if len(days) > policy.max_loan_days:
                result.blockers.append(
                    f"{capacity_id} 借用 {len(days)} 天，超过 {policy.max_loan_days} 天上限")
    return result


# 方案评价 --------------------------------------------------------------------


def _scenario_visits(scenario: Scenario, day: dt.date, stopped: Sequence[str]
                     ) -> list[Visit]:
    visits = []
    for index, demand in enumerate(scenario.visits):
        if demand.day != day or demand.site in stopped:
            continue
        visits.append(Visit(
            key=demand.request or f"{day}{demand.site}{demand.service}{index}",
            site=demand.site, service=demand.service,
            earliest=demand.start, latest_start=demand.window_end,
            duration=demand.duration, request=demand.request))
    return visits


def evaluate_option(dataset: Dataset, forecast: Forecast, option: Option,
                    sample: int | None = None) -> OptionResult:
    view = dataset.view(forecast.as_of)
    policy = policy_for(dataset, forecast.round.name)
    scenarios = forecast.scenarios if sample is None else forecast.scenarios[:sample]
    satisfied: list[int] = []
    family_gaps: list[int] = []
    reasons: dict[str, int] = {}
    by_day: dict[dt.date, list[tuple[int, int]]] = {}
    for scenario in scenarios:
        total = family = 0
        for day_forecast in forecast.days:
            if day_forecast.stopped_sites:
                continue
            visits = _scenario_visits(scenario, day_forecast.day,
                                      day_forecast.stopped_sites)
            context = make_context(view, dataset, day_forecast.day, visits,
                                   activations=option.activations,
                                   extra_training=option.trainings)
            result = solve(context, max_nodes=40_000)
            served = len(result.assignments)
            missing_family = sum(1 for gap in result.gaps
                                 if gap.visit.family_confirmed)
            total += served
            family += missing_family
            by_day.setdefault(day_forecast.day, []).append((served, missing_family))
            for gap in result.gaps:
                reasons[gap.reason] = reasons.get(gap.reason, 0) + 1
        satisfied.append(total)
        family_gaps.append(family)
    from .forecast import percentile
    admission = check_option(dataset, view, option, forecast.round.starts, policy)
    cost = sum(view.courses[c].cost for c, _ in option.trainings if c in view.courses)
    day_summary = {day: (sum(pair[0] for pair in rows) / len(rows),
                         sum(pair[1] for pair in rows) / len(rows))
                   for day, rows in by_day.items()}
    return OptionResult(
        option, len(scenarios),
        sum(satisfied) / len(satisfied) if satisfied else 0.0,
        percentile(satisfied, 0.10), percentile(satisfied, 0.90),
        sum(family_gaps) / len(family_gaps) if family_gaps else 0.0,
        reasons, day_summary, cost, admission)


def compare_options(dataset: Dataset, forecast: Forecast, options: Sequence[Option],
                    sample: int | None = None) -> list[OptionResult]:
    """同一组情景、同一批共享人员与不可移动预约，逐个方案评价。"""
    return [evaluate_option(dataset, forecast, option, sample=sample)
            for option in options]


# 现在必须定 vs 到时还能换 ----------------------------------------------------


@dataclass
class Decision:
    kind: str          # enroll / reserve / defer
    subject: str
    detail: str
    must_decide_by: dt.date | None
    reversible: bool


def decisions_for(view: View, option: Option) -> list[Decision]:
    """把方案里的动作分成三类：现在报名、现在预留、当天再定。"""
    decisions: list[Decision] = []
    for course_id, worker in sorted(option.trainings):
        course = view.courses.get(course_id)
        if course is None:
            continue
        decisions.append(Decision(
            "enroll", f"{course_id}→{worker}",
            f"开课 {course.starts} 至 {course.ends}，脱岗 "
            f"{hhmm(course.start_minute)}-{hhmm(course.end_minute)}；"
            f"最早 {course.earliest_cert} 发证，之前不能上门",
            course.starts, False))
    by_capacity: dict[tuple[str, str], list[dt.date]] = {}
    for capacity_id, day, site in sorted(option.activations):
        by_capacity.setdefault((capacity_id, site), []).append(day)
    for (capacity_id, site), days in sorted(by_capacity.items()):
        first = min(days)
        decisions.append(Decision(
            "reserve", f"{capacity_id}→{site}",
            f"预留 {'、'.join(d.isoformat() for d in sorted(days))} 共 {len(days)} 天，"
            "同一时段只能给一个站，改动需提前两天",
            first - dt.timedelta(days=2), True))
    decisions.append(Decision(
        "defer", "具体补哪个班",
        "等该日需求明朗再落位；现在不锁死具体班次，只锁人与外援的可用性",
        None, True))
    return decisions
