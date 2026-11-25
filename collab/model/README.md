# 模型组 · 工具选择策略与统一回合接口

## 一、模块职责

模型组负责实现资源控制智能体（RC）的工具选择策略组件。在运行启用模型（`--policy model`）且当前工单需要选择工具时，组件根据已送达的因果观测 `observation`，从当次下发的合法工具列表 `legalActions` 中选定一个工具 ID 并返回对应元信息。

---

## 二、通用接口：启动初始化与单步输入

### 1. 启动工厂函数

组件模块须暴露可调用工厂函数（默认名 `build_model`，可通过配置中的 `model.entrypoint` 指定）：

```text
build_model(*, checkpoint, tokenizer, device, config) -> actor
```

| 参数 | 类型 | 含义 |
|---|---|---|
| `checkpoint` | `str \| Path \| None` | 命令行 `--checkpoint` 传入的权重目录或文件路径；无权重策略为 `None` |
| `tokenizer` | `str \| Path \| None` | 命令行 `--tokenizer` 传入的分词器目录或文件路径；不需要分词器时为 `None` |
| `device` | `str` | 目标执行设备标识（如 `"cpu"`、`"cuda"`），由 `--device` 传入 |
| `config` | `dict` | 当前 `model` 组件注册配置的独立副本，含 `id`、`version`、`entrypoint`、`catalogVersion`、`mode`、`symbols` 及可选扩展项 |

返回的策略对象 `actor` 必须实现 `choose(observation)`。若对象声明了 `symbols` 或 `identity["symbols"]`，须与 `config["symbols"]` 顺序一致；若声明了 `version`、`identity["version"]` 或 `identity["policyVersion"]`，须为非空字符串或非负整数且彼此一致。

### 2. 单步输入 `observation` 字段与单位

每次调用 `choose(observation)` 均传入一份已送达因果观测字典（完整样例见 [case.json](case.json)）：

| 字段 | 含义与单位 |
|---|---|
| `schema` | 观测格式标识，当前为 `"b03.packet.rc-choice.v1"` |
| `timeNs` | 当前决策时刻的整数仿真时间戳，单位为 **纳秒（ns）** |
| `legalActions` | 本次工单允许选择的有序工具 ID 列表（`model.symbols` 的保序子集）；返回值必须从中选取 |
| `actionCatalog` | 合法工具字典（键集与 `legalActions` 相同），每项含 `tool`、`scope`、`serviceSet`（对应 `authorizedServiceSets` 下标）、`pool`、`budget`、`stage` |
| `authorizedServiceSets` | 授权可写业务索引集合的列表，供 `actionCatalog[id]["serviceSet"]` 索引 |
| `budgets` | 本次工单下发的规则求解预算，含 `path_limit`、`path_expansion_budget`、`search_budget`、`search_service_limit` |
| `nodes` | 压缩表，列为 `id`、`online`，表示本帧观测到的节点在线状态 |
| `links` | 压缩表，列为 `id`、`src`、`dst`、`observedStatus`、`usable`、`availableWireBps`、`delayMs`、`jitterMs`、`ber`、`loss`、`observedCapacityBps`、`heldWireBps`；带宽与容量单位为 **bit/s**，时延与抖动单位为 **毫秒（ms）**，误码率与丢包率为小数比例 |
| `services` | 压缩表，列为 `id`、`src`、`dst`、`type`、`category`、`priority`、`payloadBps`、`maxDelayS`、`maxJitterS`、`maxBer`、`maxLoss`、`fresh`、`active`、`revision`、`diagnosis`、`writable`、`protected`；载荷速率单位为 **bit/s**，时延与抖动上限单位为 **秒（s）**，误码率与丢包率上限为小数比例 |
| `configs` | 压缩表，列为 `service`、`links`、`payloadBps`、`wireBps`、`epoch`、`verified`，表示已确认生效路径及预留带宽（**bit/s**） |
| `knowledgeContext` | 可选字段。仅当组件配置显式声明 `model.options.acceptKnowledge: true` 且 `knowledge.bindings.model` 已绑定查询时附加；默认不注入 |

