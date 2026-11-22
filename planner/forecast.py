"""按班期估计工时分布与不确定性。

模型保持可解释：

- 历史日只取当时能看到完整工时的日子；结算不全的日子单列，明确停办的窗口剔除；
- 整日重抽样（donor day），所以同站同天各项服务会一起变紧或一起宽松；
- 单班工时 = 服务均值 + 当日松紧 + 个别波动，日松紧在三个站之间共享；
- 已知派单是不可再低的下界，未知需求按同口径历史日程补足；
- 输出分位数而不是单一均值，并保留"多个时段同时吃紧"的联合概率。
"""
from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import Iterable, Sequence

from .data import Dataset, Request, Round, View, hhmm
from .roster import BANDS, overlap, round_role_demand, spread_minutes

SERVICES = ("bath", "escort", "clean")


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@dataclass(frozen=True)
class Summary:
    count: int
    mean: float
    p10: float
    p50: float
    p90: float

    @classmethod
    def of(cls, values: Iterable[float]) -> "Summary":
        data = [float(v) for v in values]
        if not data:
            return cls(0, 0.0, 0.0, 0.0, 0.0)
        return cls(len(data), mean(data), percentile(data, 0.10),
                   percentile(data, 0.50), percentile(data, 0.90))

    def as_dict(self) -> dict[str, float]:
        return {"mean": round(self.mean, 1), "p10": round(self.p10, 1),
                "p50": round(self.p50, 1), "p90": round(self.p90, 1)}


# 历史日 ----------------------------------------------------------------------


@dataclass
class HistoryDay:
    day: dt.date
    market: bool
    counts: dict[tuple[str, str], int]
    starts: dict[tuple[str, str], list[int]]
    deadlines: dict[tuple[str, str], list[int]]
    durations: list[tuple[str, str, int]]
    day_factor: float = 0.0

    @property
    def visits(self) -> int:
        return len(self.durations)


@dataclass
class History:
    comparable: list[HistoryDay]
    excluded: list[tuple[dt.date, str]]
    stopped: list[tuple[dt.date, str]]
    service_mean: dict[str, float]
    service_sd: dict[str, float]
    idiosyncratic_sd: float
    start_ladder: dict[str, list[int]]
    low_support: list[str]

    def donors(self, market: bool) -> list[HistoryDay]:
        return [d for d in self.comparable if d.market == market]


