# API 配额审计数据契约

本工具有五种离线模式，均只读取本地 JSON、只写本地报告，不调用任何外部服务。此外提供两个独立的本地 SQLite 命令：持久化预付额度台账（见第 4 节）与可追溯用量事实仓（见第 5 节）。两者都只使用 Python 标准库 `sqlite3` 与本地文件，不连接任何外部服务，也不改变上述五种模式的输入与结果；事实仓与台账互不读取或改写对方的存储。

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


## 5. 可追溯用量事实仓（`api-usage-warehouse` / `python -m api_quota_audit.warehouse`）

新增的独立命令，把「事件正常/撤回版本」与「配额策略正常/撤回版本」按知识序号追加式保存在单个 SQLite 文件中，使运营能够重现「当时已知的配额报告」。它复用受限 JSON 读取（8MB、拒绝重复键/非有限数值）、`ident` 与精确 UTC 微秒 `instant`，不读取、不改变 audit/retention/reconcile 输入，也不读取或改变 grant/hold/核销与预付余额。三条子命令：

```sh
api-usage-warehouse create --db PATH
api-usage-warehouse ingest --db PATH --source JSON
api-usage-warehouse query  --db PATH --max-seq N --event-before ISO --output JSON
```

对应 Python 函数为 `warehouse.create_database`、`warehouse.ingest_batch`、`warehouse.query_warehouse`（另可直接调用 `warehouse.build_report` 获得报告对象而不写文件）。

* `create` 只创建新库，已存在（含符号链接、悬空符号链接）即拒绝；成功时 stdout 严格为 `{"schema_version":1}` 的 JSON。
* `ingest` 在一个事务里提交一个批次，成功时 stdout 严格为批次 receipt 对象。
* `query` 从一个只读一致快照计算报告；成功时 stdout 与写入 `--output` 的文件逐字节相同。
* `ingest` / `query` 都不隐式建库（库不存在即失败、不产生文件），遇到未知 schema 一律拒绝；数据库路径为符号链接时拒绝。
* 业务/输入错误退出码 2，只写 stderr，不打印成功 JSON；失败事务回滚，不留下新序号、版本或 receipt。`query` 非法参数（`max_seq` 越界、非法时间字符串）或文件错误同样退出 2，旧输出文件保持不变。
* 只要求单机本地 SQLite；DB 提交与报告文件之间不做跨资源原子提交。

### 5.1 入仓输入与规范身份

顶层严格为 `batch_id`、`event_revisions`、`policy_revisions`，缺字段或多字段整份失败。`batch_id` 及全部业务 id/scope 沿用 `ident`；两个数组各最多 2000 行、可空。`event_id` 与 `policy_id` 各自跨批全库唯一身份（主键分别为 `(event_id, revision)` 与 `(policy_id, revision)`），两个种类可以同名。

* 事件正常版本严格含 `event_id`、`revision`、`deleted=false`、`principal`、`route`、`at`、`units`；撤回版本严格含 `event_id`、`revision`、`deleted=true`。`deleted` 必须是 JSON 布尔。`units` 为 1..1,000,000,000 的整数，拒绝 bool/浮点。
* 策略正常版本严格含 `policy_id`、`revision`、`deleted=false`、`principal`、`route`、`valid_from`、`valid_to`、`window_seconds`、`limit`；撤回版本严格含 `policy_id`、`revision`、`deleted=true`。`valid_to` 可为 `null` 表示正无穷，否则要求 `valid_from < valid_to`（秒级相等也不行）。`window_seconds` 为 1..31,536,000 的整数，`limit` 为 1..10^12 的整数，均拒绝 bool/浮点。
* 时间统一沿用 `instant` 语义：显式时区、精确到微秒、等价偏移视为同一 UTC 时刻，不截秒判等。批次不读取系统时钟，也不禁止更早业务时刻。
* `revision` 是 1 开始的正整数。身份首次出现必须是非 `deleted` 的 `revision=1`；之后严格连续（同批可有同身份多个连续 revision，也可由后续批次接续），不能跳号或覆盖；同一批次内重复同一 `(id, revision)` 即错误；已存在 revision 即使内容相同也不能由另一个 `batch_id` 再次写入。撤回已撤回身份错误（重复撤回），恢复必须用撤回之后的下一完整非 `deleted` 版本。
* 两个数组的排列不影响结果：解析后按各自的 `(id, revision)` 排序。规范批次身份 = `batch_id` + 按 `(id, revision)` 排序的两个版本数组 + 全部严格字段与值（时间转 UTC 微秒）；同 `batch_id` 内容冲突错误。

