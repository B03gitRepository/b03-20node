# 知识库组 · 版本化只读检索与多入口挂载接口

## 一、模块职责与接口定位

知识库组负责提供版本化、只读的技术知识检索组件（契约版本 `b03.knowledge.lookup.v2`）。组件基于锁定的知识版实现按编号精确查找或按主题自主召回与排序；宿主按配置将检索结果组装后注入 OM、规则或模型环节，知识条目不改变写权限或原始 QoS 需求。

此外，链路共用的底层计算口径单独维护在[底层计算公式知识版](../../knowledge/formulas-v1.json)中（见第三节第 3 点），便于统一核查与全链路直接调用。

### 当前实际启用状态与能力边界

为方便知识库组判断新想法是否可直接基于现有接口落地、还是需要联系系统集成组扩展接口，当前边界明确如下：

- **默认配置（`config/components.json`）下的真实启用状态**：
  - 业务知识检索中，目前**只有“按编号精确查找”在首张授权工单后实际触发一次**（固定查询 `K044` 用于首单残余准入核查）；
  - 默认配置未开启主题检索，也未绑定 `om`、`rules`、`model`。`config/collab-rules-knowledge.json` 在规则环节用 `detour guidance` 演示路径排序。[三环节联调配置](../../config/collab-opt-in.json)把同一主题的检索结果交给 OM、规则和示例模型，20 与 100 节点工单均已核验这三个环节的回执。
  - 换言之，当前先以统一接口和核验骨架为主；各组明确具体业务知识需求后，若超出下表现有范围，再由系统集成组协同扩展接口。

| 分类 | 具体能力 | 说明 |
|---|---|---|
| **现有接口可直接支持**（无需改主控接口，现在即可开发与对比） | 扩充文本类技术/运维/策略知识库 | 源 JSON 的每条记录填写 `id`、`topics`、`content`、`sources`、`applicability`。宿主在查询结果中加入知识版的 `version`，共 6 个字段 |
| **现有接口可直接支持** | 替换检索与排序算法 | 在 `lookup` 中自研关键词、向量检索、重排或外部检索服务，返回排序后的本版 ID 列表 |
| **现有接口可直接支持** | 按固定主题/编号定点注入 | 通过 `knowledge.bindings` 指定接入环节。OM 按绑定接收，规则与模型插件的接收条件见第三节 |
| **现有接口可直接支持** | 核查与对齐底层计算公式 | 查阅并核对 `knowledge/formulas-v1.json` 中的 11 项默认计算口径 |
| **超出当前范围**（需求明确后需找系统集成组扩接口） | 按当前工单故障/拓扑动态查询 | 当前 `lookup` 仅接收配置文件写死的单个 `topic` 或 `identifier`，不传入实时网络状态或告警上下文 |
| **超出当前范围** | 智能体自主决定何时查、查什么 | 当前由宿主按配置在固定时机注入，尚不支持 OM 或模型在决策过程中自主发起多轮提问 |
| **超出当前范围** | 透传结构化数值/特征/权重载荷 | 宿主从源条目提取上述 5 项，再加入版本。查询结果只交付这 6 项 |
| **长期架构底线**（不开放） | 改写写权限、原始 QoS 或强制下发 | 知识库始终为只读参考，不参与写权限签发与设备直接写入 |

---

## 二、通用接口：启动初始化与知识版格式

### 1. 启动初始化函数

组件模块须提供初始化入口函数（默认名 `build_knowledge`，通过配置 `knowledge.entrypoint` 指定）：

```text
build_knowledge(*, config, library) -> provider
```

| 参数 | 类型 | 含义 |
|---|---|---|
| `config` | `dict` | 当前 `knowledge` 组件注册配置副本，含 `id`、`version`、`entrypoint` 及可选 `options` |
| `library` | `dict` | 所选版本化知识 JSON 的独立深拷贝（完整样例见 [case.json](case.json)） |

返回的 `provider` 对象须实现 `lookup(*, identifier=None, topic=None)`。在 `b03.knowledge.lookup.v2` 契约下，同一进程内沿用本版缓存索引；更换知识版时须同步更新配置中的 `source` 与 `version`。

### 2. 知识版 `library` 数据结构

