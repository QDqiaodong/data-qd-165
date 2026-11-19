# data-qd-165 · API配额窗口审计

Exact fixed-window API usage and quota breach attribution。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；建议 Python 3.12。使用 uv 建立独立环境，构建工具锁版本。首次安装无公共缓存时需要访问包索引；业务运行不访问网络。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/api-quota-audit --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

修改源码后重新安装。样例应报告 `window_count=3`、`exceeded_count=1`，超限窗口为 `/records`，使用量 7、上限 5。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

## 共享容量保留与申诉模式

`--mode retention`（默认 `audit` 行为与 v1 契约完全不变）回答在 principal 与 route 两类共享配额同时约束下应保留哪些请求，以及人工申诉某条请求必须保留时的替换与损失。输入/输出契约、求解结构与申诉隔离保证见 [数据契约](docs/contract.md)。

```sh
PYTHONPATH=src python3 -m api_quota_audit.cli \
  --mode retention --source fixtures/retention.json --output run/retention.json
```

输出 `schema_version=2`，`result` 含 `baseline`、逐项 `appeals` 和 `deduplicated_request_count`；三级目标为保留数、priority 总和、按 id 字典序。生产求解为多项式时间的最小费用最大流，不做子集枚举、不接外部优化器；每份申诉独立重求、互不污染。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/api_quota_audit/model.py 为业务模型，common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路，ledger.py 为独立的 SQLite 预付额度台账命令。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 区间计量证据对账模式

`--mode reconcile` 判断「采样系统的区间用量上下界」与「结算系统的区间精确账单」是否可能来自同一条整数用量时间序列，并在不可行时给出可逐边复核的负权环证书。输入/输出契约、求解结构与证书语义见 [数据契约](docs/contract.md) 第 3 节。

```sh
PYTHONPATH=src python3 -m api_quota_audit.cli \
  --mode reconcile --source fixtures/reconcile.json --output run/reconcile.json
```

输出 `schema_version=3`，`result` 严格含 `status`、`ranges`、`certificate`。`status` 为 `feasible`、`meter_conflict` 或 `invoice_conflict`；两个冲突是合法业务报告，退出码仍为 0。可行时每个 query 给出精确最小/最大值与两份完整 slot 分配见证；冲突时给出原始约束边组成的闭合负权环，边数不超过 `n+1`。全部使用精确整数差分约束与全点最短路，不做浮点近似、逐单位枚举或外部求解器。

## 本地持久预付额度台账（`api-credit-ledger`）

新增的独立命令把客户预付 units 存成一个 SQLite 文件中的持久台账：grant 批次有到期时刻，reserve 按 `(expires_at_us, lot_id)` 跨批次预占并冻结来源，commit 沿原分配顺序部分核销、cancel 全释放，批次到期或被 revoke 时依赖它的活跃 hold 整笔失效并释放其他来源。操作凭据按 `op_id` 冻结保存，可安全重试；报告在关闭重开后仍可复核每个批次的来源余额。输入/输出契约、存储结构、跨批次失效次序与事务边界见 [数据契约](docs/contract.md) 第 4 节。

```sh
.venv/bin/api-credit-ledger init   --db run/ledger.db
.venv/bin/api-credit-ledger apply  --db run/ledger.db --source fixtures/ledger.json
.venv/bin/api-credit-ledger report --db run/ledger.db --output run/ledger-report.json
```

`python -m api_quota_audit.ledger` 是同等入口。`init` 创建空库（不覆盖，stdout 为 `schema_version=1` 的 JSON）；`apply` 在一个事务内执行最多 500 个操作，成功 stdout 严格为 `receipts` 数组对象；`report` 导出已提交状态的只读快照，stdout 与写入文件逐字节相同。业务/输入错误退出 2、只写 stderr，不隐式建库、拒绝未知 schema 与符号链接 DB，报告不得与 DB 同一文件。仅用标准库 `sqlite3`，不连外部服务。

## 可追溯用量事实仓（`api-usage-warehouse`）

新增的独立命令把 API 用量事件与配额策略各自保存成不可覆盖的完整版本/撤回版本，每个成功批次分配一个连续知识序号，使运营能重现「当时已知的配额报告」。历史查询有两个独立截止：`--max-seq` 先选每个身份在知识截止内的最高版本（最新版本已撤回即视为不存在，绝不回退旧版本），再按 `--event-before` 的严格 `at <` 截止筛事件；策略按事件头的历史生效段归属，同一 epoch 窗口内不同策略版本各自使用自己的完整 `limit`。

```sh
.venv/bin/api-usage-warehouse create --db run/warehouse.db
.venv/bin/api-usage-warehouse ingest --db run/warehouse.db --source fixtures/warehouse.json
.venv/bin/api-usage-warehouse query  --db run/warehouse.db \
  --max-seq 1 --event-before 2026-01-01T02:00:00Z --output run/warehouse-report.json
```

`python -m api_quota_audit.warehouse` 是同等入口；Python 调用 `create_database`、`ingest_batch`、`query_warehouse`。`create` 不覆盖已有库，stdout 为 `{"schema_version":1}`；`ingest` 在一个事务内校验 revision 连续性、策略区间不重叠及每条有效事件唯一策略，成功后 stdout 为冻结的批次 receipt；`query` 从只读一致快照发布双截止报告，stdout 与输出文件逐字节相同。业务/输入错误退出 2、只写 stderr，失败批次不留下序号或版本；不隐式建库、拒绝未知 schema 与符号链接 DB、拒绝输出与 DB 同文件。历史存储方案与隔离性说明见 [数据契约](docs/contract.md) 第 5 节。



## API 共享阶梯计价与成本中心费用分摊（`--mode chargeback`）

`--mode chargeback` 把同一接口窗口内全部已知用量先按供应商阶梯汇总计价，再把精确有理数费用按池内用量占比分摊到各成本中心，并联合求解整数分矩阵：每池整数分合计等于该池精确总费用的四舍五入（恰半分向上），每中心合计等于输入的 `required_cents`，每格只能是自身精确费用的 floor 或 ceil，人工 lock 的格必须取其指定金额。主目标为跨全部格的最小绝对舍入误差，并列时取按 `(tariff_id, window_start_us, center)` 排序的完整 cents 向量字典序最小者。输入/输出契约、联合可分性、精确求解结构与复用旧流算法时的单位增广/初始势修正见 [数据契约](docs/contract.md) 第 6 节。

```sh
PYTHONPATH=src python3 -m api_quota_audit.cli \
  --mode chargeback --source fixtures/chargeback.json --output run/chargeback.json
```

输出 `schema_version=4`，`result` 严格含 `status`（`balanced`/`unallocatable`）、按池排序的 `pools`、按 id 排序的 `centers`、按向量顺序的 `cells`、`total_invoice_cents`、`rounding_error` 与 `deduplicated_event_count`。业务无解是合法报告、退出 0，但所有分币结果与误差为 `null`，精确计价基础仍完整返回；输入/格式/文件错误退出 2 并保留旧报告。全部金额与误差为精确有理数，不使用浮点 epsilon、逐分金额枚举或外部求解器；本模式不读写额度台账与事实仓。