### 5.2 序号、receipt 与幂等重试

每个成功的新 `batch_id` 占一个连续 `seq`（初始空库无批次；首批为 1，其后 +1），空批次同样占号；同批所有 revision 归属同一 `seq`。receipt 严格含 `batch_id`、`seq`、`event_revision_count`、`policy_revision_count`，两个计数是该次成功写入的行数（含撤回版本），随数据库持久保存。

完全相同规范内容的重试直接返回保存的原 receipt：不增 `seq`、不重验当前头（因此后续已提交的新 revision 不影响重试）。`batch_id` 已存在但规范内容不同则失败。

### 5.3 最终状态校验

新批次全部版本、序号与 receipt 写入后，**以批次结束状态**（而非临时中间状态）做一次校验，失败则整批回滚：

1. 以新 `seq` 内每个身份的最高 revision 为头，排除 `deleted` 后，同 `(principal, route)` 所有策略的生效区间 `[valid_from, valid_to)` 不得重叠；端点相等（一个结束即另一个开始）允许；`valid_to=null` 视为到正无穷。
2. 每条有效事件（当前全部有效事件，包括未在本批出现的身份）必须在其 `at` 匹配恰好一条同 `(principal, route)` 策略，否则整批失败。

因此策略可以先于事件入仓；不要求无事件的 scope 有策略；策略撤回、更改 scope/区间与事件移位可以在同一批联动，校验只针对最终状态，不校验同批临时中间状态。

### 5.4 双截止历史查询

`max_seq` 为 0..当前最大 `seq` 的整数（类型必须是 int，越界错误）；`event_before` 沿用 `instant` 归一化为 UTC 微秒，严格取 `at < event_before`。选版顺序固定：

1. **知识截止**：对每个事件/策略身份，先只考虑 `seq <= max_seq` 的版本并取最高 `revision`；若该头是 `deleted`，该身份在此快照中不存在（不得回退到更旧的版本）。
2. **事件时间截止**：事件头版本选定之后才比较其 `at` 与 `event_before`。即使新头越过了截止，也绝不回退挑旧版本。
3. 策略头版本选定后，按事件头的 `at` 匹配其历史生效段（`valid_from <= at < valid_to`），得到唯一归属。

`seq=0` 结果为空。序列号上限、两类版本集合必须在同一只读事务（同一 SQLite 读快照）内读取。

窗口仍由 UTC epoch 锚定 `[start,end)`，`start = floor(at_us / (window_seconds*1e6)) * window_seconds`（负 epoch 同样地板除），策略生效时刻不改变 epoch 锚点。每条筛入事件只贡献给唯一选定的 `(policy_id, revision)`；输出窗口按 `principal`、`route`、`policy_id`、`policy_revision`、`window_start_us` 区分，因此同一 epoch 窗口被两个策略版本分割时各自使用自己的完整 `limit`，不合并、不按时长折算；无事件不输出窗口。聚合数值（`used`、`over_by`、`exceeded`）与 UTC 字符串格式复用 `model.analyze` 抽取出的共享聚合函数（`breach`/`utc_text`/`window_start`），保留负 epoch 地板除、微秒边界和超限定义。

报告严格含 `schema_version=1`、`max_seq`、`event_before_us`、`windows`、`window_count`、`exceeded_count`、`selected_event_count`。每个 window 严格含 `principal`、`route`、`policy_id`、`policy_revision`、`window_start_utc`、`window_end_utc`、`limit`、`used`、`over_by`、`exceeded`、`event_versions`；`event_versions` 为该窗口全部事件头的 `{event_id, revision}`，按 `event_id` 排序。`windows` 按上述区分键排序。报告不含易变生成时间，因此在追加后续 `seq` 后，用同一双截止重查的业务 JSON 完全一致。

### 5.5 历史存储/投影方案与隔离性

选择**查询时从不可变版本重建快照**，而不是为每个提交序号物化归属与窗口投影：