def build_history(view: View, limits: tuple[dt.date, dt.date] | None = None,
                  min_cell: int = 10) -> History:
    requests_by_day: dict[dt.date, list[Request]] = {}
    for request in view.requests:
        requests_by_day.setdefault(request.day, []).append(request)
    completed = view.completed_visits()
    completed_by_day: dict[dt.date, list[tuple[Request, int]]] = {}
    for request, minutes in completed:
        completed_by_day.setdefault(request.day, []).append((request, minutes))

    market_days = view.market_days()
    comparable: list[HistoryDay] = []
    excluded: list[tuple[dt.date, str]] = []
    stopped: list[tuple[dt.date, str]] = []

    service_values: dict[str, list[int]] = {}
    for request, minutes in completed:
        service_values.setdefault(request.service, []).append(minutes)
    service_mean = {s: mean(v) for s, v in service_values.items()}
    service_sd = {s: (pstdev(v) if len(v) > 1 else 0.0) for s, v in service_values.items()}

    # 同站同日内的残差：当日松紧之外还剩多少个别波动
    within: list[float] = []
    by_site_day: dict[tuple[dt.date, str], list[float]] = {}
    for request, minutes in completed:
        by_site_day.setdefault((request.day, request.site), []).append(
            minutes - service_mean.get(request.service, 0.0))
    for values in by_site_day.values():
        if len(values) > 1:
            centre = mean(values)
            within.extend(v - centre for v in values)
    idiosyncratic_sd = pstdev(within) if len(within) > 1 else 5.0

    for day in sorted(requests_by_day):
        if limits and not (limits[0] <= day <= limits[1]):
            continue
        stopped_here = {c.site for c in view.calendar
                        if c.day == day and c.kind == "stop"}
        for site in sorted(stopped_here):
            # 停办按站点记录：整天一律剔除会把其他站的可比样本一起丢掉
            stopped.append((day, site))
        rows = [r for r in requests_by_day[day] if r.site not in stopped_here]
        done = [(request, minutes) for request, minutes in completed_by_day.get(day, [])
                if request.site not in stopped_here]
        done_ids = {request.request for request, _ in done}
        pending = [request.request for request in rows
                   if request.request not in done_ids
                   and "started" in view.event_map(request.request)
                   and "returned" not in view.event_map(request.request)]
        if pending:
            excluded.append((day, f"{len(pending)} 条班次尚未返回"))
            continue
        if not done:
            excluded.append((day, "没有完整工时"))
            continue
        counts: dict[tuple[str, str], int] = {}
        starts: dict[tuple[str, str], list[int]] = {}
        deadlines: dict[tuple[str, str], list[int]] = {}
        for request in rows:
            key = (request.site, request.service)
            counts[key] = counts.get(key, 0) + 1
            starts.setdefault(key, []).append(request.earliest)
            deadlines.setdefault(key, []).append(request.deadline)
        durations = [(request.site, request.service, minutes)
                     for request, minutes in done]
        day_factor = mean(minutes - service_mean.get(request.service, 0.0)
                          for request, minutes in done)
        comparable.append(HistoryDay(day, day in market_days, counts,
                                     {k: sorted(v) for k, v in starts.items()},
                                     {k: sorted(v) for k, v in deadlines.items()},
                                     durations, day_factor))

    start_ladder: dict[str, list[int]] = {}
    for request in view.requests:
        start_ladder.setdefault(request.service, []).append(request.earliest)
    start_ladder = {s: sorted(set(v)) for s, v in start_ladder.items()}

    low_support: list[str] = []
    for service in sorted(service_mean):
        n = len(service_values[service])
        if n < min_cell:
            low_support.append(f"{service}: 仅 {n} 个完整工时样本")
    for service, values in start_ladder.items():
        for slot in values:
            count = sum(1 for r in view.requests
                        if r.service == service and r.earliest == slot)
            if count < min_cell:
                low_support.append(f"{service} {hhmm(slot)}: 仅 {count} 条派单")
    return History(comparable, excluded, stopped, service_mean, service_sd,
                   idiosyncratic_sd, start_ladder, low_support)


# 情景 ------------------------------------------------------------------------


@dataclass(frozen=True)
class DemandVisit:
    day: dt.date
    site: str
    service: str
    start: int
    duration: int
    known: bool
    request: str | None = None
    latest_start: int | None = None

    @property
    def window_end(self) -> int:
        """可办理窗口的最晚开始；未知需求至少留出与时长相等的余地。"""
        return self.latest_start if self.latest_start is not None else self.start

    @property
    def end(self) -> int:
        return self.start + self.duration


@dataclass
class Scenario:
    index: int
    visits: list[DemandVisit]
    donor: dt.date | None


@dataclass
class CellForecast:
    day: dt.date
    site: str
    service: str
    minutes: Summary
    visits: Summary
    bands: list[Summary]
    role_minutes: dict[str, Summary]
    known_visits: int


@dataclass
class DayForecast:
    day: dt.date
    market: bool
    stopped_sites: tuple[str, ...]
    cells: list[CellForecast]
    cells_by_key: dict[tuple[str, str], CellForecast] = field(default_factory=dict)

    @property
    def stopped(self) -> str | None:
        return "、".join(self.stopped_sites) if self.stopped_sites else None


@dataclass
class Forecast:
    round: Round
    as_of: dt.date
    days: list[DayForecast]
    scenarios: list[Scenario]
    history: History
    rules: dict[str, object]
    notes: list[str]

    def day(self, day: dt.date) -> DayForecast:
        for item in self.days:
            if item.day == day:
                return item
        raise KeyError(day)


# 主流程 ----------------------------------------------------------------------


