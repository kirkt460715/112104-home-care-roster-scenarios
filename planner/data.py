"""交换资料的读取与按排班日可见性筛选。

所有时间均为 UTC+08:00；``minute`` 是当日 00:00 起计的分钟数。
资料本身不改写，任何"当时能看到什么"都由 :class:`View` 按登记日期裁出。
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path


DATA = Path(__file__).resolve().parents[1] / "data"

# 抹平 int 比较与格式化的小工具 ------------------------------------------------


def as_date(value: str) -> dt.date:
    """解析 ``YYYY-MM-DD`` 日期。"""
    return dt.date.fromisoformat(value)


def as_minute(value: str) -> int:
    return int(value)


def hhmm(minute: int) -> str:
    """把当日分钟数打印成 ``HH:MM``。"""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def read_csv(name: str, root: Path = DATA) -> list[dict[str, str]]:
    with (root / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# 记录类型 --------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    request: str
    site: str
    service: str
    day: dt.date
    earliest: int  # 最早开始
    deadline: int  # 最晚开始
    recorded: dt.date

    @property
    def window(self) -> int:
        return self.deadline - self.earliest


@dataclass(frozen=True)
class Event:
    request: str
    kind: str  # started / returned / unassigned / canceled
    start_minute: int | None
    end_minute: int | None
    recorded: dt.date


@dataclass(frozen=True)
class CalendarEntry:
    day: dt.date
    kind: str  # 早市 / stop
    recorded: dt.date
    site: str


@dataclass(frozen=True)
class Round:
    name: str
    as_of: dt.date
    starts: dt.date
    ends: dt.date

    def days(self) -> list[dt.date]:
        return [self.starts + dt.timedelta(days=i)
                for i in range((self.ends - self.starts).days + 1)]


@dataclass(frozen=True)
class Rule:
    service: str
    primary: str
    partner: str  # 空串表示一人服务

    @property
    def roles(self) -> tuple[str, ...]:
        return (self.primary,) if not self.partner else (self.primary, self.partner)

    @property
    def headcount(self) -> int:
        return len(self.roles)


@dataclass(frozen=True)
class Availability:
    capacity_id: str
    worker: str
    site: str
    start_date: dt.date
    end_date: dt.date  # 含
    start_minute: int
    end_minute: int
    kind: str  # regular / loan

    def covers(self, day: dt.date) -> bool:
        return self.start_date <= day <= self.end_date


@dataclass(frozen=True)
class Qualification:
    worker: str
    credential: str
    start_date: dt.date
    end_date: dt.date  # 不含

    def valid(self, day: dt.date) -> bool:
        return self.start_date <= day < self.end_date


@dataclass(frozen=True)
class Course:
    course: str
    starts: dt.date
    ends: dt.date
    start_minute: int
    end_minute: int
    seats: int
    credential: str
    earliest_cert: dt.date
    eligible: tuple[str, ...]
    cost: int

    def day_span(self, day: dt.date) -> bool:
        return self.starts <= day <= self.ends


@dataclass(frozen=True)
class Agreement:
    event: str
    kind: str  # visit / loan / course
    subject: str
    worker: str
    site: str
    starts: dt.date
    start_minute: int
    end_minute: int
    state: str  # occupied / cancelled
    recorded: dt.date


@dataclass(frozen=True)
class Adjustment:
    reference: str
    day: dt.date
    worker: str
    site: str
    status: str  # served / released / contact_failed / cert_delayed / cert_granted


@dataclass(frozen=True)
class Policy:
    round: str
    max_loan_days: int
    max_course_cost: int
    max_late_probability: float


# 数据集 ----------------------------------------------------------------------


@dataclass
class Dataset:
    requests: list[Request]
    events: list[Event]
    calendar: list[CalendarEntry]
    rounds: list[Round]
    rules: dict[str, Rule]
    availability: list[Availability]
    qualifications: list[Qualification]
    courses: list[Course]
    transport: dict[tuple[str, str], int]
    agreements: list[Agreement]
    adjustments: list[Adjustment]
    policy: dict[str, Policy]

    # -- 读取 --------------------------------------------------------------
    @classmethod
    def load(cls, root: Path = DATA) -> "Dataset":
        requests = [Request(r["request"], r["site"], r["service"], as_date(r["day"]),
                            as_minute(r["earliest"]), as_minute(r["deadline"]),
                            as_date(r["recorded"]))
                    for r in read_csv("requests", root)]
        events = [Event(r["request"], r["kind"],
                        as_minute(r["start_minute"]) if r["start_minute"] else None,
                        as_minute(r["end_minute"]) if r["end_minute"] else None,
                        as_date(r["recorded"]))
                  for r in read_csv("visit_events", root)]
        calendar = [CalendarEntry(as_date(r["day"]), r["kind"], as_date(r["recorded"]), r["site"])
                    for r in read_csv("calendar", root)]
        rounds = [Round(r["round"], as_date(r["as_of"]), as_date(r["starts"]), as_date(r["ends"]))
                  for r in read_csv("planning_dates", root)]
        rules = {r["service"]: Rule(r["service"], r["primary"], r["partner"])
                 for r in read_csv("service_rules", root)}
        availability = [Availability(r["capacity_id"], r["worker"], r["site"],
                                     as_date(r["from"]), as_date(r["to"]),
                                     as_minute(r["start"]), as_minute(r["end"]), r["kind"])
                        for r in read_csv("availability", root)]
        qualifications = [Qualification(r["worker"], r["credential"],
                                        as_date(r["from"]), as_date(r["to"]))
                          for r in read_csv("qualifications", root)]
        courses = [Course(r["course"], as_date(r["starts"]), as_date(r["ends"]),
                          as_minute(r["start_minute"]), as_minute(r["end_minute"]),
                          int(r["seats"]), r["credential"], as_date(r["earliest_cert"]),
                          tuple(w for w in r["eligible"].split("|") if w), int(r["cost"]))
                   for r in read_csv("courses", root)]
        transport = {(r["origin"], r["destination"]): int(r["minutes"])
                     for r in read_csv("transport", root)}
        agreements = [Agreement(r["event"], r["kind"], r["subject"], r["worker"], r["site"],
                                as_date(r["starts"]), as_minute(r["start_minute"]),
                                as_minute(r["end_minute"]), r["state"], as_date(r["recorded"]))
                      for r in read_csv("agreements", root)]
        adjustments = [Adjustment(r["reference"], as_date(r["day"]), r["worker"],
                                  r["site"], r["status"])
                       for r in read_csv("actual_adjustments", root)]
        policy = {r["round"]: Policy(r["round"], int(r["max_loan_days"]),
                                     int(r["max_course_cost"]),
                                     float(r["max_late_probability"]))
                  for r in read_csv("planning_policy", root)}
        return cls(requests, events, calendar, rounds, rules, availability,
                   qualifications, courses, transport, agreements, adjustments, policy)

    # -- 基本查询 ----------------------------------------------------------
    def round(self, name: str) -> Round:
        for item in self.rounds:
            if item.name == name:
                return item
        raise KeyError(f"未知班期 {name!r}")

    def request(self, request_id: str) -> Request:
        for item in self.requests:
            if item.request == request_id:
                return item
        raise KeyError(request_id)

    def view(self, as_of: dt.date) -> "View":
        return View.from_dataset(self, as_of)


# 按排班日可见的切片 ----------------------------------------------------------


@dataclass
class View:
    """站在 ``as_of`` 当天能看到的事实。

    - 请求、履约、活动、协议都按各自的 ``recorded`` 截断；
    - 协议按 event 取 ``recorded`` 最新的一条，取消即释放；
    - 资格与可用时段没有登记日期，始终可见。
    """

    as_of: dt.date
    dataset: Dataset
    requests: list[Request]
    events: dict[str, tuple[Event, ...]]
    calendar: list[CalendarEntry]
    agreements: dict[str, Agreement]
    courses: dict[str, Course]
    availability: list[Availability]
    qualifications: list[Qualification]

    @classmethod
    def from_dataset(cls, dataset: Dataset, as_of: dt.date) -> "View":
        requests = [r for r in dataset.requests if r.recorded <= as_of]
        by_request: dict[str, list[Event]] = {}
        for event in dataset.events:
            if event.recorded <= as_of:
                by_request.setdefault(event.request, []).append(event)
        events = {key: tuple(value) for key, value in by_request.items()}
        calendar = [c for c in dataset.calendar if c.recorded <= as_of]
        latest: dict[str, Agreement] = {}
        for agreement in dataset.agreements:
            if agreement.recorded > as_of:
                continue
            current = latest.get(agreement.event)
            if current is None or agreement.recorded >= current.recorded:
                latest[agreement.event] = agreement
        courses = {course.course: course for course in dataset.courses}
        return cls(as_of, dataset, requests, events, calendar, latest, courses,
                   dataset.availability, dataset.qualifications)

    # -- 请求/事件 ---------------------------------------------------------
    def request_ids(self) -> set[str]:
        return {r.request for r in self.requests}

    def event_map(self, request_id: str) -> dict[str, Event]:
        return {event.kind: event for event in self.events.get(request_id, ())}

    def completed_visits(self) -> list[tuple[Request, int]]:
        """只返回完整工时（started 与 returned 均可见）的请求及分钟数。"""
        result = []
        by_id = {r.request: r for r in self.requests}
        for request_id, events in self.events.items():
            if request_id not in by_id:
                continue
            event_map = {e.kind: e for e in events}
            started, returned = event_map.get("started"), event_map.get("returned")
            if not started or not returned:
                continue
            if started.start_minute is None or returned.end_minute is None:
                continue
            if returned.end_minute <= started.start_minute:
                continue
            result.append((by_id[request_id], returned.end_minute - started.start_minute))
        result.sort(key=lambda item: (item[0].day, item[0].site, item[0].service, item[0].request))
        return result

    # -- 活动 --------------------------------------------------------------
    def calendar_on(self, day: dt.date, site: str) -> list[CalendarEntry]:
        return [c for c in self.calendar if c.day == day and c.site == site]

    def market_days(self) -> set[dt.date]:
        return {c.day for c in self.calendar if c.kind == "早市"}

    def stopped(self, day: dt.date, site: str) -> bool:
        return any(c.kind == "stop" for c in self.calendar_on(day, site))

    # -- 协议 --------------------------------------------------------------
    def active_agreements(self, kind: str | None = None) -> list[Agreement]:
        return [a for a in self.agreements.values()
                if a.state == "occupied" and (kind is None or a.kind == kind)]

    def course_enrollments(self) -> list[Agreement]:
        return self.active_agreements("course")

    def certificate_dates(self) -> dict[tuple[str, str], dt.date]:
        """考试/培训结业后可见的凭证生效日，按课程表的最早发证日。"""
        dates: dict[tuple[str, str], dt.date] = {}
        enrolled = {(a.subject, a.worker) for a in self.course_enrollments()}
        for course_id, worker in enrolled:
            course = self.courses.get(course_id)
            if course is None:
                continue
            key = (worker, course.credential)
            current = dates.get(key)
            if current is None or course.earliest_cert < current:
                dates[key] = course.earliest_cert
        return dates
