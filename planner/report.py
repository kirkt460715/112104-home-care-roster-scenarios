"""把结果整理成给站长看的文字/表格，并提供命令行入口。"""
from __future__ import annotations

import argparse
import datetime as dt

from . import backtest, decide, dispatch, forecast, review, roster
from .data import Dataset, hhmm

DEFAULT_ROUND = "R4"

_CACHE: dict[tuple[str, int, int], "forecast.Forecast"] = {}


def cached_forecast(dataset: Dataset, round_name: str, scenarios: int):
    """同一进程里同一套情景只算一次：各节共用同一组未来工时情形。"""
    key = (round_name, scenarios, id(dataset))
    if key not in _CACHE:
        _CACHE[key] = forecast.forecast_round(dataset, round_name,
                                              scenarios=scenarios)
    return _CACHE[key]


def _table(rows: list[dict], columns: list[str]) -> str:
    widths = {key: max(len(key), *(len(str(row.get(key, ""))) for row in rows))
              for key in columns}
    header = " | ".join(key.ljust(widths[key]) for key in columns)
    rule = "-+-".join("-" * widths[key] for key in columns)
    lines = [header, rule]
    for row in rows:
        lines.append(" | ".join(str(row.get(key, "")).ljust(widths[key])
                                for key in columns))
    return "\n".join(lines)


# 预测与风险 ----------------------------------------------------------------


def workload_section(dataset: Dataset, round_name: str, scenarios: int) -> list[str]:
    result = cached_forecast(dataset, round_name, scenarios)
    lines = [f"## 工时分布（班期 {result.round.name}，"
             f"排班日 {result.as_of}，{len(result.scenarios)} 组情景）", ""]
    lines.extend(f"- {note}" for note in result.notes)
    lines.append("")
    rows = []
    for day in result.days:
        for cell in day.cells:
            rows.append({
                "日期": day.day.isoformat(),
                "站点": cell.site,
                "服务": cell.service,
                "已知": cell.known_visits,
                "班次数": f"{cell.visits.p50:.0f}",
                "工时 P10": f"{cell.minutes.p10:.0f}",
                "工时 P50": f"{cell.minutes.p50:.0f}",
                "工时 P90": f"{cell.minutes.p90:.0f}",
                "按证拆分(P50)": " / ".join(
                    f"{role} {summary.p50:.0f}"
                    for role, summary in sorted(cell.role_minutes.items())),
            })
    lines.append(_table(rows, ["日期", "站点", "服务", "已知", "班次数",
                               "工时 P10", "工时 P50", "工时 P90", "按证拆分(P50)"]))
    lines.append("")
    if result.days and any(d.stopped_sites for d in result.days):
        for day in result.days:
            if day.stopped_sites:
                lines.append(f"- {day.day} 明确停办：{'、'.join(day.stopped_sites)}，"
                             "不安排需求也不进入预测。")
        lines.append("")
    return lines


def risk_section(dataset: Dataset, round_name: str, scenarios: int) -> list[str]:
    result = cached_forecast(dataset, round_name, scenarios)
    view = dataset.view(result.as_of)
    capacity = {day: decide.capacity_for(view, dataset, day) for day in result.round.days()}
    risks = forecast.band_risks(result, capacity)
    lines = [f"## 工时风险（按站按 90 分钟时段）", ""]
    rows = []
    for item in risks:
        for band in item.bands:
            rows.append({
                "日期": item.day.isoformat(),
                "站点": item.site,
                "时段": band.label,
                "需求均值": f"{band.demand.mean:.0f}",
                "需求 P90": f"{band.demand.p90:.0f}",
                "可用分钟": band.capacity_minutes,
                "超载概率": f"{band.short_probability:.0%}",
                "平均缺口": f"{band.expected_short:.0f}",
            })
    lines.append(_table(rows, ["日期", "站点", "时段", "需求均值", "需求 P90",
                               "可用分钟", "超载概率", "平均缺口"]))
    lines.append("")
    joint = [item for item in risks if item.joint_probability >= 0.10]
    if joint:
        lines.append("同日同站多时段一起超载（联合概率，同一情景内判定，不能拿单时段概率相乘）：")
        for item in joint:
            lines.append(f"- {item.day} {item.site}：联合 {item.joint_probability:.0%}，"
                         f"吃紧时段 {'、'.join(item.tight_bands)}")
    else:
        lines.append("没有出现同日同站多时段一起超载概率 ≥10% 的情形。")
    lines.append("")
    return lines