def forecast_round(dataset: Dataset, round_name: str, as_of: dt.date | None = None,
                   scenarios: int = 400, seed: int = 20261122,
                   min_cell: int = 10,
                   history_days: int = 200,
                   plan: Round | None = None) -> Forecast:
    """按班期估计工时；``plan`` 可用于只预测某一天（覆盖检验会这么做）。"""
    plan = plan or dataset.round(round_name)
    view = dataset.view(as_of or plan.as_of)
    history = build_history(
        view, limits=(plan.starts - dt.timedelta(days=history_days),
                      plan.starts - dt.timedelta(days=1)),
        min_cell=min_cell)
    randomiser = random.Random(seed)

    known_by_day: dict[dt.date, list[Request]] = {}
    for request in view.requests:
        if not (plan.starts <= request.day <= plan.ends):
            continue
        if "started" in view.event_map(request.request):
            continue
        known_by_day.setdefault(request.day, []).append(request)

    scenarios_out: list[Scenario] = []
    for index in range(scenarios):
        visits: list[DemandVisit] = []
        for day in plan.days():
            stopped_here = {c.site for c in view.calendar
                            if c.day == day and c.kind == "stop"}
            market = day in view.market_days()
            donors = history.donors(market) or history.comparable
            donor = randomiser.choice(donors) if donors else None
            factor = donor.day_factor if donor else 0.0
            known = known_by_day.get(day, [])
            known_counts: dict[tuple[str, str], int] = {}
            for request in known:
                key = (request.site, request.service)
                known_counts[key] = known_counts.get(key, 0) + 1
            donor_counts = donor.counts if donor else {}
            sites = sorted({site for site, _ in list(donor_counts) + list(known_counts)}
                           - stopped_here)
            for site in sites:
                for service in SERVICES:
                    key = (site, service)
                    here = [r for r in known if r.site == site and r.service == service]
                    total = max(donor_counts.get(key, 0), len(here))
                    for request in here:
                        visits.append(DemandVisit(
                            day, site, service, request.earliest,
                            _sample_duration(randomiser, history, service, factor),
                            True, request.request, request.deadline))
                    # 未知需求沿用同一站同一服务的历史开始时段，避免凭空造出晚班
                    donor_starts = donor.starts.get(key, []) if donor else []
                    for offset in range(total - len(here)):
                        slot = (donor_starts[offset] if offset < len(donor_starts)
                                else (history.start_ladder.get(service) or [480])[0])
                        deadline = (donor.deadlines.get(key) or [None])[0] if donor else None
                        visits.append(DemandVisit(
                            day, site, service, slot,
                            _sample_duration(randomiser, history, service, factor),
                            False, None,
                            max(slot, deadline) if deadline is not None else slot))
        scenarios_out.append(Scenario(index, visits, donor.day if donor else None))

    day_forecasts = [_summarise_day(day, view, scenarios_out, dataset)
                     for day in plan.days()]
    notes = _notes(history, view)
    return Forecast(plan, view.as_of, day_forecasts, scenarios_out, history,
                    dataset.rules, notes)


def _sample_duration(randomiser: random.Random, history: History,
                     service: str, day_factor: float) -> int:
    base = history.service_mean.get(service, 60.0)
    sd = max(1.0, history.idiosyncratic_sd)
    return int(max(10, round(randomiser.gauss(base + day_factor, sd))))


def _summarise_day(day: dt.date, view: View, scenarios: Sequence[Scenario],
                   dataset: Dataset) -> DayForecast:
    stopped_sites = tuple(sorted({c.site for c in view.calendar
                                  if c.day == day and c.kind == "stop"}))
    market = day in view.market_days()
    cells: list[CellForecast] = []
    keys = sorted({(v.site, v.service) for s in scenarios for v in s.visits
                   if v.day == day and v.site not in stopped_sites})
    for site, service in keys:
        per_scenario: list[list[DemandVisit]] = []
        for scenario in scenarios:
            group = [v for v in scenario.visits
                     if v.day == day and v.site == site and v.service == service]
            per_scenario.append(group)
        bands = []
        for band_index, (band_start, band_end) in enumerate(BANDS):
            bands.append(Summary.of(
                sum(spread_minutes(v.start, v.window_end, v.duration,
                                   band_start, band_end) for v in group)
                for group in per_scenario))
        known = sum(1 for group in per_scenario for v in group if v.known)
        roles = dataset.rules[service].roles
        role_minutes = {
            role: Summary.of(sum(v.duration for v in group) for group in per_scenario)
            for role in roles}
        cells.append(CellForecast(
            day, site, service,
            Summary.of(sum(v.duration for v in group) for group in per_scenario),
            Summary.of(len(group) for group in per_scenario),
            bands, role_minutes, int(round(known / max(1, len(scenarios)))),
        ))
    return DayForecast(day, market, stopped_sites, cells,
                       {(c.site, c.service): c for c in cells})


