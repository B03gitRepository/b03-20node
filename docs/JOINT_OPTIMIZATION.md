# 联合优化指南

四组的接口、合成样例和独立自测集中在[协作入口](../collab/README.md)。

20 和 100 节点使用同一套工单和组件契约。选择与官方输入规模匹配的仓库，先按[首页](../README.md)运行默认工单，保存核验报告作为对照。

| 小组 | 职责 | 首个联调示例 | 提交的证据 |
|---|---|---|---|
| 模型组 | 工具选择、观测编码、数值精度、采样和检查点恢复 | [模型接口](MODEL.md)、`config/int8-model.json` | 记录合法动作和策略版本。训练还需记录真实行为概率、两次参数更新和新进程恢复。 |
| 仿真组 | 分组队列、上报传输、设备执行和引擎兼容 | [仿真接口](SIMULATOR.md)、`config/example-simulator.json` | 引擎指纹、配对的上报时间、指令编号和逐跳读回。网络等待与求解耗时分别计量。 |
| 知识库与规则库组 | 技术条目版本和只读检索、候选路径及多业务联合求解 | [知识接口](KNOWLEDGE.md)、[规则接口](RULES.md)、`config/example-knowledge.json`、`config/branch-rules.json` | 知识编号、来源和适用条件。规则另附授权范围、配置提案、审计与实测结果。 |
| 系统集成组 | OM/RC、A2A、权限、动作目录、设备写入、计分、回归与发布 | `config/components.json`、`run.py verify` | 同输入对照、归档回执、版本对应关系和通过验收的提交。 |

## 提交一项改动

1. 从约定基线建分支，注明规模、基线提交、场景指纹、随机种子、计分窗、排空期、可写业务、组件版本和所用模型检查点。
2. 新建继承 `config/components.json` 的配置，每次替换一个组件。登记实现版本，将可移植资产放在清单旁或可安装的软件包中。[溯源格式](PROVENANCE.md)支持外部发行包及版本化资产。
3. 使用同一份授权数据、只读引擎、CPU 设置及预热约定运行基线和候选，各自写入新目录。
4. 在候选版执行归档核验、带原依赖的严格核验和样例导出。在合并请求中附复现命令和简短对照表，场景轨迹、数据集与权重保存在授权工作区。
5. 系统集成组验证两个节点规模，登记通过的组件版本和 Git 提交。更改模型可见的工具语义或观测编码时，单独登记版本，并在留出场景与原策略对照。

```bash
python -B -m unittest discover -s tests
python run.py run --record-component-io --output runs/reference
python run.py run --components config/example-simulator.json --record-component-io --output runs/simulator
python run.py run --policy M --components config/branch-rules.json --record-component-io --output runs/pure-rule
python run.py run --components config/example-knowledge.json --record-component-io --output runs/knowledge
python run.py knowledge --components config/example-knowledge.json --id K044
python run.py verify runs/simulator --report runs/simulator-check.json
python run.py verify runs/simulator --components config/example-simulator.json --strict-source --simulator "$B03_SIMULATOR" --report runs/simulator-strict.json
python run.py examples runs/simulator --output runs/simulator-examples.json
```

模型示例还需按[模型文档](MODEL.md)安装依赖、设置权重和分词器。固定规则和模型均经 OM/RC、A2A、同一设备写入口和逐跳读回。固定规则按配置直接选工具。

## 比较结果

`SUMMARY.json` 的 `competitionMetrics` 使用仿真纳秒。每组时延报告样本数、均值、最近秩 P95 和最大值，空样本的时延为空。主表覆盖计分窗，`includingDrain` 延伸至设备排空结束。拒绝、未完成或没有配置变更的工单可按记录逐项查看。

| 指标 | 计算口径 |
|---|---|
| 状态感知 | 从窗内官方事件到匹配的新鲜报告送达。链路变化需两端均有匹配报告。节点故障缺少匹配报告时仍计入事件分母。现有协议没有纯位置变化的坐标上报。 |
| 策略生成 | 从绑定的 OM 快照内最新的新鲜报告送达至方案就绪。`generationComputeNs` 单列 RC 实测计算耗时。缺少新鲜证据的工单单独计数。 |
| 策略下发 | 对已签收工单，从首次发送配置写指令至最后一条不同写指令送达。同一指令重试沿用原编号，首次下发、最后观测到的设备生效与 OM 最终签收分别留时戳。 |
| 连通度 | 按有效时长加权计算在线节点对连通率。活跃业务端点可达率使用自己的分母。 |
| 业务保障 | 报告请求业务秒、未保障业务秒 U 及保障比例，按优先级加权的 W 用于评价。分组交付与丢包另见分组计数。 |

业务速率、QoS、优先级、权限和时间窗取自实际工单。技术知识为处置提供参考。设备确认释放后，对应资源才能复用。模型只能使用已送达证据和本单合法目录。离线计分在回合结束后可以读取仿真状态。

[中文协作交接](COLLABORATION_zh.md)

时延分布附计分分母。每份方案均记录新鲜证据的到达时刻，包括保持配置的工单。覆盖率另计缺少证据、关窗后就绪、提案拒绝后保持及已核验配置。可达率、请求业务秒和保障比例逐项与区间账复核。