def load_section(dataset: Dataset, round_name: str, scenarios: int,
                 day: dt.date) -> list[str]:
    """按名字排班，暴露"同一持证人被两组同时算进人手"。"""
    result = cached_forecast(dataset, round_name, scenarios)
    view = dataset.view(result.as_of)
    scenario = result.scenarios[0]
    visits = [roster.Visit(v.request or f"{v.site}{v.service}{i}", v.site, v.service,
                           v.start, v.window_end, v.duration)
              for i, v in enumerate(scenario.visits) if v.day == day]
    context = roster.make_context(view, dataset, day, visits)
    rows = []
    for load in roster.band_loads(context):
        if load.naive_slots == 0 and load.total_demand == 0:
            continue
        rows.append({
            "站点": load.site,
            "时段": load.label,
            "表上人头": load.naive_slots,
            "真正可填": load.filled_slots,
            "重复计数": load.duplicated_people,
            "角色分钟": load.total_demand,
            "缺口分钟": load.short_minutes,
        })
    lines = [f"## {day} 分时段人手核对", ""]
    lines.append(_table(rows, ["站点", "时段", "表上人头", "真正可填",
                               "重复计数", "角色分钟", "缺口分钟"]))
    lines.append("")
    lines.append("「表上人头」是按名字在表上数出的角色数；「真正可填」要求该人当天在站、"
                 "持对应证、且同一人不能同时顶两个角色。差额就是同一名持证人被两组重复算入。")
    lines.append("")
    return lines


# 承诺与备班 ----------------------------------------------------------------


def commitment_section(dataset: Dataset, round_name: str) -> list[str]:
    """家庭已确认的连续照护时段优先锁定；锁不住的要写清必须通知家庭的缺口。"""
    result = cached_forecast(dataset, round_name, 1)
    view = dataset.view(result.as_of)
    rows = []
    kept = 0
    for day in result.round.days():
        committed = roster.committed_visits(view, day)
        if not committed:
            continue
        solved = roster.solve(roster.make_context(view, dataset, day, []))
        served = solved.served_keys
        for visit in committed:
            if visit.key in served:
                kept += 1
                continue
            gap = next((g for g in solved.gaps if g.visit.key == visit.key), None)
            rows.append({
                "日期": day.isoformat(),
                "请求": visit.key,
                "站": visit.site,
                "原约时段": f"{hhmm(visit.fixed_start)}-"
                            f"{hhmm(visit.fixed_start + visit.duration)}",
                "原约人员": "、".join(visit.fixed_workers),
                "原因": gap.reason if gap else "未排定",
                "必须通知家庭的缺口": gap.detail if gap else "需人工核对",
                "处理": "正式改约后才能释放原时段",
            })
    lines = ["## 家庭已确认时段（优先锁定）", ""]
    lines.append(f"已按原约锁定的班次 {kept} 段；需要通知家庭的缺口 {len(rows)} 段。")
    lines.append("")
    if rows:
        lines.append(_table(rows, ["日期", "请求", "站", "原约时段", "原约人员",
                                   "原因", "必须通知家庭的缺口", "处理"]))
        lines.append("")
        lines.append("这些时段不会因为当班排不下就自动让给别的需求："
                     "要释放，必须先和家庭正式改约。")
        lines.append("")
    return lines


