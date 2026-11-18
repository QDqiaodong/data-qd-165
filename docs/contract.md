# API 配额审计数据契约

本工具有三种离线模式，均只读取本地 JSON、只写本地报告，不调用任何外部服务。此外提供一个独立的本地持久化预付额度台账命令（见第 4 节），只使用 Python 标准库 `sqlite3` 与本地文件，不连接任何外部服务，也不改变上述三种模式的输入与结果。

## 1. 默认固定窗口用量审计（schema_version=1，`--mode audit`，可省略）

Policies have unique ids and exact principal/route scopes, window_seconds 1..31,536,000 and positive integer unit limits. Events have id, principal, route, ISO timestamp with explicit offset, and positive integer units. Identical repeated event ids are deduplicated; conflicting payloads fail the whole input.

Each event belongs to exactly one matching policy. UTC epoch-anchored fixed windows are half-open [start,end); timestamps at the boundary begin the next window. Units are summed exactly and excess is max(0,used-limit). Only observed windows are emitted. No rolling windows, burst buckets, policy inheritance, online throttling or external API calls are modeled.

## 2. 共享容量保留与申诉审计（schema_version=2，`--mode retention`）

### 2.1 输入

顶层字段严格为 `principal_policies`、`route_policies`、`requests`、`appeals`，缺字段或多字段整份失败。

* `principal_policies` / `route_policies`：每类最多 100 项。每项严格为 `principal`/`route`、`window_seconds`、`limit`；scope 在各自类内唯一，重复即失败。`window_seconds` 为 1..31,536,000 的整数，`limit` 为 1..400 的整数；布尔值一律拒绝（Python 的 `True/False` 不会被当作 1/0）。
* `requests`：最多 400 项，每项严格为 `id`、`principal`、`route`、`at`、`priority`，没有 `units`，每行是一个不可拆分的单位请求。
  * `id`、`principal`、`route` 沿用 ident 约束（非空白、去首尾空格后等于自身、长度 ≤128、无控制字符）。
  * `at` 沿用显式时区、精确到微秒的 instant 语义；不同偏移表达的同一 UTC 时刻视为同一时刻。
  * `priority` 为 0..1,000,000,000 的整数，拒绝布尔与浮点。
  * 相同 `id` 且规范化后全部内容相同视为同一请求（去重，输入可重复出现）；同 id 但任何规范化字段冲突则整份失败。
  * 每条请求都必须同时找到唯一的 principal 策略和唯一的 route 策略；即使最终不保留，也先做该校验，找不到任一侧即整份失败。
* `appeals`：最多 20 个不重复、且必须已存在于 `requests` 的请求 id；空数组合法，空请求集合法。
* 所有标识符比较采用 Python 字符串序。

### 2.2 窗口与容量

每个策略按自己的 `window_seconds` 以 UTC epoch 为锚点切半开窗口 `[start, end)`：
`start = floor(at_micros / (window_seconds * 1e6)) * window_seconds`。负 epoch 时间同样按地板除法落入其之前的窗口。principal 窗口与 route 窗口独立锚定，长度可以不同、边界可以不对齐，窗口之间没有跨期结转。

每条保留的请求同时占用其 principal 窗口和 route 窗口各 **一个** 名额；两侧各自的 `limit` 分别约束保留数量。

### 2.3 三级业务目标

在全部窗口容量内依次优化，前一级相同才比较下一级，不引入任何额外隐藏规则（如早到优先）：

1. 最大化保留请求数；
2. 在保留数相同的方案中，最大化保留请求的 priority 总和；
3. 仍并列时，选择按 id 排序后字典序最小的保留列表（即第一个成员身份不同处，包含更小 id 的方案胜出）。

### 2.4 精确求解结构（生产路径）

问题建模为有向网络上的字典序最小费用最大流，每条被保留请求对应一条完整流路：

```
source -> principal窗口 -> 请求in -> 请求out -> route窗口 -> sink
```

* source 到 principal 窗口、route 窗口到 sink 的边容量为该窗口 `limit`，使两侧容量同时约束每个保留请求；
* 每个请求是容量为 1 的 in→out 边，表示不可拆分单位，流量不可能只走一侧，因此强制保留只占一次名额、任何名额都不会被重复预订；
* 三级目标编码为一条整数费用、三个严格不进位的权重带（设去重后请求数为 n）：
  * 计数带 `W1 = 2^n · (n·1e9 + 1)`，每保留一条减 `W1`，保证数量优先；
  * priority 带 `W2 = 2^n`，每保留一条再减 `W2·priority`，保证同数量下 priority 总和最大；
  * 字典序带按 id 升序分配位权 `2^(n-1-i)`，同数量同 priority 下保留更小 id 的方案费用更低。
  各带极差严格小于下一阶单位，带间不会串扰。