| 层级 | 字段 | 要求与说明 |
|---|---|---|
| 顶层 | `schema`、`version`、`entries` | `schema` 固定为 `"b03.knowledge.v1"`；`version` 须与配置 `knowledge.version` 一致；`entries` 为条目列表 |
| 条目项 | `id` | 本版唯一非空字符串标识 |
| 条目项 | `topics` | 非空主题词字符串列表 |
| 条目项 | `content` | 非空知识正文 |
| 条目项 | `sources` | 非空来源说明列表 |
| 条目项 | `applicability` | 非空适用条件说明 |

---

## 三、两条查询分支、三个接入环节与底层公式知识版

### 1. `lookup(*, identifier=None, topic=None) -> list[str]` 两条查询分支

每次调用 `identifier` 与 `topic` 严格二选一（传入非空字符串，另一参数为 `None`）：

| 查询分支 | 输入参数 | 组件返回值要求 | 宿主组装与交付内容 | 当前应用状态 |
|---|---|---|---|---|
| **分支 A：按编号精确查找** | `identifier="<ID>"`, `topic=None` | 本版存在该编号时返回 `[identifier]`，否则返回 `[]` | 返回对应单条 6 字段条目（含 `id`、`topics`、`content`、`sources`、`applicability`、`version`）；知识版摘要记录于工单回执 `knowledge_contexts[port].component.sourceSha256` | 默认运行仅在首单残余准入核查时点名查询一次 `K044` |
| **分支 B：按主题召回与排序** | `identifier=None`, `topic="<查询词>"` | 自主召回并排序后的本版唯一 ID 列表（允许 `[]`；可接入关键词、向量检索或外部服务） | 严格按返回 ID 顺序组装同版 6 字段条目（含 `version`）；知识版摘要记录于工单回执 `knowledge_contexts[port].component.sourceSha256` | 默认关闭。规则示例用该分支比较路径排序，三环节联调示例也用它向 OM、规则和模型交付结果 |

### 2. 三个接入环节挂载规则（`knowledge.bindings`）

通过配置中的 `knowledge.bindings` 分别声明 `om`、`rules`、`model` 入口，每项填写 `{"topic": "<查询词>"}` 或 `{"identifier": "<条目ID>"}`。关闭单个入口须直接省略该键（填入 `{}` 或 `null` 均会报错）；关闭全部入口可配置 `"bindings": {}`，未启用入口沿用既有流程：

| 接入环节 | 触发时机 | 注入位置 | 接入方前置开关与边界 | 当前内容状态 |
|---|---|---|---|---|
| `om` | OM 完成权限诊断后、将工单交给 RC 前 | `diagnosis.knowledgeContext` | 配置 `knowledge.bindings.om` 即注入；授权范围与原始 QoS 仍按已送达观测独立计算 | 默认关闭。三环节联调示例在 OM 的诊断记录中保存检索结果 |
| `rules` | 规则求解调用 `solve_action` 前 | `request["knowledgeContext"]` | 所选工具由 v2 规则插件处理且声明 `rules.options.acceptKnowledge: true` | 默认未开启；示例配置含 `EX.PATH.DELAY` / `EX.PATH.HOPS` 排序演示 |
| `model` | RC 构造模型因果观测时 | `observation["knowledgeContext"]` | 模型插件须显式声明 `model.options.acceptKnowledge: true`；内置冻结模型保持原始输入不变 | 默认关闭。三环节联调示例把检索结果交给模型插件，并记录在模型观测中 |

各入口的查询请求、排序结果、知识版来源、绑定快照与耗时记录在工单 `knowledge_contexts` 中；首单残余准入核查（`K044`）继续保留独立记录 `knowledge_io`。离线 `run.py verify` 会按归档知识版逐项复核条目内容、授权快照与注入位置。

### 3. 底层计算公式知识版（存放位置与单列原因）

单位换算、封包速率折算、路径 4 维 QoS 合成、链路容量与超载守卫、双端观测融合、`U/W` 与五项赛题指标、规则目标函数等 11 项规范口径，独立维护于：

- **公式声明与参数**：[knowledge/formulas-v1.json](../../knowledge/formulas-v1.json)（只存结构化声明、单位、参数与适用条件，不含可执行代码）
- **统一计算实现**：[src/b03_formulas.py](../../src/b03_formulas.py)（启动时读取并校验上述 JSON，供主控、规则、计量与核验模块统一调用）
- **版本登记与扩展说明**：[docs/KNOWLEDGE.md](../../docs/KNOWLEDGE.md)

**为什么将底层计算公式单列，而不并入 `om` / `rules` / `model` 三个可选接入环节**：