def default_options(dataset: Dataset, result: forecast.Forecast) -> list[decide.Option]:
    """按资料里的上限与准入条件，生成一组真正值得比较的备班方案。"""
    view = dataset.view(result.as_of)
    policy = decide.policy_for(dataset, result.round.name)
    limit = policy.max_loan_days if policy else 4
    days = [d for d in result.round.days() if not result.day(d).stopped_sites]
    options = [decide.Option("现状")]
    for course in sorted(view.courses.values(), key=lambda c: c.course):
        for worker in course.eligible:
            options.append(decide.Option(
                f"报 {course.course}（{course.credential} 证，{worker}）",
                trainings=frozenset({(course.course, worker)})))
    for capacity_id in sorted({a.capacity_id for a in view.availability if a.kind == "loan"}):
        usable = [d for d in days
                  if any(a.capacity_id == capacity_id and a.covers(d)
                         for a in view.availability)][:limit]
        for site in ("N", "S", "W"):
            if not any(a.capacity_id == capacity_id and a.site == site
                       for a in view.availability):
                continue
            if not usable:
                continue
            options.append(decide.Option(
                f"预留 {capacity_id} 到 {site}（{usable[0].isoformat()} 起 {len(usable)} 天）",
                activations=frozenset((capacity_id, d, site) for d in usable)))
    return options


def options_section(dataset: Dataset, round_name: str, scenarios: int) -> list[str]:
    result = cached_forecast(dataset, round_name, scenarios)
    options = default_options(dataset, result)
    results = decide.compare_options(dataset, result, options)
    lines = ["## 备班方案比较（同一组未来工时情景、同一批共享人员）", ""]
    lines.append(_table([item.as_row() for item in results],
                        ["方案", "内容", "满足班段(均值)", "满足班段(P10-P90)",
                         "家庭缺口(均值)", "主要缺口", "费用"]))
    lines.append("")
    lines.append("每个方案都用同一组工时情景、同一批共享人员和不可随意移动的预约评价；"
                 "「满足班段」是整期能排上的班次数，「家庭缺口」是必须通知家庭的段数。")
    lines.append("资料里没有补贴台账，费用只核对 `planning_policy` 的额度上限；"
                 "培训名额与报名条件按 `courses` 的 seats 和 eligible 核对。")
    lines.append("")
    blocked = [item for item in results if not item.admission.ok]
    if blocked:
        lines.append("准入不过、不能进入比较的备选：")
        for item in blocked:
            lines.append(f"- {item.option.name}："
                         + "；".join(item.admission.blockers))
        lines.append("")
    grading = [item for item in results if item.admission.ok and item.admission.consequences]
    for item in grading:
        lines.append(f"- {item.option.name} 的代价：" + "；".join(item.admission.consequences))
    if grading:
        lines.append("")
    view = dataset.view(result.as_of)
    lines.append("现在非定不可的动作（按最晚决定日排序）：")
    actions = []
    for option in options[1:]:
        for decision in decide.decisions_for(view, option):
            if decision.kind == "defer":
                continue
            actions.append(decision)
    actions.sort(key=lambda d: (d.must_decide_by is None, d.must_decide_by,
                                d.subject))
    loans: dict[str, list[decide.Decision]] = {}
    for decision in actions:
        if decision.kind == "reserve":
            loans.setdefault(decision.subject.split("→")[0], []).append(decision)
    for decision in actions:
        if decision.kind == "reserve":
            continue
        when = (f"，最晚 {decision.must_decide_by}"
                if decision.must_decide_by else "")
        lines.append(f"- [培训] {decision.subject}：{decision.detail}{when}")
    for capacity_id, group in sorted(loans.items()):
        when = (f"，最晚 {group[0].must_decide_by}"
                if group[0].must_decide_by else "")
        options_text = " 或 ".join(d.subject for d in group)
        lines.append(f"- [外援] 在 {options_text} 之间二选一：{group[0].detail}"
                     f"{when}")
    lines.append("- 具体补哪个班：等该日需求明朗再定；现在只锁人与外援的可用日期。")
    lines.append("")
    lines.append("届时还可以换的安排：")
    lines.append("- 借班落到哪天、哪个站，可在确认当天按需求改；同一时段只能给一个站。")
    lines.append("- 培训开课后不能撤回，学员脱岗计入；证书生效前不能用于上门。")
    lines.append("- 本周才开课的证书不能用来承诺本周的家庭时段。")
    lines.append("")
    return lines


