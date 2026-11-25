# B03 通信网络资源管控系统（20 节点）

> 本仓库为 **20 节点** 分组仿真协作基线（与另一规模共用同一套组件接口和评测闭环）。
>
> 核心导航：[四组契约、样例与自测](collab/README.md) · [实测基线与指标](docs/RELEASE_RESULTS.md) · [完整对接与评分指南](docs/JOINT_OPTIMIZATION.md)

---

## 一、30 秒看懂系统怎么跑

![B03 通信网络资源管控系统架构全景](docs/architecture.svg)

> 💡 **交互式流程演示**：克隆仓库后，用浏览器直接打开 [docs/flow_demo.html](docs/flow_demo.html) 即可单步播放“一张工单如何走完闭环”及“四组改哪里”（离线单文件，无需联网或装依赖）。

系统由 **运维智能体（OM）** 和 **资源控制智能体（RC）** 协作，围绕一张工单走完 5 步闭环：

1. **感知与开单（OM）**：分组仿真器推进网络事件，探针经真实网络队列将状态上报给 OM；OM 只读诊断后开立工单，明确**可写业务范围、保护名单与状态版本**。
2. **协议交接（A2A 1.0）**：OM 通过 A2A 协议将工单和当前观测快照交给 RC。
3. **决策与求解（RC → 模型 / 知识库 / 规则库）**：RC 用固定策略或模型在合法目录内选定一个工具，按需查询只读技术知识（如 `K044` 准入核查），再调用规则求解器算出具体路径与带宽分配。
4. **校验与唯一写入（关口 → 执行器）**：校验关口独立检查权限、共享容量、原始 QoS 和状态版本；通过后由**唯一设备执行器**顺序下发，并做**逐跳读回**确认真实生效。
5. **签收与复算（OM → 离线核验）**：读回证据交回 OM 复核签收；完整轨迹落盘后，用 `run.py verify` 离线复算五项赛题指标和 `U / W`。

**统一对照原则**：固定规则（如 `--policy I`）和模型推理（`--policy model`）走完全同一套工单、校验、写入和读回链路，保证各组改进都能在同条件下公平对比。

---

## 二、按小组快速找入口

| 小组 | 你负责替换的部分 | 配置示例（基于 `config/components.json`） | 接口细则文档 |
|---|---|---|---|
| **模型组** | 从已送达观测选一个合法工具；两种规模共用采样与评价接口 | `config/collab-selector.json` | [模型组开工](collab/model/README.md) |
| **规则组** | 接收快照、权限与预算，返回需变更的业务路径和审计信息 | `config/collab-rules.json` | [规则组开工](collab/rules/README.md) |
| **知识库组** | 按 ID 或主题返回本版条目，排序可调；OM、规则与模型可各自启用 | `config/collab-rules-knowledge.json` | [知识组开工](collab/knowledge/README.md) |
| **仿真组** | 通过事件、已送达观测、设备和计量四类公开端口接入分组引擎 | `config/collab-simulator.json` | [仿真组开工](collab/simulator/README.md) |
| **系统集成组** | OM/RC、A2A、权限目录、方案校验、唯一写入、五指标核验与发版合入 | `config/components.json` | [联调与评分 docs/JOINT_OPTIMIZATION.md](docs/JOINT_OPTIMIZATION.md) |

> **⚠️ 关于“可替换”的客观边界**
>
> 只要遵循本组接口契约，即可在启动时通过配置（`--components config/xxx.json`）替换组件，无需改动主控和评测链路。新算法可先用固定工具 ID（如 `--policy X`）独立验证收益；若希望已训练的模型学会调用新工具，则需登记新目录版本与模型映射，并配合重新训练验证。

---

## 三、5 分钟跑通最小闭环

需要 **Python 3.12** 和赛事方分发的官方场景包。分组仿真引擎 **resnet_py 1.1.0** 的源码已内置于 `simulator/resnet_py/`，设置 `B03_DATA` 即可运行。`B03_SIMULATOR` 可选，用于指定外部引擎。详见[资产与路径说明](docs/ASSETS.md)。

```bash
# 1. 指向本地官方数据目录
export B03_DATA=/path/to/b03-data
export PYTHONDONTWRITEBYTECODE=1

# 2. 创建环境并安装基础依赖
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# 3. 跑通默认工单并离线核验（每次请使用新的输出目录）
python run.py run --output runs/first
python run.py verify runs/first --report runs/first-check.json
```

- **本仓默认配置**：对应官方场景 `data/scenarios/sce20261010-conf02/sce01/`、默认脚本 **`09`**（3 条授权业务、固定工具 `I`、残余准入）；默认参考权重为 **`v18（动作映射 A–C）`**。全量业务可加 `--services all`，更多参数见 `python run.py run --help`。
- **训练与采样入口**：20 / 100 节点统一通过 `episode.py sample` 与 `episode.py evaluate` 采样工单轨迹并获取 `U / W` 评价，模型组可自由接入自家训练器（详见 [collab/model/README.md](collab/model/README.md)）；内置 `train.py step` 仅面向 100 节点 `v374` 与脚本 `04/05`。
- **源码单测**：运行 `python -B -m unittest discover -s tests`（92 项回归测试）。

**进阶用法按需查阅**（不在首页堆命令）：
- 切换各组插件、跑独立规则 `M` 或对比五项指标 → [docs/JOINT_OPTIMIZATION.md](docs/JOINT_OPTIMIZATION.md)
- 跑冻结模型、CPU 动态 INT8 或在线 PPO 恢复 → [docs/MODEL.md](docs/MODEL.md)
- 导出真实组件 I/O 样例（`run.py examples`）与严格依赖溯源（`--strict-source`） → [docs/PROVENANCE.md](docs/PROVENANCE.md)
- 查看当前验收实测数据与已知边界 → [docs/RELEASE_RESULTS.md](docs/RELEASE_RESULTS.md)

---

## 四、目录速览与协作约定

```text
├── run.py / episode.py / train.py  # 运行核验、统一采样评价与参考训练入口
├── collab/                         # 四组黑盒开工包（精简契约、合成样例与离线自测 check.py）
├── config/                         # 默认组件清单 (components.json) 及各组替换示例配置
├── src/                            # OM/RC、A2A、唯一设备执行、逐跳读回与各组件适配器实现
├── simulator/                      # 分组网络仿真引擎 resnet_py 源码
├── knowledge/                      # 版本化技术知识 JSON
├── docs/                           # 架构图、交互流程演示页及四组详细接口文档
└── tests/                          # 92 项源码与契约回归测试
```

**三点协作约定**：
1. **分支开发**：从 `main` 切自己分支，通过 `config/` 扩展配置切换自家组件；**每次对比尽量只换 1 个组件**。
2. **PR 交付物**：附上配置差异、复现命令，以及同条件下的 `run.py verify` 对照表（动作、U/W、耗时、设备生效与读回数）。
3. **数据说明**：赛事方无公开许可，具体业务场景数据暂不上传 GitHub；大体积模型权重与运行日志按需保存在本地或服务器即可。

<details>
<summary>关键核心源码文件直达（供系统集成与联调查阅）</summary>

- [OM / RC 角色与工单鉴权](src/packet/packet_roles.py)
- [A2A 1.0 通信传输](src/packet/packet_a2a.py)
- [唯一设备写入执行](src/packet/packet_device.py)
- [逐跳配置读回](src/packet/packet_device_readback.py)
- [分组与业务指标统计](src/packet/packet_measure.py)

</details>