def _notes(history: History, view: View) -> list[str]:
    notes = [f"可比历史日 {len(history.comparable)} 天；"
             f"结算不全剔除 {len(history.excluded)} 天；"
             f"明确停办 {len(history.stopped)} 个站点日。"]
    if history.excluded:
        notes.append("剔除示例：" + "、".join(
            f"{d.isoformat()}({why})" for d, why in history.excluded[:3]) + "。")
    if history.stopped:
        notes.append("停办窗口不进入预测（只剔除停办站点，其他站当天仍可比）："
                     + "、".join(f"{d.isoformat()} {site}"
                                 for d, site in history.stopped) + "。")
    notes.extend("低支撑：" + item for item in history.low_support[:6])
    return notes




# 与容量对照的风险 ------------------------------------------------------------


@dataclass
class BandRisk:
    day: dt.date
    site: str
    band: int
    capacity_minutes: int
    demand: Summary
    short_probability: float
    expected_short: float

    @property
    def label(self) -> str:
        start, end = BANDS[self.band]
        return f"{hhmm(start)}-{hhmm(end)}"


@dataclass
class DaySiteRisk:
    day: dt.date
    site: str
    bands: list[BandRisk]
    joint_probability: float
    note: str = ""

    @property
    def worst_probability(self) -> float:
        return max((b.short_probability for b in self.bands), default=0.0)

    @property
    def tight_bands(self) -> list[str]:
        return [b.label for b in self.bands if b.short_probability > 0]


@dataclass
class BandCapacity:
    """某站某时段可用的持证人员分钟，按人列出，并注明各自持什么证。"""

    minutes: dict[str, int]
    eligible: dict[str, set[str]]

    @property
    def total(self) -> int:
        return sum(self.minutes.values())


def band_risks(forecast: Forecast,
               capacity: dict[dt.date, dict[str, dict[int, BandCapacity]]]
               ) -> list[DaySiteRisk]:
    """``capacity[day][site][band]`` 是该时段可用的人与证。

    同一情景内先把所有时段一起判定，再统计"两个及以上时段同时吃紧"的概率，
    避免把各时段概率直接相乘而丢掉同日同站的相关性。人员分钟按角色走最大流，
    同一名持两证的人不会被算成两个人。
    """
    from .roster import max_flow

    results: list[DaySiteRisk] = []
    for day_forecast in forecast.days:
        day = day_forecast.day
        for site in sorted({cell.site for cell in day_forecast.cells}):
            if site in day_forecast.stopped_sites:
                continue
            per_band: list[tuple[int, BandCapacity, list[dict[str, int]]]] = []
            for band_index, (band_start, band_end) in enumerate(BANDS):
                caps = capacity.get(day, {}).get(site, {}).get(band_index)
                if caps is None:
                    continue
                demands: list[dict[str, int]] = []
                for scenario in forecast.scenarios:
                    demand: dict[str, int] = {}
                    for visit in scenario.visits:
                        if visit.day != day or visit.site != site:
                            continue
                        minutes = spread_minutes(visit.start, visit.window_end,
                                                 visit.duration, band_start, band_end)
                        if minutes <= 0:
                            continue
                        for role in forecast.rules[visit.service].roles:
                            demand[role] = demand.get(role, 0) + minutes
                    demands.append(round_role_demand(demand))
                per_band.append((band_index, caps, demands))
            if not per_band:
                continue
            bands: list[BandRisk] = []
            for band_index, caps, demands in per_band:
                totals, short = [], []
                for demand in demands:
                    total = sum(demand.values())
                    got = max_flow(demand, {w: {"total": m} for w, m in caps.minutes.items()},
                                   caps.eligible) if demand else 0
                    totals.append(total)
                    short.append(max(0.0, total - got))
                bands.append(BandRisk(
                    day, site, band_index, caps.total, Summary.of(totals),
                    sum(1 for x in short if x > 0) / len(short), mean(short)))
            tight = 0
            for index in range(len(per_band)):
                count = 0
                for _, caps, demands in per_band:
                    demand = demands[index]
                    if not demand:
                        continue
                    got = max_flow(demand, {w: {"total": m}
                                            for w, m in caps.minutes.items()},
                                   caps.eligible)
                    if sum(demand.values()) > got:
                        count += 1
                if count >= 2:
                    tight += 1
            results.append(DaySiteRisk(day, site, bands,
                                       tight / len(forecast.scenarios)))
    return results