def dispatch_section(dataset: Dataset, round_name: str, scenarios: int,
                     as_now: dt.date | None = None) -> list[str]:
    """先比方案，再把可以发给站点的调配逐条列出来；变化后按真实时段复核。"""
    result = cached_forecast(dataset, round_name, scenarios)
    options = default_options(dataset, result)
    evaluated = decide.compare_options(dataset, result, options)
    usable = [item for item in evaluated if item.admission.ok]
    if not usable:
        return ["## 调配清单", "", "没有通过准入核对的方案。", ""]
    best = max(usable, key=lambda item: (item.satisfied_mean, -item.cost))
    view = dataset.view(result.as_of)
    items = dispatch.build_dispatches(view, best.option)
    lines = ["## 调配清单（先比方案，再逐条发站点）", ""]
    lines.append(f"选中的方案：{best.option.name} —— {best.option.describe()}")
    lines.append(f"满足班段均值 {best.satisfied_mean:.0f}"
                 f"（P10-P90 {best.satisfied_p10:.0f}-{best.satisfied_p90:.0f}），"
                 f"必须通知家庭的缺口 {best.family_gap_mean:.1f} 段/期。")
    lines.append("")
    lines.append(_table([{"调配": item.render()} for item in items], ["调配"]))
    lines.append("")
    stale = dispatch.recheck(dataset, as_now or result.as_of, items, best.option)
    changed = [item for item in stale if item.status != "ready"]
    if changed:
        lines.append("复核后仍然能发的部分：")
        for item in dispatch.sendable(stale):
            lines.append(f"- {item.render()}")
        lines.append("")
        lines.append("受影响的真实时段（只重查这些，不整包重发）：")
        for spot in dispatch.affected_bands(items, changed):
            lines.append(f"- {spot}")
        lines.append("")
        lines.append("需要撤回、锁定或改期的条目：")
        for item in changed:
            lines.append(f"- {item.render()}")
        lines.append("")
    else:
        lines.append("按当前资料复核，清单内条目全部仍可发。")
        lines.append("")
    return lines


# 回看 ------------------------------------------------------------------