* 两张版本表只做追加插入（`event_versions`、`policy_versions`），`(id, revision)` 是物理主键，`seq` 记录引入该版本的知识序号；`batches` 表按 `seq` 自增保存规范身份与冻结 receipt。任何提交都不会 `UPDATE`/`DELETE` 既有版本行。
* 查询用 `revision = (SELECT MAX(revision) ... WHERE id = ? AND seq <= max_seq)` 在每个身份内选头，`seq <= max_seq` 条件同时进入外层与子查询，保证「先按知识截止选版本」；`deleted` 头随后被丢弃，早于时间截止的判断发生在其后。
* 归属与聚合完全由所选版本在内存中重新计算，不读取任何「当前」状态或可变缓存。旧 `max_seq` 的查询只可能看到 `seq <= max_seq` 的行，因此后来的策略更正、事件移位或撤回不可能污染旧知识快照；同一 epoch 窗口的窗口键含 `policy_revision`，不同策略版本天然隔离（各自的 `limit` 独立，不合并）。

该方案在查询时不依赖批内中间状态，也不会因跨批更正而失效；代价是查询需做一次全版本扫描（单机规模、每类最多 2000 行/批，可接受）。物化投影需要为每个新 `seq` 重写所有后续快照，且在迟到更正/撤回时更容易引入旧快照污染，故未采用。

### 5.6 失败、重开与发布保护

任一格式、revision、覆盖/重叠或事件无唯一策略错误，整批（两类版本、知识序号、批次凭据）在一个 SQLite 事务内回滚，无新 `seq`/版本/receipt，旧知识快照仍可读取。关库重开后可查询与重试所有已成功 `seq` 的 receipt。`query` 沿用已有原子写报告与输出符号链接拒绝，并拒绝 `output` 与 DB 指向同一文件（包括硬链接、`..`、符号链接等路径别名）；不得修改 DB（逻辑业务数据与序号保证不变，不承诺物理 DB 字节不变）。

### 5.7 验收复现

* `seq1`：`P1` 覆盖 A scope（`/x`），`E1` 在 `00:30`；`seq2` 把 E 的 `revision=2` 移到 B scope（`/y`）的 `01:30`，同批加入 B 策略。查询 `(seq1, 02:00)` 见 E1/P1；查询 `(seq2, 01:00)` 事件为空（不回退到 E1 旧版本）；查询 `(seq2, 02:00)` 见 E2/B。
* 把 B 策略改为 `01:31` 开始会产生覆盖缺口 → 整批失败，不出现 `seq2`；此前全部可见历史与 receipt 在错误前后逐项相等（测试直接比对 `batches`/版本行与同双截止报告，不使用被测查询生成期望）。
* 仅修订策略阈值/窗口长度而事件未改：新 `seq` 报表重归属与重算（`policy_revision`、窗口锚点、`limit`、超限随之改变），旧 `seq` 报告逐字节不变。
* 测试覆盖：撤回后恢复、策略撤回与事件撤回同批、同身份同批多版本、相邻生效区间、一个 epoch 窗口内换策略（不合并限额）、负 epoch 及微秒边界、时区等价与数组乱序批次重试、已用 revision/冲突 batch、空批次、源 JSON 及输出保护、关库重开；并沿用原业务测试验证共享聚合未回归。


## 6. API 共享阶梯计价与成本中心费用分摊（schema_version=4，`--mode chargeback`）

新增离线模式，回答「同一接口窗口内多个成本中心的共享用量如何按供应商阶梯计价，并把整数分同时对齐到各中心已批准金额与人工锁定的格」。它复用 `ident`、精确 UTC 微秒 `instant`、负 epoch 地板窗口（`model.window_start`）以及受限 JSON/原子发布，但**不读取也不写入**额度台账、事实仓或旧的 audit/retention/reconcile 结构，不选择保留请求，也不反推未知用量。Python 入口为 `chargeback.chargeback_analyze(data)`；命令为 `api-quota-audit --mode chargeback` / `python -m api_quota_audit.cli`。

### 6.1 输入

顶层字段严格为 `tariffs`、`centers`、`events`、`locks`，四者均为数组，缺字段或多字段整份失败。全部 id、principal、route、center 沿用 `ident`；**所有整数拒绝 bool 与浮点**。实现先完整校验四个数组（含全部金额、tiers、事件与 locks），之后才作可分配性判断；不会因为金额不平或锁定矛盾而跳过后续非法输入。

