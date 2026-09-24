# 工业专网能力预约与违约归因平台

多车间制造集团把质检、控制与设备协同流量统一接入园区专网的服务端平台：维护链路容量、
时延等级、可靠性目标、维护窗口与租户配额；对批量预约执行**原子化准入**并给出可解释拒绝
原因；已确认预约遇到维护、故障或遥测退化时按**业务优先级**迁移、降级或中止；补偿只依据
**连续观测窗口与归因结果**计算，重复遥测与重复结算不改变账本；支持更正历史遥测后重算受
影响时段而**不污染已封账月份**。

## 分层结构

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 领域层 | `industrial_capacity/domain/` | 枚举与实体：链路、租户、维护窗口、预约、遥测、事件、处置动作、补偿、账本、账期 |
| 应用层 | `industrial_capacity/application/` | 用例服务与纯算法：`admission` 准入、`operations` 运维处置、`telemetry` 观测、`settlement` 结算、`catalog` 基础数据 |
| 端口 | `application/ports.py`、`application/repository.py` | 时钟、ID 生成、持久化协议（可替换） |
| 适配器 | `industrial_capacity/adapters/` | 事务化内存仓库（单锁串行化）、可控时钟、顺序 ID |
| 接口层 | `industrial_capacity/interfaces/` | 标准库 `http.server` 实现的本地 JSON API、序列化器、端到端演示 |

时间统一为整数秒 Unix 时间，窗口一律采用半开区间 `[start, end)`；结算账期按 UTC `YYYY-MM`。

## 关键业务规则

- **原子化批量准入**：一个批量在同一事务内校验，任一项不满足则整批不落库；拒绝逐项给出
  原因码（容量不足、租户配额超限、维护冲突、链路故障、时延/可靠性不满足、链路或租户不存
  在等）与并发占用明细。容量判定同时计入既有预约与同批已过检项；并发批量由仓库互斥锁串行
  化，保证不超卖、不挤占既有生产线。
- **优先级处置**：事件（故障/维护/遥测退化）影响的预约按 `控制(40) > 质检(30) > 协同(20)`
  排序竞争替代路径容量。次序为：可迁移到满足 SLO 且有余量的链路 → 迁移（无中断、无补偿）；
  遥测退化时可降级业务降一档运行；控制类不可降级，链路硬不可用且无替代时中止。
- **连续观测窗口**：连续不少于 3 个真实观测分钟桶违反预约 SLO 才构成退化事件；瞬时抖动与
  数据缺口不触发。降级补偿按连续违约分钟 × 2 元/分钟，中止补偿按不可用分钟 × 6 元/分钟。
- **结算幂等**：补偿以 `(事件, 预约)` 为唯一幂等键，账本分录另有唯一过账键；重复结算返回
  同一笔，`/settlement/replay` 可任意重放，账本条目与余额不变。重复遥测（内容相同的重发）
  被幂等忽略，不产生新版本。
- **更正重算与封账保护**：遥测更正生成新版本（旧版本标记失效但保留可审计），重算受影响
  时段；事件不再成立则撤销并在**当前开放账期**全额冲销，金额变化则以差额 ADJUSTMENT 计入
  当前开放账期。已封账月份的历史分录永不改写，只追加可审计的冲销/调整。

## 本地 API

启动（固定基准时钟，便于确定性复现）：

```bash
python3 -m industrial_capacity.interfaces.api --port 8080
```

主要端点（JSON）：

- `POST /admin/links`、`POST /admin/links/{code}/status`、`GET /admin/links`
- `POST /admin/tenants`、`GET /admin/tenants`
- `POST /admin/maintenance`、`GET /admin/maintenance?link=`
- `POST /admin/periods/close`、`GET /admin/periods`
- `POST /admin/clock/advance`、`POST /admin/clock/set`（测试时钟）
- `POST /reservations/batches`（接受 201 / 整批拒绝 409，body 含逐项原因）
- `GET /reservations/batches/{code}`、`GET /reservations`
- `POST /telemetry/{link}`、`POST /telemetry/{link}/correct`、`GET /telemetry/{link}/versions?bucket_ts=`
- `POST /ops/failures`、`POST /ops/maintenance/activate`、`POST /ops/degradation/detect`
- `POST /ops/incidents/{code}/close`、`GET /ops/incidents`、`GET /ops/actions`
- `POST /settlement/incidents/{code}/settle`、`POST /settlement/settle-all`、`POST /settlement/replay`
- `POST /settlement/recompute`、`GET /settlement/compensations`、`GET /ledger`

## 场景演示

通过真实 HTTP 请求完整演示：并发预约、跨午夜维护、部分链路故障、结算重放与更正：

```bash
python3 -m industrial_capacity.interfaces.demo
```

## 测试

在项目根目录执行（40 个测试：纯算法/服务单元测试 + 真实 HTTP 驱动的四类 API 场景测试）：

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

```bash
python3 -m compileall -q industrial_capacity tests
```