* 求解使用带 Johnson 势函数的连续最短路增广（Dijkstra + 位势），初始网络为 DAG 且节点按拓扑序编号，首轮势函数由一次有序扫描得到。复杂度为输入规模的多项式，明确禁止指数子集枚举，也不使用任何外部优化器。

### 2.5 申诉状态处理与隔离

对 `appeals` 中的每个请求 **独立** 处理：

1. 先把该请求本身从图中移除，并把它所在 principal 窗口和 route 窗口的可用容量各减 1，即强制预留它的两个名额；
2. 对剩余网络重新执行同一套三级最优求解；
3. 把强制请求加回保留集合（它不经过任何边，因此只计一次，也不参与费用竞争）。

每份申诉都从同一份原始窗口容量出发重建残量状态，申诉之间没有共享可变状态，不能相互污染；强制保留沿完整共享容量关系（整张流网络的交替路径）寻找替换，而不是只检查该请求的两个直接窗口或在基准集合上简单删一个请求。

因为策略 `limit ≥ 1`，单条被强制请求总能在自己的两个窗口各占一个名额，合法单条申诉永远有解，不会被误报为无解。已在基准保留集合中的申诉，其重求结果与基准完全相同（entered/removed 均为空、两个 loss 均为 0）。

### 2.6 输出

报告外层严格为 `schema_version=2`、`mode="retention"`、`source_sha256`、`result`；`result` 严格包含 `baseline`、`appeals`、`deduplicated_request_count`。

* `baseline` 严格包含 `kept_ids`、`dropped_ids`、`kept_count`、`priority_sum`。保留与丢弃列表完整互补且均按 id 排序；`priority_sum` 只累计保留请求。
* 每项申诉严格包含 `request_id`、`kept_ids`、`dropped_ids`、`kept_count`、`priority_sum`、`entered_ids`、`removed_ids`、`count_loss`、`priority_loss`。
  * `entered_ids` = 本次保留集合 − 基准保留集合，`removed_ids` 反向；两者均按 id 排序。
  * `count_loss = 基准 kept_count − 本次 kept_count ≥ 0`。
  * `priority_loss = 基准 priority_sum − 本次 priority_sum`，是有符号整数：同数量的强制最优不可能超过基准最优（强制可行域是基准可行域的子集），但当强制保留造成数量损失时，剩余请求的 priority 总和可能高于基准，此时 `priority_loss` 为负数，不得截断为 0。
  * 申诉按 `request_id` 排序。

### 2.7 确定性与发布保证

业务结果与输入排列、重复同一规范请求的位置、申诉排列均无关（内部统一按 Python 字符串序规范化）。`source_sha256` 始终反映源文件真实字节，源文件永不被修改。新模式复用受限 JSON 解析（拒绝重复键、非有限数值、未定义字段）、精确微秒时间与原子发布；任一输入错误退出码为 2，且已存在的旧报告保持不变。

## 3. 区间计量证据对账（schema_version=3，`--mode reconcile`）

### 3.1 输入

顶层字段严格为 `timeline`、`max_slot_units`、`meters`、`invoices`、`queries`，缺字段或多字段整份失败。

* `timeline`：2..161 个带显式时区的 ISO 时刻，沿用 instant 语义归一化为 UTC 微秒后须严格递增。设 `n = len(timeline) - 1`，slot `i` 是 `[timeline[i], timeline[i+1])`，`x[i]` 是其中未知的用量。
* `max_slot_units`：1..1,000,000,000,000 的整数，所有 slot 的共同上界；不假定段内均匀速率。
* `meters`：最多 1200 项，每项严格为 `id`、`start`、`end`、`lower`、`upper`，含义为 `lower <= sum(x[start:end]) <= upper`，`lower`/`upper` 为 0..1,000,000,000,000,000 且 `lower <= upper`。
* `invoices`：最多 200 项，每项严格为 `id`、`start`、`end`、`units`，要求 `sum(x[start:end])` 恰为 `units`。
* `queries`：最多 40 项，每项严格为 `id`、`start`、`end`，求该段用量范围。
* 所有区间端点都是 timeline 索引，要求 `0 <= start < end <= n`，不插值。
* `id` 沿用 ident 约束；各数组内部 id 唯一（重复即输入错误），不同种类可以同名。空 `meters`/`invoices`/`queries` 均合法。
* 索引与所有业务整数拒绝 bool 与浮点（`True`/`False` 不等于 1/0）。
* 先完整校验所有输入（含全部数组），再作任何业务判断；不会因为早发现计量矛盾而跳过非法账单或 query。

