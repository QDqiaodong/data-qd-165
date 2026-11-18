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
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 区间计量证据对账模式

`--mode reconcile` 判断「采样系统的区间用量上下界」与「结算系统的区间精确账单」是否可能来自同一条整数用量时间序列，并在不可行时给出可逐边复核的负权环证书。输入/输出契约、求解结构与证书语义见 [数据契约](docs/contract.md) 第 3 节。

```sh
PYTHONPATH=src python3 -m api_quota_audit.cli \
  --mode reconcile --source fixtures/reconcile.json --output run/reconcile.json
```

输出 `schema_version=3`，`result` 严格含 `status`、`ranges`、`certificate`。`status` 为 `feasible`、`meter_conflict` 或 `invoice_conflict`；两个冲突是合法业务报告，退出码仍为 0。可行时每个 query 给出精确最小/最大值与两份完整 slot 分配见证；冲突时给出原始约束边组成的闭合负权环，边数不超过 `n+1`。全部使用精确整数差分约束与全点最短路，不做浮点近似、逐单位枚举或外部求解器。