* `tariffs`：最多 40 项，每项严格为 `id`、`principal`、`route`、`window_seconds`、`tiers`。`id` 唯一，`(principal, route)` 唯一。`window_seconds` 为 1..31,536,000 的整数。`tiers` 为 1..8 项**有序边际阶梯**，每项严格为 `upto`、`rate_num`、`rate_den`：`rate_num` 为 0..10^9、`rate_den` 为 1..10^6 的整数（单位「分/用量单位」）；`upto` 为正整数累计用量上界，有限值最大 10^15 且严格递增，只有末项且必须末项为 `null`（无穷）。第一阶从累计 0 开始；不要求单价递减，0 价合法。
* `centers`：最多 50 项，每项严格为 `id`、`required_cents`；`id` 唯一，`required_cents` 为 0..10^18 的整数。它是本次全部池合计后的**指定中心金额**（不是预算上限）。无用量中心也保留且只能归集 0 分。空 `centers` 仅在无 `events` 且无 `locks` 时合法。
* `events`：最多 10,000 项，每项严格为 `id`、`principal`、`route`、`at`、`units`、`center`；`units` 为 1..10^9 的整数，`at` 沿用 `instant`（显式时区、精确微秒、等价偏移视为同一 UTC 时刻），`center` 必须在 `centers` 中，每条事件必须找到唯一 `(principal, route)` scope tariff。相同 `id` 且**全部规范字段**相同视为一条（时区等价视为相同）；同 id 任一字段不同（含 `center`）整份失败。重复规范事件不重复计费。
* `locks`：最多 2,000 项，每项严格为 `tariff_id`、`window_start`、`center`、`cents`；`cents` 为 0..10^18 的整数。`window_start` 为显式时区 ISO，规范到微秒后必须**恰为**所指 tariff 的一个窗口起点，且所指 `(tariff, window, center)` 非空格真实存在。未知 tariff/center/池/格以及同格重复 lock 属输入错误（重复金额相同也拒绝）。结构合法但 `cents` 不在该格 floor/ceil 内是合法的 `unallocatable` 业务结果，不报输入错误。

### 6.2 窗口、价格汇总与精确计价

每条事件按所属 `tariff.window_seconds` 落入 UTC epoch 锚定的半开窗口 `[start,end)`：`start = floor(at_us / (window_seconds*1e6)) * window_seconds`（负 epoch 同用地板除）。池键为 `(tariff_id, window_start_us)`。同池累计 `U`，每个非空 `(tariff_id, window_start_us, center)` 格累计 `u`；无事件的 tariff 不产生池，无用量的池中心组合不生成格。规范事件归并后，池数最多 80、非空格最多 2000，超限即输入错误。

**共享阶梯先对池总量计价，再分摊到中心**：

```
C = Σ_k overlap_k · rate_num_k / rate_den_k        # 与 [0,U) 的区间重叠
a = C · u / U                                        # 该格精确费用
R = floor(C + 1/2)                                  # 恰半分向上，非银行家舍入
```

即折扣阶梯只按池内全部已知用量的总量应用一次，而不是把同一阶梯分别套到每个中心的用量。`C`、`a` 全程为精确有理数，输出一律最简非负 `{numerator, denominator}`（`denominator > 0`，0 统一为 `0/1`）。即使全池单价为 0，已发生用量仍产生 0 费用格。不输出逐事件价格，也不按事件先舍入。

### 6.3 双边受控分币的联合求解

对每个非空格令 `z ∈ {floor(a), ceil(a)}`（整数 `a` 只有一个选择）。要求：

* 每池 `Σ z = R`（行约束）；
* 每中心 `Σ z = required_cents`（列约束）；
* 每个 lock 的格金额等于 `cents`。

记 `z = floor(a) + x`，`x ∈ {0,1}`。行/列约束与 lock 因此化为**二部 0-1 可行流**：行（池）→ 格的容量一单位边 → 列（中心）。行列合计不等、格界被锁死或出现任何不可达的列/行时，整张矩阵无解。`sum(required_cents) != sum(R)` 直接不可分配。