def review_section(dataset: Dataset, round_name: str) -> list[str]:
    item = review.review_round(dataset, round_name)
    lines = [f"## 回看（{item.round}，排班日 {item.as_of}）", ""]
    lines.extend(f"- {note}" for note in item.notes)
    lines.append("")
    rows = []
    for day in item.days:
        for trace in day.traces:
            rows.append({
                "日期": day.day.isoformat(),
                "请求": trace.request,
                "站": trace.site,
                "服务": trace.service,
                "排班时可见": trace.visible.render(),
                "真实履约": trace.actual.render()
                + ("（返回时间晚于排班日）" if trace.late_return else ""),
            })
    lines.append(_table(rows, ["日期", "请求", "站", "服务", "排班时可见", "真实履约"]))
    lines.append("")
    recommended = review.recommended_plan(dataset, round_name)
    fulfilment = review.compare_fulfilment(item, recommended)
    no_basis = [row for row in fulfilment
                if "无法还原当初建议" in row.note]
    fulfilment = [row for row in fulfilment if row not in no_basis]
    if no_basis:
        lines.append(f"在岗与可用资料只覆盖部分班期：{len(no_basis)} 个站点日"
                     "无法还原当初的建议，只核对真实履约，不做建议归因。")
        lines.append("")
    if fulfilment:
        lines.append("建议兑现对照（当初建议 vs 真实履约）：")
        lines.append("")
        lines.append(_table([{
            "日期": row.day.isoformat(),
            "站点": row.site,
            "可见": row.requested,
            "完成": row.completed,
            "没人接": row.unassigned,
            "取消": row.canceled,
            "未结": row.open,
            "缺口": row.shortage,
            "预估偏差": f"{row.estimate_share:.0%}" if row.shortage else "-",
            "调配未落实": f"{row.execution_share:.0%}" if row.shortage else "-",
            "说明": row.note,
        } for row in fulfilment],
            ["日期", "站点", "可见", "完成", "没人接", "取消", "未结", "缺口",
             "预估偏差", "调配未落实", "说明"]))
        lines.append("")
        lines.append("缺口按当初可见的派单归因：当初建议没覆盖的是需求预估偏差，"
                     "建议排了却没到岗的是调配没落实；还没有结论的时段不参与归因。")
        lines.append("")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="工时预测与短期资质缺口排班")
    parser.add_argument("--as-of", type=dt.date.fromisoformat,
                        help="站在哪一天重算（默认用班期表里的 as_of）")
    parser.add_argument("--scenarios", type=int, default=200,
                        help="模拟情景数")
    parser.add_argument("--section", default="all",
                        choices=["all", "workload", "risk", "load", "commit",
                                 "options", "review", "history", "coverage",
                                 "dispatch"],
                        help="只输出某一节")
    parser.add_argument("--day", type=dt.date.fromisoformat,
                        help="load 小节要看哪一天")
    options = parser.parse_args(argv)

    dataset = Dataset.load()
    lines: list[str] = []
    if options.section in ("all", "history"):
        item = cached_forecast(dataset, DEFAULT_ROUND, options.scenarios)
        lines.append("## 历史资料可用性")
        lines.append("")
        lines.extend(f"- {note}" for note in item.notes)
        lines.append("")
    if options.section in ("all", "workload"):
        lines.extend(workload_section(dataset, DEFAULT_ROUND, options.scenarios))
    if options.section in ("all", "risk"):
        lines.extend(risk_section(dataset, DEFAULT_ROUND, options.scenarios))
    if options.section in ("all", "load"):
        lines.extend(load_section(dataset, DEFAULT_ROUND, options.scenarios,
                                  options.day or dt.date(2026, 10, 9)))
    if options.section in ("all", "commit"):
        lines.extend(commitment_section(dataset, DEFAULT_ROUND))
    if options.section in ("all", "options"):
        lines.extend(options_section(dataset, DEFAULT_ROUND, options.scenarios))
    if options.section in ("all", "coverage"):
        for round_name in ("R1", "R2", "R3"):
            item = backtest.backtest_round(dataset, round_name,
                                           scenarios=max(40, options.scenarios // 2))
            lines.extend(["", f"## 区间覆盖检验（{round_name}）", ""])
            lines.extend(f"- {note}" for note in item.notes)
            if item.points:
                rows = [{
                    "日期": p.day.isoformat(),
                    "站点": p.site,
                    "实际分钟": p.actual,
                    "P10": round(p.p10),
                    "P50": round(p.p50),
                    "P90": round(p.p90),
                    "是否覆盖": "是" if p.covered else ("高于 P90" if p.below else "低于 P10"),
                } for p in item.points[:12]]
                lines.append("")
                lines.append(_table(rows, ["日期", "站点", "实际分钟", "P10", "P50",
                                           "P90", "是否覆盖"]))
                if len(item.points) > 12:
                    lines.append(f"（仅列前 12 个站点日，共 {len(item.points)} 个。）")
            lines.append("")
    if options.section in ("all", "dispatch"):
        lines.extend(dispatch_section(dataset, DEFAULT_ROUND, options.scenarios,
                                      options.as_of))
    if options.section in ("all", "review"):
        for round_name in ("R1", "R2", "R3", "R4"):
            lines.extend(review_section(dataset, round_name))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
