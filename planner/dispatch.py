"""把方案落成可以发给站点的调配，并在情况变化后复核还剩哪些能发。

站长先比较方案，再逐条确认。确认之后如果报名取消、外援被另一站先占，或者
家庭确认的时段发生变化，不能整包照发：要按受影响的真实时段，重新核对哪些
调配仍然成立、哪些必须撤回或改期。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

from .data import Dataset, View
from .decide import Option, check_option
from .roster import loan_sites_on


@dataclass
class Dispatch:
    kind: str            # enroll / loan
    subject: str
    worker: str
    site: str
    day: dt.date
    start_minute: int
    end_minute: int
    status: str = "ready"     # ready / blocked / withdrawn / superseded / locked
    reason: str = ""

    def render(self) -> str:
        where = f"{self.day} {self.site}" if self.kind == "loan" else self.day.isoformat()
        return f"{self.kind} {self.subject}→{self.worker} {where} [{self.status}] {self.reason}"


def build_dispatches(view: View, option: Option) -> list[Dispatch]:
    """把方案拆成逐条可发给站点的调配。"""
    items: list[Dispatch] = []
    for course_id, worker in sorted(option.trainings):
        course = view.courses.get(course_id)
        if course is None:
            continue
        items.append(Dispatch("enroll", course_id, worker, "", course.starts,
                              course.start_minute, course.end_minute))
    for capacity_id, day, site in sorted(option.activations):
        window = next((a for a in view.availability
                       if a.capacity_id == capacity_id and a.site == site
                       and a.covers(day)), None)
        items.append(Dispatch("loan", capacity_id,
                              window.worker if window else "",
                              site, day,
                              window.start_minute if window else 0,
                              window.end_minute if window else 0))
    return items


def recheck(dataset: Dataset, as_now: dt.date, dispatches: Sequence[Dispatch],
            option: Option, ) -> list[Dispatch]:
    """按"今天"重看：只保留仍然能发的部分，其余说明原因。"""
    view = dataset.view(as_now)
    admission = check_option(dataset, view, option, as_now,
                             dataset.policy.get("R4"))
    blocked = set()
    for message in admission.blockers:
        for item in dispatches:
            if item.subject in message:
                blocked.add((item.kind, item.subject, item.day))
    result: list[Dispatch] = []
    for item in dispatches:
        copy = Dispatch(item.kind, item.subject, item.worker, item.site, item.day,
                        item.start_minute, item.end_minute)
        if item.kind == "enroll":
            course = view.courses.get(item.subject)
            if course is None:
                copy.status, copy.reason = "withdrawn", "课程已不在可见资料里"
            elif course.starts <= as_now <= course.ends:
                copy.status = "locked"
                copy.reason = "已经在脱岗中，撤回也不恢复当班在岗"
            elif as_now > course.ends:
                copy.status, copy.reason = "superseded", "课程已结束，改为核对发证与资格"
            elif item.worker not in course.eligible:
                copy.status, copy.reason = "blocked", "报名条件已不含该人员"
            elif course.cost > (dataset.policy.get("R4").max_course_cost
                                if dataset.policy.get("R4") else course.cost):
                copy.status, copy.reason = "blocked", "超出培训费用额度"
        else:
            confirmed = loan_sites_on(view, item.day)
            if item.subject in confirmed and confirmed[item.subject] != item.site:
                copy.status = "superseded"
                copy.reason = f"该借班时段已被 {confirmed[item.subject]} 站确认"
            elif (item.kind, item.subject, item.day) in blocked:
                copy.status, copy.reason = "blocked", "准入核对未通过"
            elif not any(a.capacity_id == item.subject and a.site == item.site
                         and a.covers(item.day) for a in view.availability):
                copy.status, copy.reason = "withdrawn", "该日已无此站可用时段"
        result.append(copy)
    return result


def affected_bands(dispatches: Sequence[Dispatch],
                   changed: Sequence[Dispatch]) -> list[str]:
    """只圈出受影响的真实时段，而不是整包重来。"""
    keys = set()
    for item in changed:
        keys.add((item.day, item.site or "*"))
    return sorted(f"{day} {site}" for day, site in keys)


def sendable(dispatches: Sequence[Dispatch]) -> list[Dispatch]:
    return [d for d in dispatches if d.status == "ready"]