**主目标**为跨全部非空格的绝对舍入误差 `E = Σ |z - a|`。因为 `|z-a|` 相对 `floor` 的增量是 `1 - 2·frac(a)`（可为负），把「取 ceil」看作一条费用为该精确有理数的容量一边，误差最小化即**最小费用流**。

**次目标**在 `E` 相同的矩阵中，取按 `(tariff_id, window_start_us, center)` 排序的完整 `z` 向量的字典序最小者。实现给每个自由格分配一个位权（排序靠前者位更高），作为与主费用**严格分层**的第二费用分量，按 `(主误差, 位权)` 的字典序比较；主误差优先，位权只在主误差相等时决胜。因此不需要逐格枚举，也不必按中心 id 挑选尾差。

实现自带一个精确有理数势函数的最短路连续增广最小费用流（Dijkstra + Johnson 位势），不 import 旧 retention 代码。它与旧 retention 流算法结构相近（邻接表 + 二分堆 + 连续增广），但旧算法有两点假设在本模式不成立，实现据此改写：

* **单位增广假设**：旧算法每轮沿残量路径增广恰好 1 单位，因为每条请求边容量为 1。本模式的源→行、列→sink 边容量是待补的整数分差，可能大于 1；虽然每个自由格边容量为 1 使增益自然受限，实现仍按路径上的最小残量 `push` 增广，而不是硬编码 `-= 1`，从而在自由格不足时也能正确报告不可达而非死循环。
* **初始势假设**：旧算法假定初始网络为 DAG 且只有非负费用，用一次有序扫描即可。本模式的格费用 `1 - 2·frac(a)` 可为负，且「待补差额为 0」的行节点从真实源不可达却仍有出边，单次拓扑扫描会留下负 reduced cost。因此势函数改为从全零距离出发、对全部节点松弛至收敛的 Bellman-Ford（等价于虚拟零费用超级源，但不物化其边），得到对所有残量边 reduced cost ≥ 0 的 Johnson 势。

势与费用全程为 `Fraction`（或「有理数 + 整数位权」的字典序对），不使用浮点 epsilon、逐分金额枚举或外部求解器。求解后必须逐行、逐列、逐 lock 复核；任一行列/格界/lock 无法同时满足即返回 `unallocatable`，绝不逐池独立舍入后悄悄改列合计或转移不存在的用量。

### 6.4 输出

外层严格为 `schema_version=4`、`mode="chargeback"`、`source_sha256`、`result`。`result` 严格为 `status`、`pools`、`centers`、`cells`、`total_invoice_cents`、`rounding_error`、`deduplicated_event_count`；`status` ∈ `balanced` / `unallocatable`。

* `pools` 按 `(tariff_id, window_start_us)` 排序，每项严格为 `tariff_id`、`principal`、`route`、`window_start_us`、`window_end_us`、`units`、`exact_cost`、`invoice_cents`；时间为精确 UTC 微秒整数，`exact_cost` 是 `C`，`invoice_cents` 是 `R`。`total_invoice_cents = Σ R`。
* `centers` 按 id 排序，每项严格为 `id`、`required_cents`、`actual_cents`。
* `cells` 列全部非空格，按 `(tariff_id, window_start_us, center)` 排序，每项严格为 `tariff_id`、`window_start_us`、`center`、`units`、`exact_cost`、`cents`、`locked`；`exact_cost` 是 `a`，`locked` 是该格是否有 lock 的布尔值。
* `balanced`：全部 `cents`/`actual_cents` 为非负整数，`rounding_error` 为最简分数 `E`。
* `unallocatable`：仍返回完整且相同的精确计价基础、池金额与指定中心金额，但所有 `cents`/`actual_cents` 及 `rounding_error` 均为 `null`，不留部分分配。

全空输入是 `balanced`，三个数组为空，总分与事件数为 0，误差 `0/1`；有零目标的空中心时保留该中心 `actual_cents=0`。合法 `unallocatable` 仍写报告并退出 0；输入/格式/文件错误退出 2、写 stderr，不输出成功 JSON 且旧报告保留。stdout 是保存文件的同一 JSON。源路径及其硬链接/路径别名与目标同文件必须拒绝，输出符号链接拒绝；保留受限 JSON 读取及原子写入。输入排列不改变业务 `result`（tiers 是有序数据，不能乱序）；重复同一规范事件也不改变 `result`，`source_sha256` 仍反映实际源字节。

