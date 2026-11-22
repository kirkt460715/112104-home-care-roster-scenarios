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

## 排班助手

`planner/` 在只读资料之上给出预测、风险、备班比较与回看，不改动任何交换表。

```sh
python3 -m planner.report                 # 全部小节
python3 -m planner.report --section risk  # 只看工时风险
python3 -m planner.report --section load --day 2026-10-09
python3 -m planner.report --section options --scenarios 400
```

小节依次是：历史资料可用性、工时分布、工时风险、分时段人手核对、家庭已确认时段、
备班方案比较、区间覆盖检验、调配清单、回看。

几条贯穿全部结果的口径：

- **可见性**。请求、履约、活动、协议都按各自 `recorded` 截断；`planning_dates` 的
  `as_of` 就是排班日。排班日之后才登记的返回时间不会出现在当天判断里。
- **完整工时**。只有 `started` 与 `returned` 两端齐全的班次才产生工时；`unassigned`
  （明确没人接）与 `canceled`（真实没人申请）分开统计，都不当作完成工时。
- **资格**。基础资格按 `qualifications` 的 `from`/`to`（`to` 不含）；培训换来的资格
  要等证书生效（规划按 `earliest_cert`，回看按 `cert_granted`）。家庭已确认的班次
  只认当期已经生效的证书。
- **人手**。姓名逐个落位：双人服务要两个不同的人，同一名持两证的人不会被算成两个人，
  跨站要留够 `transport` 的通勤分钟。
- **外援与培训**。同一 `capacity_id` 的借班时段一天只能落一个站；开课之后的培训
  不会因后来撤班而恢复在岗；明天的证书不能拿来承诺今天的家庭时段。
- **预测**。整日重抽样历史可比日，所以同站同天各项服务会一起变紧；已知派单是不可
  再低的下界。风险按 90 分钟时段给出超载概率，并单独统计同日同站多时段一起超载的
  联合概率（同一情景内判定，不能把单时段概率相乘）。
- **回看**。只回放当时可见的事实；实际可比较的时段、低支撑组合、明确停办窗口分列。
  建议兑现按可见派单归因：当初建议没覆盖的是需求预估偏差，建议排了却没到岗的是
  调配没落实；还没有结论的时段不参与归因。

`python3 -m unittest discover -s tests -v` 同时覆盖资料结构与上面这些业务口径。

## 交换表

- `requests.csv`。派单台登记的已知服务需求及可办理时段。 主键由 `request` 确定。
- `visit_events.csv`。实际履约凭证。returned 对陪诊包括返站，unassigned 是明确无接单。 主键由 `request`、`kind` 确定。
- `calendar.csv`。排班日前已经可见的活动，以及明确暂停窗口。 主键由 `day`、`site` 确定。
- `planning_dates.csv`。历史回看排班日与下一班期。截止日当天有时分登记时，以当日 18:00 为界。 主键由 `round` 确定。
- `service_rules.csv`。同场服务的资格。primary 和 partner 均需满足，空伙伴表示一人；双人服务需要不同人员担任。 主键由 `service` 确定。
- `availability.csv`。人员在实际站点可工作的区间。同 capacity_id 的外援时段只有一份。 主键由 `capacity_id`、`site` 确定。
- `qualifications.csv`。人员已有资质时间范围，to 不含。尚待结业的资格单列培训表。 主键由 `worker`、`credential` 确定。
- `courses.csv`。培训开课、脱岗和预计可发凭证的最早日期。eligible 列列出本期符合报名条件的人。 主键由 `course` 确定。
- `transport.csv`。站点双向通勤分钟。 主键由 `origin`、`destination` 确定。
- `agreements.csv`。已确认的固定服务、借班和培训状态。occupied 是已经占用，cancelled 是后续正式释放。 主键由 `event`、`recorded` 确定。
- `actual_adjustments.csv`。班务实际兑现的记录。联络失败和调配撤销均不能视为替补已在岗。 主键由 `reference`、`day` 确定。
- `planning_policy.csv`。外援配额按站点共享，培训费用的上限用内部额度单位。 主键由 `round` 确定。
