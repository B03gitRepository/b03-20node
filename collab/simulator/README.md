# 仿真组 · 四类公开数据端口

底层默认使用仓内锁定的分组仿真引擎，v2 扩展组件通过**四类纯数据回调端口**接入（无需继承引擎内部类）。仓内示例使用合成数据。切换插件只需改一份组件清单，主控的 OM/RC、分组时钟、设备指令通道、逐跳读回与业务计量完全复用。

## 本地离线自测

在仓库根目录执行（仅需 Python 3.10+ 标准库，自动读取仓内 `src/simulator_ports.py`，不启动仿真引擎也不依赖赛事数据）：

```sh
python collab/simulator/check.py
python collab/simulator/check.py --provider my_detector.py --factory build --fixture my-case.json
```

[合成样例 case.json](case.json) 包含一条虚构链路变化、一次延时探针、一次设备查询和一段计量区间。[参考实现](../../src/example_public_simulator.py) 演示了链路变化后触发本端设备探针的零内部依赖写法。

## 实现四个回调端口

工厂函数 `build(settings)` 返回一个对象，需声明与组件注册一致的 `id`、`version` 和 `contractVersion = "b03.packet-simulator.v2"`。所有回调时间戳均为整数仿真纳秒；完整字段名与示例记录见 [case.json](case.json)。

| 回调方法 | 系统传入内容 | 返回值与作用 |
|---|---|---|
| `on_event(record)` | 计分窗内发生链路状态或参数变化时触发（序号仅计此类可见批次）；包含物理变化时刻、变化链路、允许采样的 `probeNodes` 及剩余窗口时长 | 返回形如 `[{"op": "probe", "nodeId": ..., "delayNs": ...}]` 的列表（每个可采节点至多一次）。桥接层在正延时后、关窗前调度物理采样，在线节点经真实队列发送常规探针包 |
| `on_observation(record)` | 探针或业务上报到达控制面时触发；含原始采样时刻、到达时刻、报文 ID、是否接收、原因及送达消息 | 返回 `None`。OM/RC 仅读取原生已送达缓存 |
| `on_device(record)` | 排空结束后按原生发生顺序回放节点执行与回执到达记录；含指令/报文 ID、状态、版本与执行时钟（查询回执在 `readback` 中附节点本地采样） | 返回 `None`。工单配置仍走现有设备指令通道 |
| `on_meter(interval)` | 排空结束后交付每段左闭右开业务计量区间，含起止时刻、活跃/未保障业务数及 `U / W` 纳秒增量 | 返回 `None`。数值由主控计量器依据物理路径与原始需求独立计算 |

所有回调输入均为独立拷贝；`changes` 仅反映后端当前物理事件批次，控制面只能读取经分组网络送达后的报文。主控在所有事件推进至排空结束、动作端口关闭后，才把设备时间线与业务计量区间交付给扩展组件（保留原始时间戳），确保回调无法偷看计量损失或未确认生效结果来反向调度本回合探针。

## 接入真实工单对照

使用仓库 Python 环境并设置 `B03_DATA` 后，在仓库根目录运行：

```sh
python run.py run --components config/collab-simulator.json --output runs/probe-demo
python run.py verify runs/probe-demo --strict-source --components config/collab-simulator.json
```

复制 [示例配置](../../config/collab-simulator.json)，将 `simulator.entrypoint` 改为 `your_module:build` 并更新 `id`、`version` 与 `settings`。外部安装包或源码依赖按 [依赖溯源说明](../../docs/PROVENANCE.md) 登记。运行摘要会记录四类回调次数与探针请求数；`verify` 会逐项核对回调次数、设备执行/回执/计量记录的有序 SHA-256 摘要，并把每次探针请求与真实变化端点及完成回调逐一对齐。底层引擎兼容与锁文件说明见 [docs/SIMULATOR.md](../../docs/SIMULATOR.md)。