### 6.5 验收与测试要点

* 两池各产生 1 分、每池 A/B 各占一半、A/B 各要求 1 分：逐池给 A 补尾差虽局部正确却破坏中心列，合法且字典序最小矩阵为 `[0,1,1,0]`；锁定首格为 1 后联动为 `[1,0,0,1]`；改变一池价格后重算费用、格界和联合可分性。
* 测试规模足够小时**独立枚举**所有格 floor/ceil 组合，用独立实现的约束验算与精确误差核全局结果；绝不调用被测求解器生成期望。覆盖共享折扣按池总量而非各中心单算、阶梯临界点与 0 价、精确半分、整数格不能被加 1、同格重复事件与 center 冲突、多时区与负 epoch/微秒窗口边界、两套合计相同但支持关系无解的稀疏矩阵、锁定可行与锁定不可行、无用量中心、空输入、主误差不同与并列的字典序决胜、不同分母下不得浮点近似。
* 另提供有多个池/中心及跨池锁定的可运行较大用例，验证生产路径无需指数枚举或逐分金额展开；并实跑 CLI 的成功、业务无解与非法输入保留旧报告，沿用旧业务测试检查回归。

## 7. 乱序事件的连续会话变更流（schema_version=5，`--mode sessions`）

新增离线模式，把乱序到达的 API 调用事件按「相邻间隔」维护成最大连续会话，并随每一步操作输出旧身份撤销、开放会话快照 upsert 与不可撤销的封账增量。它复用 `ident`、精确 UTC 微秒 `instant`（含等价时区、负 epoch 与微秒比较）以及受限 JSON/原子发布，但**不套用任何 epoch 固定窗口聚合**，也不读取或写入额度台账、事实仓或其他模式的结构。Python 入口为 `sessions.SessionAccumulator(policies, allowed_lateness_seconds)`（逐步 `apply(operation)` / `snapshot()`）与 `sessions.sessions_analyze(data)`；命令为 `api-quota-audit --mode sessions --source JSON --output JSON` 与 `python -m api_quota_audit.cli` 同等调用。状态只在本次内存对象生命周期内有效：无数据库、无断点恢复、无后台服务。

### 7.1 输入

顶层字段严格为 `policies`、`allowed_lateness_seconds`、`operations`，缺字段或多字段整份失败。

* `policies`：最多 100 项，每项严格为 `principal`、`route`、`gap_seconds`；scope `(principal, route)` 唯一，重复即失败。`gap_seconds` 为 1..31,536,000 的整数（拒绝 bool/浮点），同一 scope 的所有会话共用该间隔。
* `allowed_lateness_seconds`：0..31,536,000 的整数（拒绝 bool/浮点）。
* `operations`：最多 2000 项，可空。每项按 `kind` 严格区分：
  * `event`：严格为 `kind`、`id`、`principal`、`route`、`at`、`units`；`units` 为 1..1,000,000,000 的整数，`at` 沿用 instant 归一化为精确 UTC 微秒。每条 event 都必须匹配一条 policy scope，**即使它将被判迟到或是重复重试，也先完整校验字段与 scope**。
  * `retract`：严格为 `kind`、`event_id`。
  * `watermark`：严格为 `kind`、`at`。
* 全部 id/scope 沿用 ident；事件 id 是**全局唯一身份**，不按 scope 分开。

### 7.2 会话定义与实际维护方法

会话只包含「已接纳且尚未撤回、且尚未封账」的成员。对每个 scope，把成员按 `(at_us, id)` 排序，相邻两条**间隔严格小于 `gap_us = gap_seconds*1e6`** 即属于同一个最大连通组件；间隔恰等于 `gap_us` 时分段。因此它不是固定桶，也不是只把新事件与最早成员比较：多个事件同一微秒合法，删除中间桥接成员可把一个组件拆成多个，删除最后一个成员则会话消失。

实现不维护增量组件索引，而是在每个合法 `apply` 内执行同一个状态变换：