**压缩表解码规则**：`nodes`、`links`、`services`、`configs` 均包含 `columns`、`defaults`、`rows`。每行格式为 `[entity_id, mask, *replacements]`：除首列实体 ID 外，其余列先取 `defaults` 默认值，再按整数 `mask` 的二进制位从低到高，将置 `1` 对应列依次替换为 `replacements` 中的覆盖值。可直接复用 [check.py](check.py) 中的 `decode(table)`。

---

## 三、两条运行分支：冻结推理与在线训练

组件在配置文件中通过 `model.mode` 选择运行分支：`frozen`（冻结推理）或 `online-ppo`（在线采样与训练）。

### 分支 A：冻结推理模式（`mode: "frozen"`）

- **调用时机**：启用模型且工单需要选择工具时，由 RC 调用 `actor.choose(observation)`（无需模型决策的工单不触发调用）。
- **返回格式**：返回二元组 `(legal_tool_id, meta)`，满足严格 JSON 序列化（禁止 `NaN` / `Infinity`）：
  ```text
  (legal_tool_id, {"policyVersion": <非空字符串或非负整数>})
  ```
- **行为约束**：
  1. 不得原地修改传入的 `observation`。
  2. 整轮评测中各次调用返回的 `policyVersion` 须与冻结策略声明版本一致。
  3. 不得在 `meta` 中覆写宿主保留字段（`method`、`modelCalled`、`selected`、`selectionWallNs`、`adapterId`、`adapterVersion`、`catalogVersion`、`entrypoint`、`mode`、`modelIO`、`legalActions`、`actionCatalog`；若包含 `observation`，其内容须与原输入完全一致）。

### 分支 B：在线采样与训练模式（`mode: "online-ppo"`）

1. **启动加载与状态钩子**
   - 除 `choose(observation)` 外，策略对象还须实现 `save_checkpoint(path)` 与 `restore_checkpoint(path)`。
   - 若命令行传入 `--checkpoint`，宿主在调用 `build_model(...)` 之后、执行工单之前，自动调用 `actor.restore_checkpoint(checkpoint)`。
   - 两个钩子仅持久化与恢复策略自身参数及优化状态，不得读写宿主控制状态或回执目录。

2. **单步采样输出**
   - `choose(observation)` 返回 `(legal_tool_id, meta)`，除遵守冻结模式的只读输入与保留字段约束外，`meta` 须提供以下字段：

   | 字段 | 要求 | 说明 |
   |---|---|---|
   | `policyVersion` | 必填 | 当前采样参数版本（非空字符串或非负整数） |
   | `behaviorProbabilities` | 必填 | 覆盖 `observation["legalActions"]` 的有限概率分布字典，各项在 `[0, 1]` 且总和与 `1.0` 误差 `< 1e-5` |
   | `behaviorProbability` | 必填 | 选中工具的实际采样正概率（`0 < p <= 1`），且与 `behaviorProbabilities[legal_tool_id]` 一致 |
   | `policyMode` | 可选 | 若提供须为 `"sample"` |
   | `legalProbabilities` | 可选 | 合法工具分布字典，须为 `legalActions` 上的有效概率分布 |
   | `oldLogProbability` | 可选 | 若提供须等于 `ln(behaviorProbability)` |
   | `sampleId` | 可选 | 采样记录标识，采样轨迹会原样透传 |

3. **统一回合采样与奖励反馈（20 / 100 节点通用）**
   - 通过 `episode.py sample` 或 Python 接口 `episode.sample(script, output, ...)` 运行授权训练脚本，输出 `schema: "b03.episode.sample.v1"` 的 JSON：
     - **回合级字段**：`schema`、`nodes`、`script`、`actorMode`、`verifiedCalls`、`archiveSha256`、`startNs`（计分窗起点）、`endNs`（计分窗终点）、`prefixUnweightedServiceNs`（首次有效决策前的未加权未保障业务纳秒数）、`unmetServiceNs`（整回合未保障业务总纳秒数）、`steps`。
     - **单步轨迹 `steps[i]`**：`observation`、`action`、`policyVersion`、组件实际记录的概率与采样字段（`behaviorProbability`、`behaviorProbabilities`、`oldLogProbability`、`sampleId` 按需透传）、`untilNs`（至下一次决策或计分窗终点时刻）、`rewardUnweightedServiceNs`（该区间负未加权未保障业务纳秒数；同时刻连续决策可出现 `0`）、`terminal`（是否为末步）。
     - **奖励守恒**：`prefixUnweightedServiceNs - sum(step["rewardUnweightedServiceNs"]) == unmetServiceNs`，除以 `1e9` 与离线核验的 `U` 精确一致。
   - **训练脚本白名单与评价分离**：20 节点允许 `01 / 02 / 04 / 05`，100 节点允许 `04 / 05`，支持 `--services`、`--start`、`--end`、`--drain` 指定训练窗口。无权重 `U` 用于基准训练；加权指标 `W` 与五项赛题指标通过 `episode.py evaluate`（返回 `schema: "b03.episode.evaluation.v1"`，含 `business`、`competitionMetrics`、`toolChoices`、`modelCalls`、`readbacks`、`solverWallNs`、`selectionWallNs`）独立评价。
   - **训练器分工**：模型组可使用任意自研或第三方训练器消费 `episode.py sample` 输出并迭代权重。仓内 [train.py](../../train.py) 仅作为 100 节点 `v374` 的内置在线 PPO 参考实现，详见 [docs/MODEL.md](../../docs/MODEL.md)。