### 3.2 联合业务契约

未知量是一份同时满足全部证据的整数向量 `x[0..n)`。判断顺序固定：

1. **计量相容**：全部 slot 边与 meter 约束（每条 `lower <= sum <= upper`）须由同一份用量分配满足；否则 `meter_conflict`。
2. **账单批次裁决**：计量可行后，全部 invoice 必须**联合**成立；不是逐张检查数值范围，也不挑选可接受子集，任一联合矛盾即 `invoice_conflict`。
3. **查询极值**：联合可行时，对每个 query 求精确最小/最大区间用量，**均基于全部 meters 加全部 invoices**，不是局部约束。每端都附带一份覆盖全部 `n` 个 slot 的整数分配作为见证；不同 query 及两端允许使用不同分配，每份见证独立满足全部约束（不能把各 slot 或 query 各自的最值拼成一条见证）。

### 3.3 精确求解结构

以累计量 `P[0] = 0`、`P[j] = sum(x[:j])` 表示，每条证据化为差分约束 `P[v] - P[u] <= w`：

* meter `upper` 边 `start -> end` 权 `upper`，`lower` 边 `end -> start` 权 `-lower`；
* invoice 同构，上下边权为 `units` 与 `-units`；
* slot `i` 的 `upper` 边 `i -> i+1` 权 `max_slot_units`，`lower` 边 `i+1 -> i` 权 `0`。

约束系统可行当且仅当有向图无负权环。实现先用 Bellman-Ford 在 slot+meter 子图上判定计量相容；把 invoice 边加入同一张图后再判一次，故任何新出现的负环必含 invoice 边。可行时用全点最短路（Floyd–Warshall，精确整数）得到：query `[start,end)` 的最大值 `dist(start,end)`，最小值 `-dist(end,start)`。同一距离向量 `dist(source, ·)` 本身满足所有约束边，其逐 slot 增量即为一份达到该极值的完整分配（`dist(source,v) <= dist(source,u)+w`）。全部算术保持精确整数，不使用浮点近似、逐单位枚举或外部求解器。

### 3.4 输出与可核验证书

报告外层严格为 `schema_version=3`、`mode="reconcile"`、`source_sha256`、`result`；`result` 严格为 `status`、`ranges`、`certificate`。

* `status` ∈ `feasible` / `meter_conflict` / `invoice_conflict`；两个 conflict 是合法业务报告，命令成功退出 0。
* `feasible`：`certificate` 为空数组；`ranges` 按 query id 排序，每项严格为 `id`、`min_units`、`max_units`、`min_allocation`、`max_allocation`。每份 allocation 是长度 `n` 的整数数组，依次给出全部 slot 用量；即使最小等于最大，两份完整数组也给出。
* 两个 conflict：`ranges` 为空数组，`certificate` 非空。
* `certificate` 是按行走顺序排列的原始约束边数组，每边严格含 `kind`、`ref`、`side`、`from`、`to`、`weight`：`kind` 为 `meter`/`invoice`/`slot`，`ref` 为原 id（slot 用 `i` 的十进制字符串），`side` 为 `upper`/`lower`，其余值须与上述原始边完全相符。连续边首尾相接，末边终点等于首边起点，所有 `weight` 之和严格小于 0，边数最多 `n+1`。
* 证书不含闭包捷径边，也不含虚构超级源边：Bellman-Ford 以全零距离起步（等价于虚拟源但不物化其边），末轮仍可松弛的节点沿前驱链回溯即得原始边构成的简单负环。
* `meter_conflict` 证书只引用 meter 与 slot 边；`invoice_conflict` 证书可引用全部来源并至少含一条 invoice 边（因计量子图已被证明无负环）。
* 证书与 allocation 都是可独立复算的真实见证，不是不可复现的错误字符串。

### 3.5 确定性与发布保证

结果与源数组排列无关（内部按 id 排序）。沿用原输入字节 SHA256、同路径拒绝、输出符号链接拒绝及原子写入；格式/字段/范围等输入错误退出 2 且保留原目标文件。既有 `audit`（schema_version=1）与 `retention`（schema_version=2）入口及结果保持不变，本模式仅复用 instant 的精确 UTC 时间与受限 JSON/原子发布。

## 4. 本地持久预付额度台账（`api-credit-ledger` / `python -m api_quota_audit.ledger`）