1. **先校验、暂存本步效果**：事件接纳/判迟到/识别重试、撤回、watermark 校验都先在不修改既有状态的前提下完成；任何输入错误在此抛出 `ValueError`，登记、watermark、step 与两张表全部保持此前状态，调用者可继续喂合法操作。
2. **从暂存后的开放成员全量重建最大组件**：用当前全部有效开放事件（排除已封账、迟到与已撤回身份，绝不把旧已封事件放回开放构造）按 scope 重新分组。会话身份 `session_id` 取当前成员中 Python 字符串序最小的 event id；`first_us`/`last_us` 为成员最早/最晚微秒，`closes_at_us = last_us + gap_us`，`units` 为成员 units 的精确整数和，`event_ids` 按字符串序排列。身份更名可由乱序插入、桥接或删除最小 id 触发，成员、用量与边界始终来自该身份当前所在的组件。
3. **在同一状态上封账**：对重建后的组件，凡 `closes_at_us <= F` 即封账并离开开放集合；`F = None` 时不封。

全量重建保证合并、拆分、身份更名与封账决定共享同一份当前状态，不存在索引与事实漂移；输入规模上限 2000 步，每步线性重建成本可接受。

### 7.3 Watermark、迟到边界与不可撤销封账

watermark 起始为 `null`；第一条可取任意合法时刻，之后归一化值不得倒退（相等合法），系统墙钟不参与业务。定义迟到边界

```
F = watermark_us - allowed_lateness_seconds * 1_000_000
```

没有 watermark 时 `F = null`，任何新事件都不因时间迟到。新 event 的 `at_us < F` 判为 `late`：只做身份登记、永不贡献用量、永不进入会话；`at_us >= F`（含恰等 F）判 `accepted` 并进入开放会话。

封账是不可撤销的终态：

* 封账把完整会话快照追加到已封表，成员终态化并离开开放集合；之后不得撤销、更新或重新使用该 `session_id`。
* 由于新事件满足 `at_us >= F` 而连接条件为严格小于 gap，新事件不可能再与已封成员相连；而能在全量历史上连接已封早段的事件，其时刻必然 `at_us < F` 而被判迟到，因此**先封独立早段、后到历史连接事件时封账快照保持不变**。
* 对已封会话成员的 `retract` 返回合法业务拒绝 `sealed`，不改任何会话。

### 7.4 事件身份、重试与撤回

首次 event 先登记规范内容 `(principal, route, at_us, units)`（等价时区表示同一规范微秒）。

* 完全相同的规范身份重试返回 `duplicate`：**先识别重试，再做任何 F 裁决**，因此不重新接纳、不重复计量，也不因原时刻早于当前 F 而改判；原身份即使已 `late`、已被撤回或已经封账也是如此。重试原 event 不会撤销此前的 retract 结果。
* 同 id 但任何规范字段不同是输入错误（`ValueError`），不能用同 id 更正或重新激活，且失败不留半步状态。
* `retract` 必须指向先前已登记的 id，未知 id 是输入错误。若它仍是开放有效成员则移除并置 `retracted`；它的 `at_us` 即使早于 F，只要所在会话未封账仍允许撤回。若已撤回或首次登记即为 `late`，返回 `noop`；若属于已封会话，返回 `sealed`。

### 7.5 单步原子性、封账时机与期限一致性

每一步合法输入都在**同一状态变换内**完成事件接纳/撤回、开放会话重组与封账：

* 先对本步生效后的开放成员形成最大组件，再封账其中 `closes_at_us <= F` 的全部组件；`F = null` 不封。
* watermark 推进可在当步封多个组件；`closes_at_us` 恰等 F 立即封账。
* 撤回拆出的早期片段，即使本步没有推进 watermark，只要其 `closes_at_us <= F` 也必须在当步结算。
* 桥接/合并后的会话期限完全由新组件自己的 `last_us + gap_us` 决定，**不得沿用旧短片段的封账期限**：先接受桥接再推进截止时，旧短段的旧期限不能提前封新长段。
* 有限输入读完时不自动推进 watermark 或封账，仍开放的会话原样保留。

### 7.6 变更流、快照与下游重建

每个合法操作返回的 change 严格含 `step`、`status`、`retracted_session_ids`、`upserts`、`sealed_sessions`。`step` 从 1 起按**合法调用**递增，非法调用不占 step。`status` ∈ `accepted` / `late` / `duplicate` / `retracted` / `noop` / `sealed` / `watermark`；重复、迟到、已封拒绝等不修改业务状态的行仍占 step 并保留 status，其三个变更数组为空。

