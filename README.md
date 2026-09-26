# 建设送出功率分阶段承诺门禁基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理海上风电场、送出通道、机组资源批次、场站申报、功率分配、调度情景与机组健康准入。业务状态、登录权限、幂等结果和审计事件保存在 SQLite 中，适合生产调度、设备质量、风险与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/wind_dispatch/`：场站、送出通道、机组可用量、功率申报、日前分配、调度情景和送出承诺门禁；
- `src/turbine_health/`：机组健康协议、测点导入、异常复核、分析任务租约和健康决定；
- `src/grid_qualification/`：并网机组批次、检测数据、分析、账号登录与质量审批；
- `fixtures/`：离线验收使用的检测协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 送出承诺门禁

`wind_dispatch` 在单一通道容量之上提供可审核的分阶段送出承诺：

- 调度角色发布带版本和有效期的送出边界（`POST /boundaries`），合并通道容量、海缆热限额、无功补偿能力与检修降额；版本必须递增，迟到的修订只重新评估仍待确认的计划；
- 场站角色申报机组可用率、爬坡曲线、备用要求和最迟响应时刻（`POST /declarations`，幂等），系统据此形成准备、并网、爬坡、稳定运行四个阶段，并给出获批、降额或需要豁免的门禁结论及原因；
- 确认计划（`POST /plans/{id}/confirm`）在同一事务内锁定通道和补偿余量，失败不留下部分冻结；超过最迟响应时刻或门禁要求豁免时，必须存在风控角色登记的紧急保供豁免（`POST /exemptions`，记录授权人、理由与失效时间）；
- 执行回执（`POST /plans/{id}/receipts`）按各阶段实际完成量结算，超发部分只记录不结算，结算后释放锁定；
- 后台查询（`GET /plans/{id}`、`GET /plans?state=pending`）返回评估依据、锁定、豁免和决策日志，进程重启后待确认状态从 SQLite 恢复。

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
PYTHONPATH=src python3 -m turbine_health.acceptance --workspace .
PYTHONPATH=src python3 -m grid_qualification.acceptance
```

三条命令使用临时 SQLite 数据库完成场站与通道登记、功率申报分配、送出承诺门禁、健康测点分析和并网审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m wind_dispatch.api --database wind.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m turbine_health.api --database health.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m grid_qualification.api --database grid.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。
