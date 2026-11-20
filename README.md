# B03 通信网络资源管控 · 20 节点

在分组仿真中通过 A2A 运行 OM 运维和 RC 资源管控。OM 查询已抵达的报告并限定可写业务，RC 选择工具并联合求解，节点执行后由 OM 逐跳读回。固定规则与冻结模型共用这套工单流程。固定规则直接选用指定工具，全程可在 CPU 运行。

## 准备赛事数据

出于对赛事数据来源方的保密需要，本仓库仅公开 B03 源码、运行配置、测试和使用说明。场景设定、网络拓扑、业务需求及事件脚本由合作方从已持有的赛事数据包在本地加载。

请在仓库的**同级目录**创建 `b03-data/`，将赛事包解压到里面，保留包内层级。20 节点与 100 节点仓库可以共用这一份数据。以下命令在当前源码仓库根目录执行，将示例中的归档文件路径替换成自己的位置。

```bash
mkdir -p ../b03-data
tar -xf /path/to/official-dataset.tar.gz -C ../b03-data
export B03_DATA="$(cd ../b03-data && pwd)"
```

本仓库预期的目录结构如下。

```text
../b03-data/data/scenarios/sce20261010-conf02/sce01/
    net-topo.json
    bg-service.json
    scripts/
        script-09.json
        ...
```

若解压工具另加了一层目录，让 `B03_DATA` 指向直接包含 `data/` 的那一层，或直接指向上述含 `net-topo.json` 的场景目录。Windows PowerShell 可用 `$env:B03_DATA = (Resolve-Path ..\b03-data).Path`。程序也支持用 `--data` 指定已有赛事归档或场景目录。

## 运行固定规则

准备 Python 3.12，并将 `B03_SIMULATOR` 指向本地只读分组引擎 `resnet_py`。从仓库根目录执行以下命令。

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export B03_SIMULATOR=/path/to/resnet_py
python run.py run --output runs/first
python run.py verify runs/first --report runs/first-check.json
```

默认使用 `--mode workorder --policy I`。使用 `--policy combined` 启用条件组合规则，`--services all` 处理完整需求名单，`--script` 选择官方脚本。配置切换和读回都经过设备侧。每次运行使用新的输出目录，可选参数见 `python run.py run --help`。

## 接入本地模型

20 节点的冻结接口读取 v18 Hugging Face Safetensors 目录。模型在当前获准的动作中选工具，具体配置仍由规则求解，并沿用相同的设备校验与读回流程。使用模型模式时需另备本地权重。

```bash
python -m pip install -r requirements-model.txt
python run.py run --policy model --device cuda --checkpoint /path/to/v18-hf --output runs/model
python run.py verify runs/model --report runs/model-check.json
```

## 代码导航

| 入口 | 用途 |
|---|---|
| [run.py](run.py) · [src/cli.py](src/cli.py) | 运行、参数与结果核验 |
| [src/scene.py](src/scene.py) · [config/profile.json](config/profile.json) | 定位本地数据、校验文件、选择运行窗口 |
| [packet_roles.py](src/packet/packet_roles.py) · [packet_a2a.py](src/packet/packet_a2a.py) | OM 查询、授权、RC 提交与读回签收 |
| [strong_tools.py](src/rules/strong_tools.py) · [opening.py](src/opening.py) · [frozen_policy.py](src/frozen_policy.py) | 联合分配、剩余容量补查与模型选工具 |
| [packet_device.py](src/packet/packet_device.py) · [packet_device_readback.py](src/packet/packet_device_readback.py) | 指令下发、节点执行、超时重试与读回 |
| [packet_measure.py](src/packet/packet_measure.py) · [verify_packet.py](src/packet/verify_packet.py) | 业务保障、控制时钟、分组计量与证据核验 |

[本地资源和输出说明](docs/ASSETS.md) · [运行核验](docs/VALIDATION.md)