设 B 为本步之前的开放表、A 为本步全部修改及封账之后的开放表、N 为本步新封的表：

* `retracted_session_ids` 恰为 B 中 id 不在 A 也不在 N 中的旧开放身份，按 id 排序；只撤销旧开放身份，不撤销已封结果。
* `upserts` 恰为 A 中不存在于 B、或完整快照有变化的项；本步刚拆分后仍以新快照存在的旧 id 通过 upsert 替换，不作为无关删除。
* N 的每项都作为完整快照放入 `sealed_sessions`（即使同 id 之前在 B 中）；既有已封会话不重复发出。一个 change 不会在 upserts 与 sealed_sessions 中重复同一 id。
* 全部 session 数组按 `(principal, route, first_us, session_id)` 排序。

下游按固定次序应用即可仅靠「上一开放表 + 已封表」重建当前状态：先从开放表删除 `retracted_session_ids`；再以 `upserts` 替换或插入开放项；最后对 `sealed_sessions` 删除同 id 开放项并新增已封项。该流不泄漏未来操作，也不可能追改先前发出的 sealed 结果。每次返回的 dict 与列表都是独立拷贝，后续调用无法通过共享可变引用修改旧 change 或旧 snapshot。

`snapshot()` 严格返回 `watermark_us`、`cutoff_us`、`open_sessions`、`sealed_sessions`：watermark 为空时 cutoff 也为 `null`，否则 `cutoff_us = F`；已封表为全部已封快照并按 `(principal, route, first_us, session_id)` 排序（不是封账到达顺序）。`sessions_analyze(data)` 返回 snapshot 的四个字段再加 `trace`（每项即逐步 `apply` 的 change），严格按 `operations` 原次序调用同一语义并收集轨迹：**不能把输入先按事件时间重排再输出轨迹，第 k 步也不能使用后续操作**。空 operations 给出 null 边界、空 trace 与空会话数组。

### 7.7 错误、CLI 发布与兼容

构造 `SessionAccumulator` 时即校验 policies 与迟到配置；每个 `apply` 先完成本步全部校验与计算再提交，任一输入错误不留下规范身份登记、watermark、step、开放或已封会话的半步变更。CLI 外层严格为 `schema_version=5`、`mode="sessions"`、`source_sha256`、`result`；全部合法业务状态退出 0 且 stdout 与保存文件为同一 JSON；任何操作非法则整个 CLI 退出 2、只写 stderr、不打印成功 JSON，旧目标文件保留（不保存前半段轨迹）。继续复用 8MB 受限 JSON、重复键/非有限数值拒绝、同文件与硬链接别名及输出符号链接拒绝、原子保存；`source_sha256` 始终反映原始字节，operations 的业务顺序是语义的一部分。既有 `audit`(v1)/`retention`(v2)/`reconcile`(v3)/`chargeback`(v4) 四种模式及独立台账、事实仓契约保持不变。

### 7.8 验收与测试要点

* 取 gap=6s、allowed_lateness=2s，同 scope 事件 a@0s(1)、b@5s(2)、c@10s(3) 形成一个会话；watermark 到 10s 后撤回 b，须在该撤回步把 a(units=1, closes_at=6s) 封账、把 c(units=3, closes_at=16s) 留开放；随后 7s 新事件判迟到且不能重开 a，b 的完全相同重试判 duplicate 且不能复活。
* 两种关键次序：先接受桥接再推进截止时，原短段旧期限不能提前封新长段；先封独立早段再到达能在全量历史上连接它的迟到事件时，封账快照不变。gap 恰等边界断开、F 恰等边界的新事件接纳、closes_at 恰等 F 立即封账。
* 测试用手算预期逐步核对 trace，并用仅消费 change 的独立下游重建开放表与已封表，与每一步 snapshot 比对；预期不调用被测构造器生成，另以独立暴力参考引擎做随机差分。覆盖同时间多 id、最小 id 删除更名、多 scope、撤回拆两段且只封早段、最后成员撤回、旧 event 在撤回/封账/late 后的相同重试、等价时区与冲突 id、负 epoch 及微秒、watermark 相等及倒退、非法 apply 后继续合法输入、返回对象不被后续调用修改，并实跑 CLI 及错误保留旧报告，沿用原业务测试检查回归。
