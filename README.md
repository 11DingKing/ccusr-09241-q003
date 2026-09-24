# 工业专网能力预约与违约归因平台

面向多车间制造集团的园区专网运营平台:维护链路容量、时延等级、可靠性目标、
维护窗口与租户配额,对批量能力预约执行原子化准入并给出可解释的拒绝原因;
已确认预约遇到计划维护或遥测退化时按业务优先级选择迁移、降级或中止;
补偿额度依据连续观测窗口与归因结果计算,重复遥测与重复结算不改变最终账本;
支持更正历史遥测后重算受影响时段,且不污染已封账月份。

## 工程约定

项目采用 Python 包目录组织服务端代码,仅使用标准库,无第三方依赖。
领域模型、应用服务、持久化适配和接口层保持边界清晰;时间、标识生成及
外部观测均通过可替换端口接入,便于稳定复现业务过程。运行数据不得写入
源码目录,临时文件和本地配置由 `.gitignore` 排除。

## 架构分层

```
industrial_capacity/
├── domain/            # 纯领域规则,不感知持久化与协议
│   ├── models.py      # 链路/配额/预约/遥测/违约窗口/台账等模型
│   ├── admission.py   # 容量分段核算与批量原子准入
│   ├── observation.py # 连续观测窗口识别与归因
│   ├── remediation.py # 按业务优先级的迁移/降级/中止决策
│   ├── settlement.py  # 补偿额度计算与台账幂等调和
│   └── timeutil.py    # UTC 时间与账期(自然月)工具
├── application/       # 用例编排;时钟与 ID 生成以端口注入
│   └── services.py    # 目录/准入/处置/遥测/结算五类应用服务
├── adapters/          # 内存存储、系统/可控时钟、UUID/序列 ID
├── interfaces/        # 标准库 HTTP JSON API
├── app.py             # 组合根 create_app()
└── __main__.py        # 本地运行入口
```

## 核心语义

- **原子化批量准入**:整批全有或全无;任一请求失败则整批拒绝,并逐条给出
  可读原因(容量不足含差值、时延等级不足、维护冲突、配额超限等)。
  `batch_id` 为幂等键,重放返回首次判定。判定与落库在同一把锁内完成,
  并发提交不会超占容量。
- **连续观测窗口**:单样本可用容量低于应然需求记为违约样本;按采样间隔
  连续出现且达到门槛(默认 2 个)才确认为违约序列,断档即打断;序列闭合
  (健康样本/断档/超时)后固化为窗口。窗口与维护窗口相交归因 MAINTENANCE
  (不补偿),否则归因 PLATFORM。
- **处置**:违约序列确认或维护窗口创建时,按业务优先级降序处置——优先
  迁移到满足时延等级且容量足够的其他链路,其次在本链路降级,剩余容量为
  零则中止。处置动作以 `触发源|事件|预约` 去重,重复评估不重复执行。
  违约判定基于历史应然需求(按处置记录逆向还原),不受处置自身影响。
- **补偿与结算**:补偿额度 = 快照带宽 × 受影响分钟数 × (1 − 服务达成率),
  按自然月账期切分;台账条目携带内容派生的确定性 ID 与去重键,重复结算、
  重复遥测、窗口重算均不改变最终账本。账期封账后禁止再结算。
- **历史更正**:更正样本取代原始观测(同一样本多次更正以最后到达为准)。
  开放账期就地重算(同键条目内容替换);已封账账期条目保持原样,差额以
  `CORRECTION_ADJUSTMENT` 调整条目计入当前开放账期并指向被调整账期,
  同一更正重放不产生新条目。

## 本地运行

```bash
python3 -m industrial_capacity          # 默认 127.0.0.1:8080
IC_PORT=9000 python3 -m industrial_capacity
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/links` | 注册链路(容量/时延等级/可靠性目标) |
| GET | `/links` | 链路列表 |
| PUT | `/tenants/{tenant}/quota` | 设置租户配额 |
| POST | `/links/{link}/maintenance-windows` | 创建维护窗口(触发处置) |
| GET | `/maintenance-windows?link_id=` | 维护窗口列表 |
| POST | `/reservation-batches` | 批量预约原子准入(幂等) |
| GET | `/reservation-batches/{batch}` | 批次判定结果 |
| GET | `/reservations/{id}`、`/reservations?tenant_id=` | 预约查询 |
| POST | `/telemetry/samples` | 遥测摄取(按 `sample_id` 幂等) |
| POST | `/telemetry/corrections` | 历史遥测更正与重算 |
| GET | `/breach-windows?link_id=` | 违约窗口查询 |
| GET | `/remediation-actions?reservation_id=` | 处置动作查询 |
| POST | `/settlements/{period}/run` | 执行账期结算(重放安全) |
| POST | `/settlements/{period}/seal` | 账期封账 |
| GET | `/settlements` | 账期列表 |
| GET | `/ledger?tenant_id=&period=` | 台账查询 |

错误统一返回 `{"error": {"code", "message", "details"}}`;已封账账期的
结算请求返回 `409 PERIOD_SEALED`。

## 测试

在项目根目录执行:

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖领域规则(准入分段核算、连续窗口识别、优先级处置、补偿计算与
幂等调和)以及本地 API 场景:并发预约不超占、跨午夜维护与容量核算、
部分链路故障下的迁移/降级与补偿、重复遥测与结算重放账本不变、
封账月更正只产生调整条目、跨月违约窗口按账期切分。

## 编译检查

在项目根目录执行:

```bash
python3 -m compileall -q industrial_capacity tests
```
