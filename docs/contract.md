# API 配额审计数据契约

本工具有两种离线模式，均只读取本地 JSON、只写本地报告，不调用任何外部服务。

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
