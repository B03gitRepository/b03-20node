# 分组仿真适配器

默认工单使用 `b03.resnet` 适配器和锁定的 `resnet-simulation` 1.1.0 分组引擎。引擎在纳秒级 SimPy 时钟上执行官方拓扑、需求与事件。OM/RC 通过 A2A 交换已隔离的观测。指令、回执和逐节点配置查询均经过仿真分组网络，主控计量器独立核算已生效路径、QoS 与实际需求。

## 公开扩展接口

新扩展请从[四个公开数据接口、独立样例与测试](../collab/simulator/README.md)开始。工单主控使用已送达快照和唯一的指令发送器。后端桥接层负责挂接原生控制环、采集节点本地读回、接入物理时钟和评价快照。队列、传输及设备状态机由分组引擎提供。

## 后端实现边界

原生桥接层集中挂接控制环与设备执行器、读取生效的转发状态，并将物理事件批次映射为公开事件。更换回调提供方时使用四个公开输入。更换原生后端时须保留以下面向主控的操作。

| 操作 | 主控使用方式 |
| --- | --- |
| 时钟与交付 | `advance(until_ns)` 按原生次序处理事件，包括同时刻优先级。`start_ns`、`end_ns`、`stop_ns` 和 `now` 均为整数纳秒。`summary()` 返回评价及排空边界的分组、设备及守恒计数。 |
| 已送达证据 | `connect_work_order(roles, emit, ...)` 将一个工单循环绑定到指定时刻的只读链路及需求快照、已送达需求规格、控制日志及唯一指令发送器。OM/RC 沿用原有的隔离输入。 |
| 设备 | 原生指令路径完成序列化、传输、执行和应答。桥接层为每个节点连接一个本地读取器。重复查询重放首次样本和分组标识。 |
| 计量 | `start_meter(directory, solver)` 在同一时刻的原生变化后采集评分快照。`meter_snapshot()` 仅向评价器提供物理节点、链路、活动原业务及端到端生效配置。`payload_wire_bps(rate)` 核算线速。执行至排空终点并包含该时刻事件后，`finish_meter()` 关闭动作入口、重演终态设备和计量记录，并输出 U/W、分母及连通度。`close_meter()`、`close()` 释放资源。 |

## 原生 v1 接口兼容

`B03_DATA` 指向官方场景目录、解压目录或原始压缩包。默认引擎在当前仓库的 `simulator/resnet_py/`。可用 `--simulator` 或 `B03_SIMULATOR` 选择外部版本，命令行参数优先。每次运行请使用新输出目录。

```sh
python run.py run --output runs/periodic
python run.py run --components config/example-simulator.json --output runs/local-probes
python run.py verify runs/local-probes
python run.py verify runs/local-probes --strict-source \
  --components config/example-simulator.json
```

示例实现设备本地链路变化检测。链路参数或管理状态变化后，两端各等待配置的检测间隔、采样本地端口，再发出原生探测分组。示例默认间隔为 200,000,000 ns。离线节点无法发送。网络队列、丢包、两端报告、观测新鲜度及设备读回均保留在工单中。基线继续原有周期探测。对照 `SUMMARY.json` 和 `controller.jsonl` 中的事件及两端已收到的报告，可分别比较检测耗时、U/W、分组开销和配置完成情况。

## 原生 v1 提供方接口

原生 v1 仿真组件登记 `id`、`version`、`contractVersion`、`entrypoint` 和可选 JSON 对象 `settings`，契约为 `b03.packet-simulator.v1`，入口为可导入的 Python 工厂。新接入优先使用上文四个公开端口。

```json
{
  "extends": "components.json",
  "simulator": {
    "id": "example.local-port-probes",
    "version": "1.0.0",
    "contractVersion": "b03.packet-simulator.v1",
    "entrypoint": "example_simulator:build",
    "settings": {"detectionDelayNs": 200000000}
  }
}
```

`build(settings)` 接收隔离的对象，返回所选引擎 `resnet.simulation.simulation.Simulation` 的子类。其 `adapter_id`、`adapter_version` 和 `contract_version` 必须与清单一致。构造函数接收原生场景、仿真配置、运行目录及 `decision_policy` 关键字。运行时依赖原生 SimPy 时钟、分组链路与队列、控制器、节点执行器、指令和查询接口、状态、计数及 `advance`、`summary`、`log`、`close`。提供方保留这些接口及交付语义，主控在其上接入计量钩子和设备闭环。[提供方样例](../src/example_simulator.py) · [适配器与身份检查](../src/simulator_component.py)

物理事件、真实链路及设备状态供仿真器与评价器使用。设备探针采样本地状态，控制器以分组到达时刻标记观测。OM/RC 只取得作用域、已送达报告及查询回复的副本。提供方作为受信任的 Python 代码与引擎同进程运行。角色接口只提供相应观测与权限。

## 使用兼容的引擎版本

外部提供的源码版可放在仓库外。主控从其 `src` 导入 `resnet`。更换版本时，在仿真器的 `provenance.assets.engineLock` 登记完整引擎锁，其路径相对声明它的清单解析。提供方的软件包、辅助模块和资产沿用模型与规则组件的来源登记方式。[提供方与依赖记录](PROVENANCE.md)

```json
{
  "extends": "../config/example-simulator.json",
  "simulator": {"provenance": {"assets": {"engineLock": "partner.lock.json"}}}
}
```

锁包含 `distribution`、`version`、`files` 和 `runtimeSha256`。文件表使用相对发布根目录的 POSIX 路径和小写 SHA-256 摘要。纳入 `src` 下所有可导入文件、原生 `configs/common.json`、`pyproject.toml` 及替代引擎实际读取的其他输入或实现文件。项目名、版本需与锁一致。按下式从排序后的文件表计算运行时摘要。

```python
hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
```

使用合作方清单及实际引擎路径运行，通过 `--data` 或 `B03_DATA` 显式提供官方数据。运行前后均核对所选引擎。封存内容包括引擎锁原字节、适配器身份、运行时指纹、原输入及分组回执。档案核验直接读取这些记录，严格核验还对照已安装引擎、源码、登记资产及依赖。

含 `node_move` 的官方脚本通过 `--data` 载入时使用 `endpoint_at_event`。原生事件批次在事件时刻把节点放到指定终点坐标。官方输入未提供移动持续区间，链路变化由脚本提供的链路事件及引擎设备状态驱动。评价器轨迹中的坐标只属于物理事件证据。角色观测仍须由分组送达。

## 暂缓清理不可达设备

默认设备环等待未完成的清理。长时间故障时，可在启动工单时启用以下选项，让无资源冲突的其他工单继续。

```sh
python run.py run --mode workorder --defer-unreachable-cleanup --output runs/deferred-cleanup
python run.py verify runs/deferred-cleanup
```

仅在释放单项业务的终态工单满足全部条件时可暂缓。控制器需确认源端已停止，旧代次完整保留，清理写入过期，后续协调查询无回复，原生事务也已超时。控制器保护这项业务、旧线速占用的全部链路及不可达设备。其他工单可使用互不冲突的链路和其他目标设备。两条路径共用的健康路由器按其当前设备版本串行执行写入。

收到来自此前不可达设备的新鲜原生探测后，控制器恢复旧工单，先查询该旧业务及配置代次，再根据读回决定是否重发清理。全部节点读回完毕后才归还保留容量并记录 OM 结果。终态中 `b03LoopStatus.deferred_cleanups` 列出未解决代次。核验器检查保留账、恢复探测及后续设备操作次序。
