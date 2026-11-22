"""按人、按资质核对的班表可行性。

这里刻意不把"持证"当成"人手"：每个班次都要把姓名逐个落位，双人服务要求
两个不同的人同时在场，跨站还要留出通勤分钟。任何未结的班次不会被补一个返回
时间，也不会因为某人同时持有两个证就把他算成两个人。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .data import Agreement, Dataset, Rule, View, hhmm

# 时段按 90 分钟切分，覆盖全部可能的服务窗口（08:00-16:00），
# 用来观察"多个时段一起吃紧"
BANDS = ((480, 570), (570, 660), (660, 750), (750, 840), (840, 930), (930, 960))


def overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def spread_minutes(earliest: int, latest_start: int, duration: int,
                   band_start: int, band_end: int) -> float:
    """一个班次落在某时段内的期望分钟数。

    派单给的是可办理窗口，不是固定开始时刻；规划阶段不知道当天几点上门，
    就按窗口内均匀开始把工时摊开。窗口越窄，摊布越接近确定值；窗口只有
    一个点时就是原来的时长交叠。这样 08:00 不会因为"最早开始"被人为挤满。
    """
    if latest_start <= earliest:
        # 窗口退化成一个点：就是确定的开始时刻，直接用时长交叠
        return float(overlap(earliest, earliest + duration, band_start, band_end))
    span_start = earliest
    span_end = latest_start + duration
    share = overlap(span_start, span_end, band_start, band_end)
    if share <= 0:
        return 0.0
    return duration * share / ((latest_start - earliest) + duration)


@dataclass(frozen=True)
class Visit:
    key: str
    site: str
    service: str
    earliest: int
    latest_start: int
    duration: int
    committed: bool = False
    family_confirmed: bool = False
    fixed_workers: tuple[str, ...] = ()
    fixed_start: int | None = None
    request: str | None = None

    @property
    def latest_end(self) -> int:
        return self.latest_start + self.duration


@dataclass
class WorkerDay:
    worker: str
    windows: dict[str, tuple[int, int]] = field(default_factory=dict)
    credentials: set[str] = field(default_factory=set)
    firm_credentials: set[str] = field(default_factory=set)  # 已有证书，不含培训换来的
    absences: list[tuple[int, int]] = field(default_factory=list)
    bookings: list[tuple[str, int, int]] = field(default_factory=list)

    def window(self, site: str) -> tuple[int, int] | None:
        return self.windows.get(site)


@dataclass(frozen=True)
class Assignment:
    visit: Visit
    workers: tuple[str, ...]
    start: int
    end: int


@dataclass(frozen=True)
class Gap:
    visit: Visit
    reason: str
    detail: str = ""

    @property
    def needs_family_notice(self) -> bool:
        return self.visit.family_confirmed


@dataclass
class RosterResult:
    day: dt.date
    assignments: list[Assignment]
    gaps: list[Gap]
    rebooking: list[Gap]

    @property
    def served_keys(self) -> set[str]:
        return {a.visit.key for a in self.assignments}

    @property
    def served_minutes(self) -> int:
        return sum(a.end - a.start for a in self.assignments)

    def worker_minutes(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for assignment in self.assignments:
            for worker in assignment.workers:
                totals[worker] = totals.get(worker, 0) + assignment.end - assignment.start
        return totals


@dataclass
class RosterContext:
    day: dt.date
    workers: dict[str, WorkerDay]
    rules: dict[str, Rule]
    transport: dict[tuple[str, str], int]
    visits: list[Visit]

    def travel(self, origin: str, destination: str) -> int:
        if origin == destination:
            return 0
        return self.transport.get((origin, destination),
                                  self.transport.get((destination, origin), 0))

    def eligible(self, worker: str, credential: str) -> bool:
        return credential in self.workers[worker].credentials

    def firm_eligible(self, worker: str, credential: str) -> bool:
        return credential in self.workers[worker].firm_credentials

    def present_workers(self, site: str) -> list[str]:
        return [w for w, state in self.workers.items()
                if state.window(site) is not None and w in self.workers]

    def role_options(self, visit: Visit, firm: bool = False) -> list[tuple[str, ...]]:
        """按服务规则给出当天在该站真正可组成的持证组合。

        ``firm`` 为真时只看已经生效的证书，用于家庭已确认的班次。
        """
        check = self.firm_eligible if firm else self.eligible
        roles = self.rules[visit.service].roles
        present = self.present_workers(visit.site)
        if len(roles) == 1:
            return [(w,) for w in present if check(w, roles[0])]
        options = []
        for first in present:
            for second in present:
                if first == second:
                    continue
                if (check(first, roles[0]) and check(second, roles[1])) or \
                        (check(first, roles[1]) and check(second, roles[0])):
                    options.append((first, second))
        return options

    def worker_free(self, worker: str, site: str, start: int, end: int) -> bool:
        state = self.workers.get(worker)
        if state is None:
            return False
        window = state.window(site)
        if window is None or not (window[0] <= start and end <= window[1]):
            return False
        for absent_start, absent_end in state.absences:
            if overlap(start, end, absent_start, absent_end):
                return False
        for booked_site, booked_start, booked_end in state.bookings:
            gap = self.travel(booked_site, site)
            if not (end + gap <= booked_start or booked_end + gap <= start):
                return False
        return True

    def candidate_starts(self, visit: Visit, workers: Sequence[str]) -> list[int]:
        starts = {visit.earliest, visit.latest_start}
        for worker in workers:
            state = self.workers[worker]
            window = state.window(visit.site)
            if window:
                starts.add(window[0])
            for booked_site, _, booked_end in state.bookings:
                gap = self.travel(booked_site, visit.site)
                starts.add(booked_end + gap)
                starts.add(booked_end + gap - visit.duration)
            for absent_start, absent_end in state.absences:
                starts.add(absent_end)
                starts.add(absent_end - visit.duration)
        valid = []
        for start in sorted(starts):
            if not (visit.earliest <= start <= visit.latest_start):
                continue
            if all(self.worker_free(w, visit.site, start, start + visit.duration)
                   for w in workers):
                valid.append(start)
        return valid

    def book(self, visit: Visit, workers: Sequence[str], start: int) -> Assignment:
        end = start + visit.duration
        for worker in workers:
            self.workers[worker].bookings.append((visit.site, start, end))
        return Assignment(visit, tuple(workers), start, end)

    def unbook(self, workers: Sequence[str]) -> None:
        for worker in workers:
            self.workers[worker].bookings.pop()


# 可见事实 → 当天的可行性问题 ------------------------------------------------


def course_enrollments(view: View,
                       extra: Iterable[tuple[str, str]] = ()) -> dict[tuple[str, str], object]:
    """当天仍然算数的培训报名。

    开课之后才撤销的培训仍然算脱岗，课程不会因为协议 later 变成 cancelled 就
    把人放回岗位；开课前就撤销的报名才真正释放。
    """
    records: dict[tuple[str, str], list[Agreement]] = {}
    for agreement in view.dataset.agreements:
        if agreement.kind != "course" or agreement.recorded > view.as_of:
            continue
        records.setdefault((agreement.subject, agreement.worker), []).append(agreement)
    result: dict[tuple[str, str], object] = {}
    for (course_id, worker), rows in records.items():
        course = view.courses.get(course_id)
        if course is None:
            continue
        cancelled_before_start = rows and all(
            row.state == "cancelled" and row.recorded <= course.starts for row in rows)
        if cancelled_before_start:
            continue
        result[(course_id, worker)] = course
    for course_id, worker in extra:
        course = view.courses.get(course_id)
        if course is not None:
            result[(course_id, worker)] = course
    return result


def credentials_on(view: View, worker: str, day: dt.date,
                   actual_certs: bool = False,
                   training: Iterable[tuple[str, str]] = ()) -> set[str]:
    """某人当天可直接上门服务的资格。

    基础资格按 ``qualifications`` 的有效区间判断（``to`` 不含）；培训换来的资格
    必须等证书真正生效：规划时按课程的最早发证日，回看时按实际发证记录。
    本周未生效的证书不会提前出现。
    """
    credentials = {q.credential for q in view.qualifications
                   if q.worker == worker and q.valid(day)}
    for (course_id, enrolled), course in course_enrollments(view, training).items():
        if enrolled != worker:
            continue
        if actual_certs:
            grants = [a.day for a in view.dataset.adjustments
                      if a.reference == course_id and a.worker == worker
                      and a.status == "cert_granted"]
            granted = min(grants) if grants else None
        else:
            granted = course.earliest_cert
        if granted is not None and granted <= day:
            credentials.add(course.credential)
    return credentials


def loan_sites_on(view: View, day: dt.date) -> dict[str, str]:
    """已确认的借班在当天占用哪个站点；同一借班时段只落一站。"""
    result: dict[str, str] = {}
    for agreement in view.active_agreements("loan"):
        if agreement.starts != day:
            continue
        current = result.get(agreement.subject)
        if current is None:
            result[agreement.subject] = agreement.site
        elif current != agreement.site:
            raise ValueError(
                f"{agreement.subject} 在 {day} 同时登记到 {current} 与 {agreement.site}；"
                "同一借班时段只能落一站")
    return result


def committed_visits(view: View, day: dt.date) -> list[Visit]:
    grouped: dict[str, list[Agreement]] = {}
    for agreement in view.active_agreements("visit"):
        if agreement.starts == day:
            grouped.setdefault(agreement.subject, []).append(agreement)
    visits = []
    for subject, rows in grouped.items():
        request = next((r for r in view.requests if r.request == subject), None)
        if request is None:
            continue
        start = min(row.start_minute for row in rows)
        end = max(row.end_minute for row in rows)
        visits.append(Visit(
            key=subject, site=request.site, service=request.service,
            earliest=start, latest_start=start, duration=end - start,
            committed=True, family_confirmed=True,
            fixed_workers=tuple(sorted({row.worker for row in rows})),
            fixed_start=start, request=subject,
        ))
    return visits


def make_context(view: View, dataset: Dataset, day: dt.date,
                 visits: Iterable[Visit] = (),
                 activations: Iterable[tuple[str, dt.date, str]] = (),
                 actual_certs: bool = False,
                 reserve_commitments: bool = True,
                 extra_training: Iterable[tuple[str, str]] = ()) -> RosterContext:
    """把当天可见的人、证、预约与需求装进一个可行性问题。

    ``activations`` 是计划启用中的借班（capacity_id, 日期, 站点）。同一借班时段
    只能落一站，且只在被启用的日期出现。``extra_training`` 用来试算"如果现在
    报这一期培训"，其余口径与真实资料一致。家庭已确认的班次由 :func:`solve`
    先落位，这里只把它们列进需求。
    """
    activated: dict[tuple[str, dt.date], str] = {}
    for capacity_id, when, site in activations:
        if when != day:
            continue
        current = activated.get((capacity_id, when))
        if current is not None and current != site:
            raise ValueError(f"{capacity_id} 在 {when} 只能落一个站点，收到 {current}/{site}")
        activated[(capacity_id, when)] = site
    confirmed = loan_sites_on(view, day)
    # 已确认的借班不能被方案悄悄改站：冲突的预留当天按原约落站，改派要正式撤销
    for capacity_id, site in confirmed.items():
        if activated.get((capacity_id, day), site) != site:
            activated[(capacity_id, day)] = site

    workers: dict[str, WorkerDay] = {}
    for entry in view.availability:
        if not entry.covers(day):
            continue
        if entry.kind == "loan":
            allowed = activated.get((entry.capacity_id, day), confirmed.get(entry.capacity_id))
            if allowed != entry.site:
                continue
        state = workers.setdefault(entry.worker, WorkerDay(entry.worker))
        current = state.windows.get(entry.site)
        window = (entry.start_minute, entry.end_minute)
        state.windows[entry.site] = window if current is None else (
            min(current[0], window[0]), max(current[1], window[1]))
    extra = tuple(extra_training)
    for worker in workers:
        workers[worker].credentials = credentials_on(view, worker, day, actual_certs,
                                                     training=extra)
        # 家庭已确认的班次只能靠已经生效的证书，不能拿下周才下来的证先承诺
        workers[worker].firm_credentials = {
            q.credential for q in view.qualifications if q.worker == worker and q.valid(day)}
    # 培训脱岗：学员在课程时段不能上门；已开始的培训不会因后来退班而恢复
    for (course_id, worker), course in course_enrollments(view, extra).items():
        if not course.day_span(day):
            continue
        state = workers.setdefault(worker, WorkerDay(worker))
        state.absences.append((course.start_minute, course.end_minute))
        if actual_certs:
            state.credentials = credentials_on(view, worker, day, actual_certs,
                                               training=extra)

    demand = list(visits)
    if reserve_commitments:
        committed = committed_visits(view, day)
        # 已与家庭确认的请求不再作为弹性需求重复出现
        reserved = {visit.key for visit in committed}
        demand = [visit for visit in demand if visit.key not in reserved] + committed
    return RosterContext(day, workers, dataset.rules, dataset.transport, demand)


# 求解 ------------------------------------------------------------------------


def _gap_reason(context: RosterContext, visit: Visit) -> Gap:
    roles = context.rules[visit.service].roles
    present = context.present_workers(visit.site)
    if not present:
        return Gap(visit, "not_on_site", f"{visit.site} 站当天没有可出勤人员")
    for role in roles:
        if not any(context.eligible(w, role) for w in present):
            return Gap(visit, "credential_gap", f"{visit.site} 站当天无人持 {role}")
    if len(roles) > 1:
        holders = {role: [w for w in present if context.eligible(w, role)] for role in roles}
        only = set().union(*holders.values())
        if len(only) == 1:
            who = next(iter(only))
            return Gap(visit, "same_person_two_roles",
                       f"{who} 一人持 {'/'.join(roles)}，双人服务不能由同一人兼任")
        if not context.role_options(visit):
            return Gap(visit, "credential_combo",
                       "持证人员有，但凑不出合规的两人组合")
    relevant = [context.workers[w] for w in present if context.role_options(visit)]
    if any(relevant) and all(
            any(overlap(visit.earliest, visit.latest_end, a0, a1) for a0, a1 in state.absences)
            for state in relevant):
        return Gap(visit, "training_or_absence",
                   "可用的持证人员在该时段都脱岗或受训")
    times = [state.window(visit.site) for state in context.workers.values()
             if state.window(visit.site)]
    if times and all(visit.latest_end <= t[0] or visit.earliest >= t[1] for t in times):
        return Gap(visit, "outside_window", "服务时段落在所有在岗窗口之外")
    return Gap(visit, "window_conflict", "可办理时段内已排不下（与已排班次或通勤冲突）")


def _committed_detail(context: RosterContext, visit: Visit, workers: Sequence[str],
                      start: int, fits_roles: bool) -> str:
    if not fits_roles:
        return "原约的持证组合在当天不成立，须通知家庭并由正式改约决定替补"
    for worker in workers:
        state = context.workers.get(worker)
        if state is None:
            return f"{worker} 当天没有到 {visit.site} 的可用时段，原时段不自动释放"
        window = state.window(visit.site)
        if window is None:
            return f"{worker} 当天不在 {visit.site} 站，原时段不自动释放"
        if not (window[0] <= start and start + visit.duration <= window[1]):
            return (f"{worker} 在 {visit.site} 的在岗窗口 {hhmm(window[0])}-"
                    f"{hhmm(window[1])} 覆盖不了原约 {hhmm(start)}-"
                    f"{hhmm(start + visit.duration)}，须通知家庭")
        if not context.worker_free(worker, visit.site, start, start + visit.duration):
            return f"{worker} 在原约时段已脱岗或已有别的预约，原时段不自动释放"
    return "原约与当班时间冲突，须通知家庭并由正式改约决定替补"


def solve(context: RosterContext, max_nodes: int = 200_000) -> RosterResult:
    """先锁定家庭已确认的班次，再尽量多排其余需求。"""
    assignments: list[Assignment] = []
    gaps: list[Gap] = []
    rebooking: list[Gap] = []

    for visit in [v for v in context.visits if v.committed]:
        if visit.key in {a.visit.key for a in assignments}:
            continue
        roles = context.rules[visit.service].roles
        fixed = list(visit.fixed_workers)
        start = visit.fixed_start if visit.fixed_start is not None else visit.earliest
        fits_roles = len(fixed) == len(roles) and len(set(fixed)) == len(fixed)
        if fits_roles:
            # 两人班要求一人覆盖一个角色；家庭已确认的班次只认已经生效的证书
            if len(roles) == 1:
                fits_roles = all(context.firm_eligible(w, roles[0]) for w in fixed)
            else:
                first, second = fixed
                fits_roles = (
                    (context.firm_eligible(first, roles[0])
                     and context.firm_eligible(second, roles[1]))
                    or (context.firm_eligible(first, roles[1])
                        and context.firm_eligible(second, roles[0]))
                )
        free = fits_roles and all(
            context.worker_free(w, visit.site, start, start + visit.duration) for w in fixed)
        if not (fits_roles and free):
            reason = "committed_credential" if not fits_roles else "committed_conflict"
            detail = _committed_detail(context, visit, fixed, start, fits_roles)
            issue = Gap(visit, reason, detail)
            rebooking.append(issue)
            gaps.append(issue)
            continue
        for worker in fixed:
            context.workers[worker].bookings.append(
                (visit.site, start, start + visit.duration))
        assignments.append(Assignment(visit, tuple(fixed), start, start + visit.duration))

    pending = [v for v in context.visits
               if not v.committed and v.key not in {a.visit.key for a in assignments}]
    pending.sort(key=lambda v: (v.latest_start, v.earliest, -v.duration))
    best: list[Assignment] = []
    nodes = 0
    done = False

    def search(index: int, chosen: list[Assignment]) -> None:
        nonlocal best, nodes, done
        if done:
            return
        nodes += 1
        if nodes > max_nodes:
            if len(chosen) > len(best):
                best = list(chosen)
            return
        if len(chosen) + (len(pending) - index) <= len(best):
            return
        if index == len(pending):
            best = list(chosen)
            if len(best) == len(pending):
                done = True
            return
        visit = pending[index]
        for workers in context.role_options(visit):
            for start in context.candidate_starts(visit, workers):
                assignment = context.book(visit, workers, start)
                chosen.append(assignment)
                search(index + 1, chosen)
                chosen.pop()
                context.unbook(workers)
                if done:
                    return
        search(index + 1, chosen)

    search(0, [])
    assignments.extend(best)
    served = {assignment.visit.key for assignment in assignments}
    for visit in pending:
        if visit.key not in served:
            gaps.append(_gap_reason(context, visit))
    return RosterResult(context.day, assignments, gaps, rebooking)


# 分时段负荷 ----------------------------------------------------------------


@dataclass
class BandLoad:
    day: dt.date
    site: str
    band: int
    role_demand: dict[str, int]
    capacity: dict[str, dict[str, object]]
    naive_slots: int          # 按名字在表上数出来的角色数
    filled_slots: int         # 按证书与同时在场核出来的可填角色数
    short_minutes: int
    total_demand: int

    @property
    def label(self) -> str:
        start, end = BANDS[self.band]
        return f"{hhmm(start)}-{hhmm(end)}"

    @property
    def duplicated_people(self) -> int:
        return max(0, self.naive_slots - self.filled_slots)


def round_role_demand(demand: Mapping[str, float]) -> dict[str, int]:
    """把摊布出来的小数分钟按角色取整，之后所有比较都用同一组整数。"""
    return {role: int(round(minutes)) for role, minutes in demand.items()
            if round(minutes) > 0}


def max_flow(demand: Mapping[str, int], capacity: Mapping[str, Mapping[str, object]],
             eligible: Mapping[str, set[str]]) -> int:
    """角色分钟 → 人员分钟的最大流。

    流量的上限同时受角色需求、人员可用分钟和"此人是否持这个证"约束，因此
    同一名持两证的人不会被算成两个人。调用方应先经
    :func:`round_role_demand` 统一取整，避免小数被截断后凭空产生缺口。
    """
    roles = [role for role, minutes in demand.items() if minutes > 0]
    workers = [w for w in capacity if capacity[w]]
    if not roles or not workers:
        return 0
    size = 2 + len(roles) + len(workers)
    source, sink = 0, size - 1
    graph = [[0] * size for _ in range(size)]

    def role_node(role: str) -> int:
        return 1 + roles.index(role)

    def worker_node(worker: str) -> int:
        return 1 + len(roles) + workers.index(worker)

    for role in roles:
        graph[source][role_node(role)] = int(demand[role])
    for worker in workers:
        graph[worker_node(worker)][sink] = int(capacity[worker]["total"])
    for role in roles:
        for worker in workers:
            if role in eligible.get(worker, set()):
                graph[role_node(role)][worker_node(worker)] = 10 ** 6

    flow = 0
    while True:
        parent = [-1] * size
        parent[source] = source
        queue = [source]
        while queue and parent[sink] == -1:
            node = queue.pop(0)
            for nxt in range(size):
                if graph[node][nxt] > 0 and parent[nxt] == -1:
                    parent[nxt] = node
                    queue.append(nxt)
        if parent[sink] == -1:
            return flow
        push = 10 ** 6
        node = sink
        while node != source:
            push = min(push, graph[parent[node]][node])
            node = parent[node]
        node = sink
        while node != source:
            graph[parent[node]][node] -= push
            graph[node][parent[node]] += push
            node = parent[node]
        flow += push


def free_minutes(context: RosterContext, worker: str, site: str,
                 start: int, end: int) -> int:
    """某人在某站在给定时段内还剩多少可用分钟（扣掉脱岗与已确认预约）。"""
    state = context.workers[worker]
    window = state.window(site)
    if window is None:
        return 0
    minutes = overlap(*window, start, end)
    for absent_start, absent_end in state.absences:
        minutes -= overlap(absent_start, absent_end, start, end)
    for booked_site, booked_start, booked_end in state.bookings:
        if booked_site != site:
            continue
        minutes -= overlap(booked_start, booked_end, start, end)
    return max(0, minutes)


def band_loads(context: RosterContext) -> list[BandLoad]:
    """分时段核对"表上人头"与"按证、按同时在岗真正可填的人手"。

    - 需求分钟：按可办理窗口把每班工时摊进各时段；
    - 表上人头：该时段可能落位的角色名额数（双人服务算两个）；
    - 真正可填：用最大流核对，每个在场持证人员最多顶一个名额，同一人不能
      同时充当两个角色；
    - 缺口分钟：按角色分钟与可用分钟核对后的差额。
    """
    loads: list[BandLoad] = []
    sites = sorted({visit.site for visit in context.visits} |
                   {site for state in context.workers.values() for site in state.windows})
    for site in sites:
        for index, (band_start, band_end) in enumerate(BANDS):
            role_demand: dict[str, int] = {}
            naive_slots = 0
            for visit in context.visits:
                if visit.site != site:
                    continue
                minutes = spread_minutes(visit.earliest, visit.latest_start,
                                         visit.duration, band_start, band_end)
                if minutes <= 0:
                    continue
                roles = context.rules[visit.service].roles
                naive_slots += len(roles)
                for role in roles:
                    role_demand[role] = role_demand.get(role, 0) + round(minutes)
            capacity: dict[str, dict[str, object]] = {}
            eligible: dict[str, set[str]] = {}
            for worker, state in context.workers.items():
                if state.window(site) is None:
                    continue
                minutes = free_minutes(context, worker, site, band_start, band_end)
                if minutes <= 0:
                    continue
                capacity[worker] = {"total": minutes}
                eligible[worker] = set(state.credentials)
            total = sum(role_demand.values())
            filled = max_flow(role_demand, capacity, eligible) if role_demand else 0
            # 人头口径：每个班次在该时段最多占一个该角色名额
            slot_demand: dict[str, int] = {}
            for visit in context.visits:
                if visit.site != site:
                    continue
                if spread_minutes(visit.earliest, visit.latest_start,
                                  visit.duration, band_start, band_end) <= 0:
                    continue
                for role in context.rules[visit.service].roles:
                    slot_demand[role] = slot_demand.get(role, 0) + 1
            slot_capacity = {worker: {"total": 1} for worker in capacity}
            matched = max_flow(slot_demand, slot_capacity, eligible) if slot_demand else 0
            loads.append(BandLoad(context.day, site, index, role_demand, capacity,
                                  naive_slots, matched, max(0, total - filled), total))
    return loads