新增的独立命令，把客户购买的预付 units 落成一个 SQLite 文件中的持久台账。它复用现有受限 JSON 读取（8MB、拒绝重复键/非有限数值）、`ident` 与精确 UTC 微秒 `instant`，但自身不读取任何 audit/retention/reconcile 输入，也不连外部服务。三条子命令：

```sh
api-credit-ledger init   --db PATH
api-credit-ledger apply  --db PATH --source JSON
api-credit-ledger report --db PATH --output JSON
```

* `init` 创建一个空数据库，绝不覆盖已存在文件（含符号链接）。成功时 stdout 严格为 `{"schema_version":1}` 的 JSON。
* `apply` 在一个事务里执行一个操作批次。成功时 stdout 严格为 `{"receipts":[...]}` 的 JSON 对象，数组按输入 `operations` 顺序逐项给出凭据（重复项也逐项给出）。
* `report` 导出一份已提交状态的只读快照；成功时 stdout 与写入 `--output` 的文件逐字节相同。
* `apply` / `report` 都不会隐式初始化缺失数据库，遇到未知 schema 一律拒绝；数据库路径为符号链接时拒绝。
* 业务/输入错误退出码 2，只写 stderr，不打印成功 JSON。调用者可凭 `op_id` 重试，无需依赖跨数据库提交或 stdout 的恰好一次投递。

### 4.1 存储结构

单个 SQLite 文件、六张业务表，全部路径走同一组 `PRAGMA foreign_keys`、`BEGIN IMMEDIATE` 事务：

* `ledger_meta`：`schema_version='1'` 与 `clock_us`（初始为 NULL，之后为非负整数微秒）。
* `lots`：每个批次一行，含 `lot_id`、`principal`、`granted`、`expires_at_us`、`status`（`active`/`expired`/`revoked`）、`available`、`retired`。`held`/`consumed` 由下述分派表在报告时汇总，不冗余存储。
* `holds`：每个预占一行，含 `hold_id`、`principal`、`units`、`status`（`active`/`committed`/`cancelled`/`invalidated`）、`used_units`（仅 committed 非 NULL）。
* `hold_allocations`：hold 的冻结原分配，`(hold_id, position)` 主键，按 position 保序，记录 `lot_id`、`units`。
* `consumed_allocations`：committed hold 的实际核销来源，结构与上表相同，同样按原分配顺序的前缀。
* `ops`：每个成功操作一行，`op_id` 主键 + 规范化身份 `canonical` + 冻结的 `receipt` JSON。重试直接读取该行。

选择直接维护余额而不是追加分录：到期/撤销引起的跨批次失效需要在一个事务里同步修改多个 lot 的 `available`/`retired` 与 hold 状态，直接更新更便于在同一处断言守恒；`ops` 表天然承担了“原凭据冻结”的追加式记录职责。批次守恒在每个 lot 上恒为

```
granted == available + held + consumed + retired
```

其中 `held` 是所有 active hold 在其上的冻结分配之和，`consumed` 是所有 committed hold 实际核销之和；`retired` 只累计到期或撤销时该 lot 尚未消耗的余额，已核销部分永不退回。

### 4.2 操作与校验

`apply` 顶层严格为 `operations`（最多 500 项，可空）。每项都有 `op_id`、`kind`、`at`，并按 kind 严格只允许这些额外字段（任何额外字段都失败，即便 `op_id` 已存在）：

| kind | 额外字段 |
| --- | --- |
| `grant` | `lot_id`、`principal`、`units`、`expires_at` |
| `reserve` | `hold_id`、`principal`、`units` |
| `commit` | `hold_id`、`used_units` |
| `cancel` | `hold_id` |
| `revoke` | `lot_id` |
| `advance` | 无 |

* `op_id`、`lot_id`、`hold_id`、`principal` 沿用 `ident`；`op_id` 全库唯一，`lot_id` 与 `hold_id` 各自全库唯一且终态后不复用。
* `at`/`expires_at` 用 `instant` 归一化为精确 UTC 微秒；无时区或非法时间失败。`expires_at > at`。
* `units` 为 1..10^12 的整数；`used_units` 为 0..该 hold 预占量 的整数。两者都拒绝 bool 与浮点。
* `grant` 要求 `expires_at > at`；`reserve` 只能用同 principal 且尚未失效的 active lot，可用量不足则整批失败，不部分成功。
* 未知 id、重复业务 id、对非 active hold 执行 `commit`/`cancel`、对非 active lot 执行 `revoke` 均失败。