---

## 四、配置替换与仓内完整实现索引

接入自研策略时，复制 [config/collab-selector.json](../../config/collab-selector.json) 并修改 `model` 字段即可在启动时替换组件：

```json
{
  "extends": "components.json",
  "model": {
    "id": "example.standalone-selector",
    "version": "1.0.0",
    "entrypoint": "standalone_selector:build_model",
    "mode": "frozen",
    "symbols": ["A", "B", "C"]
  }
}
```

- `extends`：继承默认组件清单 [config/components.json](../../config/components.json)。
- `id` / `version`：组件唯一标识与版本号（写入运行回执）。
- `entrypoint`：可导入的 `模块路径:工厂函数名`。
- `mode`：运行分支，取 `"frozen"` 或 `"online-ppo"`。
- `symbols`：组件支持的工具 ID 有序列表，须为已登记目录的子集。
- `options.acceptKnowledge`（可选）：设为 `true` 且配置 `knowledge.bindings.model` 时接收 `observation["knowledgeContext"]`。外部依赖包与资产登记见 [docs/MODEL.md](../../docs/MODEL.md) 与 [docs/PROVENANCE.md](../../docs/PROVENANCE.md)。

### 仓内完整实现索引

具体实现代码不赘述于本文，可按需直接查阅仓内参考源码与配套配置：

| 用途与模式 | 参考源码 | 配套配置 |
|---|---|---|
| 纯标准库冻结策略样例（`frozen`） | [standalone_selector.py](../../src/standalone_selector.py) | [collab-selector.json](../../config/collab-selector.json) |
| 冻结策略外层封装样例（`frozen`） | [example_model.py](../../src/example_model.py) | [example-model.json](../../config/example-model.json) |
| 内置参考冻结模型推理（`frozen`） | [frozen_policy.py](../../src/frozen_policy.py) | [components.json](../../config/components.json) / [model.json](../../config/model.json) |
| CPU 动态 INT8 推理实现（`frozen`） | [int8_model.py](../../src/int8_model.py) | [int8-model.json](../../config/int8-model.json) |
| 在线 PPO 采样与参数更新参考实现（`online-ppo`） | [packet_learner.py](../../src/packet_learner.py) | [packet-ppo.json](../../config/packet-ppo.json) |

---

## 五、离线自测与回合采样 / 评测命令

### 1. 本地离线自测（仅需 Python 3.10+ 标准库，不依赖赛事数据）

在仓库根目录执行，使用 [case.json](case.json) 与 [check.py](check.py) 验证接口格式、只读约束及非法输出拦截：

```sh
python collab/model/check.py
python collab/model/check.py --adapter path/to/your_actor.py --factory build_model
```

### 2. 真实工单采样与独立评测（需配置基础依赖与 `B03_DATA`）

```sh
# 1. 授权训练脚本采样并导出轨迹（20 / 100 节点统一入口）
python episode.py sample --script 04 --components config/collab-selector.json --output runs/sample > transitions.json
python episode.py evaluate runs/sample > evaluation.json

# 2. 开发集完整工单运行与严格核验
python run.py run --policy model --components config/collab-selector.json --output runs/model-dev
python run.py verify runs/model-dev --strict-source --components config/collab-selector.json
```
