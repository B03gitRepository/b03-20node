# 仿真组 · 四类纯数据回调端口接口

## 一、模块职责

底层默认使用仓内锁定的分组仿真引擎，v2 扩展组件通过**四类纯数据回调端口**接入（无需继承引擎内部类）。仓内示例使用合成数据。组件可在物理事件变化时调度延时探针，并只读消费观测到达、设备执行/回执及业务计量记录；主控的 OM/RC、分组时钟、设备指令通道、逐跳读回与业务计量完全复用。

---

## 二、通用接口：启动初始化与身份声明

组件模块须暴露可调用工厂函数（默认名 `build`，通过配置 `simulator.entrypoint` 指定）：

```text
build(settings) -> provider
```

| 参数 / 属性 | 类型 | 含义与要求 |
|---|---|---|
| `settings`（参数） | `dict` | 组件配置 `simulator.settings` 传入的独立参数副本 |
| `provider.id`（属性） | `str` | 组件唯一标识，须与配置 `simulator.id` 一致 |
| `provider.version`（属性） | `str` | 组件版本号，须与配置 `simulator.version` 一致 |
| `provider.contractVersion`（属性） | `str` | 固定声明为 `"b03.packet-simulator.v2"` |

返回的 `provider` 对象须实现四个回调：`on_event(record)`、`on_observation(record)`、`on_device(record)`、`on_meter(interval)`。所有回调时间戳均为整数仿真纳秒（ns），每次传入数据均为独立拷贝；完整记录样例见 [case.json](case.json)。

---

## 三、两条回调分支：事件响应与只读观测 / 回放

### 分支 A：事件响应回调 `on_event(record) -> list[dict]`

- **触发时机**：计分窗内发生链路状态或参数物理变化时触发（`sequence` 仅计此类可见批次）。
- **输入 `record` 核心字段**：

  | 字段 | 类型 | 含义 |
  |---|---|---|
  | `sequence` | `int` | 可见物理事件批次序号 |
  | `timeNs` | `int` | 物理变化发生时刻（**ns**） |
  | `remainingNs` | `int` | 距计分窗关闭的剩余时长（**ns**） |
  | `changes` | `list[dict]` | 当前物理事件批次明细（含 `type`、`linkId`、`endpointIds` 等） |
  | `probeNodes` | `list[str]` | 本次允许调度物理采样的节点 ID 列表 |

- **返回格式与约束**：
  - 返回探针调度列表（可为空 `[]`），每项为：
    ```text
    {"op": "probe", "nodeId": "<probeNodes 内节点 ID>", "delayNs": <正整数纳秒>}
    ```
  - 同一批次内每个允许采样的节点至多请求一次；`delayNs` 须为正整数且须小于 `remainingNs`（落在关窗之前）。桥接层在正延时后调度物理采样，由在线节点经真实分组队列发送常规探针包。

### 分支 B：只读观测与排空后回放回调（均返回 `None`）

为防止扩展组件借助未送达回执或业务计量损失反向调度本回合探针，主控在所有事件推进至排空（drain）结束、动作端口关闭之后，才交付 `on_device` 与 `on_meter`（保留原始纳秒时间戳）：

| 回调方法 | 触发时机 | 输入核心字段与单位 | 约束与说明 |
|---|---|---|---|
| `on_observation(record)` | 探针或业务上报到达控制面时实时触发 | `kind`、`sampledAtNs`、`arrivedAtNs`、`packetUid`、`nodeId`、`sequence`、`accepted`、`reason`、`message`（链路含 `capacity_bps` **bit/s**、`delay_ns` / `jitter_ns` **ns**、`ber`、`packet_loss_rate`、`queue_packets`） | 返回 `None`；OM/RC 仅读取原生已送达缓存 |
| `on_device(record)` | 排空结束后按原生发生顺序回放 | `phase`（`"execution"` 或 `"ackArrived"`）、`timeNs`、`nodeId`、`commandId`、`operation`（执行期）、`status`、`executedAtNs`、`version`、`packetUid`；查询回执另附 `readback`（节点本地采样） | 返回 `None`；工单配置仍走现有唯一设备指令通道 |
| `on_meter(interval)` | 排空结束后交付每段左闭右开计量区间 | `startNs`、`endNs`、`activeCount`、`unmetCount`、`unmetServiceNs`（`U` 纳秒增量）、`weightedUnmetServiceNs`（`W` 纳秒增量） | 返回 `None`；数值由主控计量器按物理路径与原始需求独立计算 |

---

## 四、配置替换与仓内完整实现索引

### 1. 注册配置结构

复制 [config/collab-simulator.json](../../config/collab-simulator.json)，更新 `simulator` 下的 `id`、`version`、`entrypoint` 与 `settings` 即可在启动时替换组件：

```json
{
  "extends": "components.json",
  "simulator": {
    "id": "example.public-port-probes",
    "version": "1.0.0",
    "contractVersion": "b03.packet-simulator.v2",
    "entrypoint": "example_public_simulator:build",
    "settings": { "detectionDelayNs": 200000000 }
  }
}
```

外部安装包或源码依赖按 [docs/PROVENANCE.md](../../docs/PROVENANCE.md) 登记；底层分组引擎兼容性、自定义引擎锁定与锁文件校验见 [docs/SIMULATOR.md](../../docs/SIMULATOR.md)。

### 2. 仓内完整实现索引

| 用途与模式 | 参考源码 / 目录 | 配套配置 |
|---|---|---|
| 四端口纯数据回调参考实现（`b03.packet-simulator.v2`） | [example_public_simulator.py](../../src/example_public_simulator.py) | [collab-simulator.json](../../config/collab-simulator.json) |
| 公开端口协议校验与仿真组件加载 | [simulator_ports.py](../../src/simulator_ports.py) / [simulator_component.py](../../src/simulator_component.py) | [components.json](../../config/components.json) |
| 原生仿真引擎子类扩展示例（v1 兼容） | [example_simulator.py](../../src/example_simulator.py) | [example-simulator.json](../../config/example-simulator.json) |
| 仓内内置分组仿真引擎与版本锁文件 | [simulator/resnet_py/](../../simulator/resnet_py/) | [simulator.lock.json](../../config/simulator.lock.json) |

---

## 五、离线自测与真实工单联调命令

### 1. 本地离线自测（仅需 Python 3.10+ 标准库，不启动仿真引擎也不依赖赛事数据）

在仓库根目录执行，使用 [case.json](case.json) 与 [check.py](check.py)（自动读取仓内 `src/simulator_ports.py`）校验身份声明、事件响应与回放接口：

```sh
python collab/simulator/check.py
python collab/simulator/check.py --provider path/to/your_detector.py --factory build --fixture collab/simulator/case.json
```

### 2. 真实工单运行与严格核验（需配置基础依赖与 `B03_DATA`）

```sh
python run.py run --components config/collab-simulator.json --output runs/probe-demo
python run.py verify runs/probe-demo --strict-source --components config/collab-simulator.json
```

运行摘要会记录四类回调次数与探针请求数；`verify` 逐项核对回调次数、设备执行/回执/计量记录的有序 SHA-256 摘要，并把每次探针请求与真实变化端点及完成回调逐一对齐。
