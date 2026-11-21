# 居家服务工时预测与短期资质缺口排班

站点班务台在各排班日可以看到的服务申请、在岗和培训资料。 所有人员、服务机构和业务凭证均为合成资料。

时间为 UTC+08:00。请求、实际履约与撤单均以各自的登记时间确定何时可见。minute 是从当日 00:00 起计的分钟数。连班路程表记录调配两个站点间所需的最低分钟。预测请求按班期分组，历史、已排和后续观测分别留档。

## 资料查阅

需要 Python 3.11.2，查看资料与运行资料检查只用标准库。CSV 使用 UTF-8，首行为字段名。空字段表示该项未填写或不适用，具体含义按各表约定。

```sh
python3 catalog.py
python3 -m unittest discover -s tests -v
```

例如，`python3 catalog.py --table requests` 可读取该表原始记录。资料检查核对结构、唯一键及部分业务引用。

## 交换表

- `requests.csv`。派单台登记的已知服务需求及可办理时段。 主键由 `request` 确定。
- `visit_events.csv`。实际履约凭证。returned 对陪诊包括返站，unassigned 是明确无接单。 主键由 `request`、`kind` 确定。
- `calendar.csv`。排班日前已经可见的活动，以及明确暂停窗口。 主键由 `day`、`site` 确定。
- `planning_dates.csv`。历史回看排班日与下一班期。截止日当天有时分登记时，以当日 18:00 为界。 主键由 `round` 确定。
- `service_rules.csv`。同场服务的资格。primary 和 partner 均需满足，空伙伴表示一人。 主键由 `service` 确定。
- `availability.csv`。人员在实际站点可工作的区间。同 capacity_id 的外援时段只有一份。 主键由 `capacity_id`、`site` 确定。
- `qualifications.csv`。人员已有资质时间范围，to 不含。尚待结业的资格单列培训表。 主键由 `worker`、`credential` 确定。
- `courses.csv`。培训开课、脱岗和预计可发凭证的最早日期。eligible 列列出本期符合报名条件的人。 主键由 `course` 确定。
- `transport.csv`。站点双向通勤分钟。 主键由 `origin`、`destination` 确定。
- `agreements.csv`。已确认的固定服务、借班和培训状态。occupied 是已经占用，cancelled 是后续正式释放。 主键由 `event`、`recorded` 确定。
- `actual_adjustments.csv`。班务实际兑现的记录。联络失败和调配撤销均不能视为替补已在岗。 主键由 `reference`、`day` 确定。
- `planning_policy.csv`。外援配额按站点共享，培训费用的上限用内部额度单位。 主键由 `round` 确定。
