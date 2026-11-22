"""班期结束后的回看：把"当初排班时看到的"与"真实履约"分开。

规则：

- 当初可见：只认 ``as_of`` 当天及以前登记的派单、取消、上门与返回记录；
- 真实履约：班期结束后按完整记录判定，用来区分需求预估偏差与调配没落实；
- 工时只从 ``started`` + ``returned`` 两端的完整记录里算，未结班次不补时间；
- ``unassigned``（明确没人接）与 ``canceled``（真实没人申请）分开统计；
- 停办窗口、结算不全的日子与低支撑组合单列，不混进"实际可比较"的样本。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .data import Dataset, Request, View, hhmm
from .forecast import Summary, build_history


@dataclass
class Outcome:
    kind: str  # completed / unassigned / canceled / open / not_visible
    minutes: int | None = None
    started_at: int | None = None
    returned_at: int | None = None
    returned_recorded: dt.date | None = None
    recorded_at: dt.date | None = None

    @property
    def complete(self) -> bool:
        return self.kind == "completed" and self.minutes is not None

    def render(self) -> str:
        if self.kind == "completed":
            return f"完成 {self.minutes} 分钟"
        if self.kind == "unassigned":
            return "明确没人接"
        if self.kind == "canceled":
            return "真实没人申请"
        if self.kind == "not_visible":
            return "排班时不可见"
        return "尚未返回"


@dataclass
class RequestTrace:
    request: str
    day: dt.date
    site: str
    service: str
    earliest: int
    deadline: int
    recorded: dt.date
    visible: Outcome
    actual: Outcome

    @property
    def late_return(self) -> bool:
        """返回时间在排班日之后才登记：当初看不到，不能提前拿来当完成工时。"""
        return (self.actual.returned_recorded is not None
                and self.actual.returned_recorded > self.recorded
                and self.visible.kind != "completed"
                and self.actual.kind == "completed")


def _outcome(events: dict, as_of: dt.date | None) -> Outcome:
    """``as_of`` 有值时是"排班当天看得到的结果"，没有时是班期结束后的最终结果。

    没有登记任何事件时，排班当天只能算"未结"；班期结束后仍无记录，则是没有
    留下可核对的履约凭证，同样不能当作完成工时。
    """
    if not events:
        return Outcome("open" if as_of is not None else "no_record")
    started = events.get("started")
    returned = events.get("returned")
    record = max((e.recorded for e in events.values()), default=None)
    if "unassigned" in events:
        return Outcome("unassigned", recorded_at=events["unassigned"].recorded)
    if "canceled" in events:
        return Outcome("canceled", recorded_at=events["canceled"].recorded)
    if started and returned and started.start_minute is not None \
            and returned.end_minute is not None \
            and returned.end_minute > started.start_minute:
        return Outcome("completed", returned.end_minute - started.start_minute,
                       started.start_minute, returned.end_minute,
                       returned.recorded, record)
    return Outcome("open", started_at=started.start_minute if started else None,
                   recorded_at=record)


@dataclass
class ReviewDay:
    day: dt.date
    market: bool
    stopped_sites: tuple[str, ...]
    traces: list[RequestTrace]
    demand_visible: bool     # 当天派单在排班日是否已全部可见
    hours_complete: bool     # 班期结束后是否都有完整工时
    low_support: list[str] = field(default_factory=list)

    @property
    def comparable(self) -> bool:
        """排班时看得到派单、这些派单后来也结了工时，才可以拿来对照。

        排班日之后才到的派单不参与需求预估，但也不该让整个时段失去可比性：
        只要当初看得见的那部分都有结论，就能比较"当初怎么建议、后来怎么落实"。
        """
        return bool(self.visible_traces) and self.visible_complete \
            and not self.stopped_sites

    @property
    def visible_traces(self) -> list[RequestTrace]:
        return [t for t in self.traces if t.visible.kind != "not_visible"]

    @property
    def late_traces(self) -> list[RequestTrace]:
        return [t for t in self.traces if t.visible.kind == "not_visible"]

    @property
    def visible_complete(self) -> bool:
        return all(t.actual.kind in ("completed", "canceled", "unassigned")
                   for t in self.visible_traces)

    @property
    def exclusion(self) -> str:
        if self.stopped_sites:
            return "明确停办"
        if not self.traces:
            return "排班时看不到派单"
        if not self.visible_traces:
            return "派单全部在排班日之后才登记"
        if not self.visible_complete:
            return "可见班次仍有未结或无履约记录"
        return ""

    @property
    def partial(self) -> bool:
        """排班时只看到一部分派单：结论只覆盖这部分，不能外推整日需求。"""
        return self.comparable and bool(self.late_traces)


@dataclass
class Review:
    round: str
    as_of: dt.date
    days: list[ReviewDay]
    notes: list[str]
    service_minutes: dict[str, Summary]
    low_support: list[str] = field(default_factory=list)
    recommendation: dict[dt.date, set[str]] = field(default_factory=dict)

    def day(self, day: dt.date) -> ReviewDay:
        for item in self.days:
            if item.day == day:
                return item
        raise KeyError(day)

    def outcome_counts(self, actual: bool = True) -> dict[str, int]:
        counts: dict[str, int] = {}
        for day in self.days:
            for trace in day.traces:
                outcome = trace.actual if actual else trace.visible
                counts[outcome.kind] = counts.get(outcome.kind, 0) + 1
        return counts

    def comparable_days(self) -> list[ReviewDay]:
        return [d for d in self.days if d.comparable]

    def low_support_days(self) -> list[ReviewDay]:
        return [d for d in self.days if d.low_support and not d.stopped_sites]

    def low_support_combos(self) -> list[str]:
        return list(self.low_support)

    def stopped_days(self) -> list[ReviewDay]:
        return [d for d in self.days if d.stopped_sites]


def trace_request(view: View, request: Request,
                  actual_view: View | None = None) -> RequestTrace:
    """还原某条派单在 ``view`` 当天看得到的记录，并给出班期结束后的真实履约。"""
    actual_view = actual_view or view.dataset.view(dt.date.max)
    return RequestTrace(
        request.request, request.day, request.site, request.service,
        request.earliest, request.deadline, request.recorded,
        _outcome(view.event_map(request.request), view.as_of),
        _outcome(actual_view.event_map(request.request), None),
    )


def review_round(dataset: Dataset, round_name: str, min_cell: int = 10) -> Review:
    plan = dataset.round(round_name)
    planning_view = dataset.view(plan.as_of)
    actual_view = dataset.view(dt.date.max)
    history = build_history(planning_view,
                            limits=(plan.starts - dt.timedelta(days=200),
                                    plan.starts - dt.timedelta(days=1)),
                            min_cell=min_cell)

    # 班期内的派单：排班日之后才登记的不算当初可见
    visible_requests = {r.request: r for r in planning_view.requests
                        if plan.starts <= r.day <= plan.ends}
    all_requests = [r for r in actual_view.requests
                    if plan.starts <= r.day <= plan.ends]
    late = {r.request: r for r in all_requests if r.request not in visible_requests}

    days: list[ReviewDay] = []
    for day in plan.days():
        stopped = tuple(sorted({c.site for c in planning_view.calendar
                                if c.day == day and c.kind == "stop"}))
        traces: list[RequestTrace] = []
        for request in all_requests:
            if request.day != day or request.site in stopped:
                continue
            events = actual_view.event_map(request.request)
            traces.append(trace_request(
                planning_view, request, actual_view)
                if request.request in visible_requests
                else RequestTrace(
                    request.request, day, request.site, request.service,
                    request.earliest, request.deadline, request.recorded,
                    Outcome("not_visible"), _outcome(events, None)))
        seen_here = [r for r in all_requests
                     if r.day == day and r.site not in stopped]
        demand_visible = bool(seen_here) and all(r.request in visible_requests
                                                 for r in seen_here)
        hours_complete = all(t.actual.complete or t.actual.kind == "canceled"
                             or t.actual.kind == "unassigned"
                             for t in traces)
        days.append(ReviewDay(day, day in planning_view.market_days(), stopped,
                              traces, demand_visible, hours_complete))

    completed = [t for d in days for t in d.traces if t.actual.complete]
    grouped: dict[str, list[int]] = {}
    for trace in completed:
        grouped.setdefault(trace.service, []).append(trace.actual.minutes)
    service_minutes = {service: Summary.of(values)
                       for service, values in grouped.items()}

    notes = [
        f"回看只把 {plan.as_of} 当天及以前登记的事实当作排班依据；"
        "之后才登记的返回时间不提前计入。",
        f"可比历史日 {len(history.comparable)} 天；"
        f"结算不全 {len(history.excluded)} 天；"
        f"停办 {len(history.stopped)} 个站点日。",
    ]
    if late:
        sample = "、".join(sorted(late)[:5])
        notes.append(f"{len(late)} 条班期派单在 {plan.as_of} 之后才登记（{sample} 等），"
                     "只能用于履约核对，不能用于当初的需求预估。")
    late_returns = [t for d in days for t in d.traces if t.late_return]
    if late_returns:
        notes.append(f"{len(late_returns)} 条班次的返回时间在排班日之后才登记，"
                     "当初只能算未结，不能用来证明完成工时。")
    full = [d for d in days if d.comparable and not d.partial]
    partial = [d for d in days if d.partial]
    notes.append("实际可比较的时段（派单全部可见）：" + (
        "、".join(d.day.isoformat() for d in full) or "无") + "。")
    if partial:
        notes.append("只按当初可见部分比较的时段：" + "、".join(
            f"{d.day.isoformat()}(可见 {len(d.visible_traces)} 条，"
            f"另有 {len(d.late_traces)} 条排班后才登记)"
            for d in partial) + "。这些日子的总需求会比当初看到的多，"
            "不能拿它反推需求预估准确。")
    blocked = [d for d in days if not d.comparable and not d.stopped_sites]
    if blocked:
        notes.append("不可直接比较：" + "、".join(
            f"{d.day.isoformat()}({d.exclusion})" for d in blocked) + "。")
    stops = [d for d in days if d.stopped_sites]
    if stops:
        notes.append("已经明确停办的窗口：" + "、".join(
            f"{d.day.isoformat()} {'、'.join(d.stopped_sites)}" for d in stops) + "。")
    # 低支撑只看排班日之前已有完整工时的历史，不把班期内的样本算进来
    low_support = _low_support(planning_view, min_cell, before=plan.starts)
    if low_support:
        notes.append("低支撑的组合（按服务、时段、站点统计历史完整工时，"
                     "样本不足时区间不要当作可靠边界）：")
        notes.extend(f"      {item}" for item in low_support)
    visible_counts = review_outcome_counts(days, False)
    notes.append("排班时可见结果：" + "、".join(
        f"{k} {v}" for k, v in sorted(visible_counts.items())) + "。")
    actual_counts = review_outcome_counts(days, True)
    notes.append("班期结束后的真实履约：" + "、".join(
        f"{k} {v}" for k, v in sorted(actual_counts.items())) + "。")
    unassigned = actual_counts.get("unassigned", 0)
    canceled = actual_counts.get("canceled", 0)
    notes.append(f"明确没人接 {unassigned} 条，与真实没人申请 {canceled} 条分开；"
                 "前者是当时有人却没能接上，后者是需求本身没来。")
    return Review(plan.name, plan.as_of, days, notes, service_minutes, low_support)


def _low_support(view: View, min_cell: int,
                 before: dt.date | None = None) -> list[str]:
    """按服务×站点×开始时段统计：排班时要预测、但历史完整工时不足的组合。

    派单本身要有、完整工时却不够，才是"低支撑"；从没出现过的时段不缺样本，
    只是不需要预测。``before`` 用来只看排班日之前的历史。
    """
    asked: set[tuple[str, str, int]] = {(r.service, r.site, r.earliest)
                                        for r in view.requests}
    complete: dict[tuple[str, str, int], int] = {}
    for request, _ in view.completed_visits():
        if before is not None and request.day >= before:
            continue
        key = (request.service, request.site, request.earliest)
        complete[key] = complete.get(key, 0) + 1
    combos: list[str] = []
    for service, site, slot in sorted(asked):
        done = complete.get((service, site, slot), 0)
        if done < min_cell:
            combos.append(f"{service} {site} {hhmm(slot)}：派单有，"
                          f"完整工时样本仅 {done} 个")
    return combos


def review_outcome_counts(days: list[ReviewDay], actual: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    for day in days:
        for trace in day.traces:
            outcome = trace.actual if actual else trace.visible
            counts[outcome.kind] = counts.get(outcome.kind, 0) + 1
    return counts


# 建议兑现 --------------------------------------------------------------------


def recommended_plan(dataset: Dataset, round_name: str) -> dict[dt.date, set[str]]:
    """用排班日当天的资料重跑一遍排班，还原"当初建议排上"的班次。

    只使用 ``as_of`` 可见的派单、可用时段与证书；返回每个排班日建议落位的
    请求集合。资料不足（例如早期班期没有在岗表）时返回空集，兑现对照也就
    只能说明"没有建议记录"。
    """
    from .roster import Visit, make_context, solve

    plan = dataset.round(round_name)
    view = dataset.view(plan.as_of)
    result: dict[dt.date, set[str]] = {}
    for day in plan.days():
        if any(c.day == day and c.kind == "stop" for c in view.calendar):
            continue
        visits = [Visit(r.request, r.site, r.service, r.earliest, r.deadline,
                        _typical_duration(view, r.service), request=r.request)
                  for r in view.requests
                  if r.day == day
                  and "started" not in view.event_map(r.request)
                  and "canceled" not in view.event_map(r.request)]
        if not visits:
            continue
        context = make_context(view, dataset, day, visits)
        if not context.workers:
            continue
        solved = solve(context, max_nodes=40_000)
        result[day] = solved.served_keys
    return result


def _typical_duration(view: View, service: str) -> int:
    """排班时还没有当天工时，用加权平均先占位；这里只用整点小时数即可。"""
    values = [minutes for request, minutes in view.completed_visits()
              if request.service == service]
    if not values:
        return {"bath": 75, "escort": 120, "clean": 55}.get(service, 60)
    return int(sum(values) / len(values))


@dataclass
class FulfilmentRow:
    day: dt.date
    site: str
    requested: int
    completed: int
    unassigned: int
    canceled: int
    open: int
    shortage: int
    estimate_share: float
    execution_share: float
    note: str = ""


def compare_fulfilment(review: Review,
                       recommended: dict[dt.date, set[str]]
                       ) -> list[FulfilmentRow]:
    """把"当初建议排上"与"真实履约"分开。

    只在结论确定的站点日上归因：

    - 当初没建议、实际明确没完成：需求预估偏低（estimate）；
    - 当初建议、实际没完成且无正式取消：调配没落实（execution）；
    - 已经取消的申请不算调配失误，也不算完成工时；
    - 还没有结论的日子（未结、无履约记录）不归因，单列在 note 里。
    """
    rows: list[FulfilmentRow] = []
    for day in review.days:
        for site in sorted({t.site for t in day.traces}):
            traces = [t for t in day.traces if t.site == site]
            visible = [t for t in traces if t.visible.kind != "not_visible"]
            unresolved = [t for t in visible
                          if t.actual.kind not in ("completed", "unassigned", "canceled")]
            if not visible:
                continue
            if day.day not in recommended:
                rows.append(FulfilmentRow(
                    day.day, site, len(visible),
                    sum(1 for t in visible if t.actual.complete),
                    sum(1 for t in visible if t.actual.kind == "unassigned"),
                    sum(1 for t in visible if t.actual.kind == "canceled"),
                    sum(1 for t in visible if t.actual.kind not in
                        ("completed", "unassigned", "canceled")),
                    0, 0.0, 0.0,
                    note="排班日没有可用的在岗资料，无法还原当初建议"))
                continue
            if unresolved:
                rows.append(FulfilmentRow(
                    day.day, site, len(traces),
                    sum(1 for t in visible if t.actual.complete),
                    sum(1 for t in visible if t.actual.kind == "unassigned"),
                    sum(1 for t in visible if t.actual.kind == "canceled"),
                    len(unresolved), 0, 0.0, 0.0,
                    note=f"{len(unresolved)} 条尚无履约结论，暂不归因"))
                continue
            suggested = {t.request for t in visible
                         if t.request in recommended.get(day.day, set())}
            done = {t.request for t in visible if t.actual.complete}
            canceled = {t.request for t in visible if t.actual.kind == "canceled"}
            # 正式取消的申请不是"缺口"，也不能拿去证明完成工时
            missing = {t.request for t in visible
                       if not t.actual.complete and t.request not in canceled}
            estimate = missing - suggested
            execution = missing & suggested
            total = len(missing)
            rows.append(FulfilmentRow(
                day.day, site, len(visible), len(done),
                sum(1 for t in visible if t.actual.kind == "unassigned"),
                len(canceled), 0, total,
                round(len(estimate) / total, 3) if total else 0.0,
                round(len(execution) / total, 3) if total else 0.0,
                _fulfilment_note(total, len(estimate), len(execution), len(canceled)),
            ))
    return rows


def _fulfilment_note(missing: int, estimate: int, execution: int,
                     canceled: int) -> str:
    if not missing:
        return "全部完成"
    parts = []
    if estimate:
        parts.append(f"{estimate} 条当初建议没有覆盖（需求低估或排班余地不足）")
    if execution:
        parts.append(f"{execution} 条建议排了却没落实到岗")
    if canceled:
        parts.append(f"{canceled} 条已正式取消")
    return "；".join(parts)