### 4.3 逻辑时钟与生命周期次序

每个**新**操作先推进不倒退的逻辑时钟，再处理全部到期批次，最后执行业务：

1. **时钟**：`clock_us` 初始为 NULL，首个新操作可接受任意合法时间；之后要求 `at_us >= clock_us`，同刻允许多个新操作并按数组次序处理。不读取墙上时钟。
2. **到期**：所有 `expires_at_us <= at_us` 的 active lot 在本轮**同时**处理。先令任何在原分配中引用它们的 active hold 整笔 `invalidated` 并释放其**全部**来源（包括仍有效的其他 lot），再把这些 lot 全部剩余未消耗额度转为 `retired`、状态置 `expired`。因此失效 hold 在活 lot 上释放的额度立即可被后续 `reserve` 使用，且失效 hold 不会被迁移到其他来源。
3. **业务**：按 kind 执行。`reserve` 取有效 lot 可用量，按 `(expires_at_us, lot_id)` 升序依次分配，允许跨批次；hold 冻结原分配，不随后续重排。`commit` 沿原分配顺序取前 `used_units` 为 consumed、释放余量，`used_units` 可为 0；`cancel` 全释放；`revoke` 在同轮到期处理后要求目标 lot 仍 active，先令依赖它的 active hold 整笔失效并释放各来源，再把目标未消耗量转 `retired`、状态置 `revoked`。`invalidated`/`cancelled`/`committed` 都是不可重新激活的终态。

因到期处理而使本次 `commit` 失效时，本批仍须失败并整批回滚（包括刚处理的到期与时钟推进）；要推进台账需另用 `advance` 成功提交。

`apply` 批次内所有新状态、来源分配、逻辑时钟和操作凭据在一个 SQLite 事务内提交；任一步失败则整批回滚。完全相同的规范操作重试返回原凭据：**先识别重试，再检查时钟**，因此不推进时间、不产生业务效果，原 `at` 早于当前时钟亦成功；同一批内重复已成功的 op 同样如此。新批次可混合旧凭据重试与新操作，时钟只被新操作推进。

### 4.4 规范操作身份

规范身份 = kind + 全部业务字段，时间以 UTC 微秒比较。字段顺序不同、等价的时区写法（如 `+01:00` 与 `Z`）都归一为同一身份。`op_id` 已存在且规范内容不同则失败。

### 4.5 凭据与报告

每个 receipt 严格为 `op_id`、`kind`、`at_us`、`invalidated_hold_ids`、`allocation`、`consumed_allocation`：

* `invalidated_hold_ids` 记录该新操作“到期处理 + revoke”导致的全部失效 hold，按 id 去重排序；
* `allocation` 仅 `reserve` 为其冻结原分配，否则空数组；
* `consumed_allocation` 仅 `commit` 为实际核销来源，否则空数组；
* 分配项严格为 `lot_id`、`units`，只列正量并保持原分配顺序。凭据冻结保存并随数据库持久化，重试时不按当前余额重算。

`report` 外层严格为 `schema_version=1`、`clock_us`、`lots`、`holds`：

* `lots` 按 `lot_id` 排序，每项严格为 `lot_id`、`principal`、`expires_at_us`、`status`、`granted`、`available`、`held`、`consumed`、`retired`；所有量为非负整数且满足 4.1 的守恒。
* `holds` 按 `hold_id` 排序，每项严格为 `hold_id`、`principal`、`units`、`status`、`allocation`、`used_units`、`consumed_allocation`；`allocation` 始终是冻结原分配、终态也保留；`used_units` 只有 `committed` 为整数，其余为 `null`；`consumed_allocation` 仅 `committed` 可非空。

报告是同一已提交状态的只读一致快照（`BEGIN` 事务内读取），不因查询推进时钟。`report` 复用已有原子写与符号链接拒绝，并拒绝 `output` 与 db 指向同一文件（包括硬链接、`..`、符号链接等路径别名），绝不把 DB 写成报告。DB 提交与报告文件之间不做跨资源原子提交。

### 4.6 失败、重开与确定性

任一新操作失败时，整批此前的新 grant、hold 变化、到期退役、时钟及凭据均不留下，此前已存在的凭据不变。成功提交后关闭并重开数据库必须重现完全相同状态。测试逐批直接验算每个 lot 的守恒与每个 hold 的来源，并覆盖失败后持久化文件状态、无效输入/未知 schema 不损坏旧库或旧报告。仅要求单机本地 SQLite 事务，不要求网络服务、多机分布式协议或断电文件系统模拟。既有三种模式的输入与结果保持不变。

