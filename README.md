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

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/api_quota_audit/model.py 为业务模型，common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。
