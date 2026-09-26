# 建设送出功率分阶段承诺门禁基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理海上风电场、送出通道、机组资源批次、场站申报、功率分配、调度情景与机组健康准入。业务状态、登录权限、幂等结果和审计事件保存在 SQLite 中，适合生产调度、设备质量、风险与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/wind_dispatch/`：场站、送出通道、机组可用量、功率申报、日前分配和调度情景；
- `src/commitment_gate/`：送出边界版本、场站申报、分阶段承诺计划、紧急保供豁免和执行结算；
- `src/turbine_health/`：机组健康协议、测点导入、异常复核、分析任务租约和健康决定；
- `src/grid_qualification/`：并网机组批次、检测数据、分析、账号登录与质量审批；
- `fixtures/`：离线验收使用的检测协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 承诺门禁

`commitment_gate` 把日前承诺、无功补偿能力、海缆热限额和场站检修计划合并成可审核的分阶段送出承诺：

- 调度人员发布带版本和有效期的送出边界（通道容量、无功补偿、海缆热限额、检修窗口），迟到修订只重估尚未确认的计划；
- 场站申报装机容量、机组可用率、爬坡曲线、备用要求与最迟响应时刻；
- 系统据此形成准备、并网、爬坡、稳定运行四阶段计划，并给出获批、降额或需要豁免的约束解释；
- 确认计划在单个事务里一次性锁定通道与补偿余量，失败不留下部分冻结；
- 紧急保供豁免记录授权人、理由与失效时间，过期豁免不能用于确认；
- 执行回执按实际完成量结算并释放冻结，待确认状态保存在 SQLite 中，服务重启后可直接恢复。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m wind_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m commitment_gate.acceptance --workspace .
PYTHONPATH=src python3 -m turbine_health.acceptance --workspace .
PYTHONPATH=src python3 -m grid_qualification.acceptance
```

四条命令使用临时 SQLite 数据库完成场站与通道登记、功率申报分配、分阶段承诺门禁、健康测点分析和并网审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m wind_dispatch.api --database wind.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m turbine_health.api --database health.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m grid_qualification.api --database grid.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m commitment_gate.api --database gate.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。