1. **全链路必选且须强一致**：底层公式决定构图单位、QoS 判定、容量约束与评分核验的客观口径，所有模块默认必须执行同一套标准，不能像业务策略建议那样按配置开关或仅在个别环节生效。
2. **高频计算性能与确定性**：求解与计量在单次工单中会被调用数千至上万次；单列后系统可在启动阶段一次性校验摘要并绑定内存计算函数，热循环零 JSON 检索开销。
3. **独立版本与回执溯源**：公式版随每次工单独立归档声明与生效参数（`formula-edition.json`），与业务经验知识（`technical-v1.json`、`collab-routing.json`）解耦，方便知识库组与系统集成组各自独立演进和审计。

---

## 四、配置替换与仓内完整实现索引

### 1. 注册配置结构

复制 [config/collab-rules-knowledge.json](../../config/collab-rules-knowledge.json) 并修改 `knowledge` 字段即可在启动时替换检索器、知识版与挂载入口：

```json
{
  "extends": "collab-rules.json",
  "knowledge": {
    "id": "example.ranked-knowledge",
    "version": "2.0.0",
    "entrypoint": "collab_ranked_knowledge:build_knowledge",
    "contractVersion": "b03.knowledge.lookup.v2",
    "source": "../knowledge/collab-routing.json",
    "bindings": {
      "rules": { "topic": "detour guidance" }
    },
    "options": { "rankFirst": "EX.PATH.DELAY" }
  }
}
```

- `source` / `version`：绑定版本化知识 JSON 路径及其 `version` 字段。
- `bindings`：按需启用 `om`、`rules`、`model` 中任意一个或多个入口。
- `options`：透传给 `build_knowledge` 的自定义参数。独立安装包或外部依赖登记见 [docs/ASSETS.md](../../docs/ASSETS.md)、[docs/PROVENANCE.md](../../docs/PROVENANCE.md) 及完整契约 [docs/KNOWLEDGE.md](../../docs/KNOWLEDGE.md)。

### 2. 仓内完整实现与知识版索引

| 用途与模式 | 参考源码 / 知识版 | 配套配置 / 绑定代码 |
|---|---|---|
| 底层计算公式知识版（11 项默认规范口径） | [formulas-v1.json](../../knowledge/formulas-v1.json) | [b03_formulas.py](../../src/b03_formulas.py)（全链路默认绑定） |
| 主题召回与可调排序参考实现（`b03.knowledge.lookup.v2`） | [collab_ranked_knowledge.py](../../src/collab_ranked_knowledge.py) | [collab-rules-knowledge.json](../../config/collab-rules-knowledge.json) / [collab-ranked-hops.json](../../config/collab-ranked-hops.json) |
| 协作路径排序示例知识版（`2.0.0`） | [collab-routing.json](../../knowledge/collab-routing.json) | [collab-rules-knowledge.json](../../config/collab-rules-knowledge.json) |
| 默认技术知识版（v1 / v2） | [technical-v1.json](../../knowledge/technical-v1.json) / [technical-v2.json](../../knowledge/technical-v2.json) | [components.json](../../config/components.json) / [knowledge-v2.json](../../config/knowledge-v2.json) |
| 轻量检索封装示例 | [example_knowledge.py](../../src/example_knowledge.py) | [example-knowledge.json](../../config/example-knowledge.json) |

---

## 五、离线自测与真实工单联调命令

### 1. 本地离线自测（仅需 Python 3.10+ 标准库，不依赖赛事数据）

在仓库根目录执行，使用 [case.json](case.json) 与 [check.py](check.py) 验证精确编号查询、主题召回排序及条目合法性：

```sh
python -I collab/knowledge/check.py
python -I collab/knowledge/check.py --provider path/to/your_knowledge.py --factory build_knowledge
```

### 2. 独立检索与真实工单联调（需配置基础依赖与 `B03_DATA`）

```sh
# 1. 查询底层计算公式知识版（按主题或编号）
python run.py knowledge --formulas --topic 容量
python run.py knowledge --formulas --id packet.wire-rate

# 2. 单独验证业务知识主题召回与排序结果
python run.py knowledge --components config/collab-rules-knowledge.json --topic "detour guidance"

# 3. 接入真实工单并严格核验（可切换 config/collab-ranked-hops.json 对比排序差异）
python run.py run --policy X --components config/collab-rules-knowledge.json --output runs/ranked
python run.py verify runs/ranked --components config/collab-rules-knowledge.json --strict-source
```
