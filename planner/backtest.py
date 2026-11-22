"""区间覆盖的后验检验：预测给的分位数区间，在历史上真的罩得住吗。

做法是"只向前看"：对每个回看日，只用当天及以前能看到的数据重做一次预测，
再拿真实的完整工时去对照。这样可以区分两件事：

- 中心偏低：P50 系统性低于实际 → 排班按中位数留人就会常年少人；
- 区间欠覆盖：实际落在 P10-P90 之外的比例高于 20% → 缓冲区比标称的窄，
  一旦超载就是"多个时段一起超"，而不是偶发一个班。

检验只用完整工时；停办窗口与结算不全的日子不参与，避免把不可比的日子
当成模型误差。
"""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from statistics import mean

from .data import Dataset, Round
from .forecast import forecast_round, percentile


@dataclass
class CoveragePoint:
    day: dt.date
    site: str
    actual: int
    p10: float
    p50: float
    p90: float

    @property
    def covered(self) -> bool:
        return self.p10 <= self.actual <= self.p90

    @property
    def below(self) -> bool:
        return self.actual > self.p90


@dataclass
class Backtest:
    points: list[CoveragePoint]
    skipped: list[tuple[dt.date, str]]
    notes: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        if not self.points:
            return 0.0
        return sum(1 for p in self.points if p.covered) / len(self.points)

    @property
    def above_p90(self) -> float:
        if not self.points:
            return 0.0
        return sum(1 for p in self.points if p.below) / len(self.points)

    @property
    def below_p10(self) -> float:
        if not self.points:
            return 0.0
        return sum(1 for p in self.points if p.actual < p.p10) / len(self.points)

    def by_site(self) -> dict[str, float]:
        result = {}
        for site in sorted({p.site for p in self.points}):
            rows = [p for p in self.points if p.site == site]
            result[site] = sum(1 for p in rows if p.covered) / len(rows) if rows else 0.0
        return result


def backtest_round(dataset: Dataset, round_name: str,
                   scenarios: int = 400, min_cell: int = 10,
                   days: int = 20) -> Backtest:
    """逐日重放：只给预测看前一天为止的资料，再拿完整工时核对。

    这里检验的是"工时长短"的不确定性，而不是"申请会不会来"：只把后来真的
    完成了的申请放进对照，取消和没人接的申请两边都剔除。否则需求数与完成数
    混在一起，会让人误以为工期模型偏了。
    """
    plan = dataset.round(round_name)
    points: list[CoveragePoint] = []
    skipped: list[tuple[dt.date, str]] = []
    truth = dataset.view(dt.date.max)
    for offset in range(1, days + 1):
        day = plan.starts + dt.timedelta(days=offset)
        as_of = day - dt.timedelta(days=1)
        view = dataset.view(as_of)
        stops = {c.site for c in view.calendar if c.day == day and c.kind == "stop"}
        mini = Round("回放", as_of, day, day)
        result = forecast_round(dataset, round_name, as_of=as_of,
                                scenarios=scenarios, min_cell=min_cell, plan=mini)
        history = result.history
        if not history.comparable:
            skipped.append((day, "没有可用的历史样本"))
            continue
        # 排班时看得见、后来也确实完成的申请，才拿来和工期区间比
        visible_done = []
        for request in view.requests:
            if request.day != day or request.site in stops:
                continue
            events = truth.event_map(request.request)
            started, returned = events.get("started"), events.get("returned")
            if started and returned and started.start_minute is not None \
                    and returned.end_minute is not None:
                visible_done.append((request,
                                     returned.end_minute - started.start_minute))
        if not visible_done:
            skipped.append((day, "当天没有完整工时"))
            continue
        market = day in view.market_days()
        donors = history.donors(market) or history.comparable
        randomiser = random.Random(f"{day}")
        by_site: dict[str, int] = {}
        for request, minutes in visible_done:
            by_site[request.site] = by_site.get(request.site, 0) + minutes
        for site, actual in by_site.items():
            requests = [r for r, _ in visible_done if r.site == site]
            totals = []
            for _ in range(scenarios):
                donor = randomiser.choice(donors)
                total = 0.0
                for request in requests:
                    total += randomiser.gauss(
                        history.service_mean.get(request.service, 60.0)
                        + donor.day_factor, max(1.0, history.idiosyncratic_sd))
                totals.append(total)
            points.append(CoveragePoint(day, site, actual,
                                        percentile(totals, 0.10),
                                        percentile(totals, 0.50),
                                        percentile(totals, 0.90)))
    notes = _coverage_notes(points, skipped)
    return Backtest(points, skipped, notes)


def _coverage_notes(points: list[CoveragePoint], skipped) -> list[str]:
    if not points:
        return ["没有足够的历史完整工时做覆盖检验。"]
    coverage = sum(1 for p in points if p.covered) / len(points)
    above = sum(1 for p in points if p.below) / len(points)
    below = sum(1 for p in points if p.actual < p.p10) / len(points)
    notes = [f"回看 {len(points)} 个站点日：P10-P90 覆盖 {coverage:.0%}，"
             f"高于 P90 的占 {above:.0%}、低于 P10 的占 {below:.0%}"
             "（标称各约 10%）。"]
    if coverage >= 0.80:
        notes.append("区间基本罩得住：按 P90 留人时，多数日子不会额外超载。")
    else:
        notes.append("区间偏窄：按 P90 留人仍会在相当多的日子被突破，"
                     "实际班表需要在这之上再留一档缓冲，或提前把外援锁死。")
    if above > 0.15:
        notes.append("实际高于 P90 偏多，说明低估主要集中在需求偏高的日子，"
                     "这些日子更要按多时段联合超载来备人，而不是逐班补。")
    if below > 0.15:
        notes.append("实际低于 P10 也偏多：区间下沿留得过宽，低需求日会把人力"
                     "闲在站里；这些日子可以较晚再决定是否动用外援。")
    underestimated = [p for p in points if p.actual > p.p50]
    if underestimated:
        gap = mean(p.actual - p.p50 for p in underestimated)
        notes.append(f"落在 P50 之上的站点日平均高出 {gap:.0f} 分钟；"
                     "若照 P50 排班，这些差额会直接变成排不上的班或延误。")
    if skipped:
        notes.append("未参与检验的日期：" + "、".join(
            f"{d.isoformat()}({why})" for d, why in skipped[:5])
            + ("等" if len(skipped) > 5 else "") + "。")
    return notes
