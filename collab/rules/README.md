# 规则组 · 纯数据配置提案与差量求解接口

## 一、模块职责

规则组负责实现资源控制智能体（RC）的路径与带宽求解器组件（契约版本 `b03.rules.patch.v2`）。在工单决策流程中，组件基于纯数据网络快照、授权范围、资源预算与可选知识提示，先宣告可受理的工具 ID，再在工具被选中后输出仅含变更业务的差量配置补丁 `patch` 及算法审计信息。

---

## 二、通用接口：启动初始化与输入字段

### 1. 启动初始化函数

组件模块须提供初始化入口函数（默认名 `build_rules`，通过配置 `rules.entrypoint` 指定）：

```text
build_rules(*, config) -> provider
```

| 参数 | 类型 | 含义 |
|---|---|---|
| `config` | `dict` | 当前 `rules` 组件注册配置的独立副本，含 `id`、`version`、`entrypoint`、`contractVersion`、`catalogVersion` 及可选 `options` |

返回的 `provider` 对象须实现 `enumerate_actions(request)` 与 `solve_action(request)`。宿主每次调用均传入独立深拷贝的 JSON 数据，组件不得依赖跨调用共享的可变输入。

### 2. 请求输入字段与单位

完整输入与输出样例见 [case.json](case.json)：

| 字段 | 适用阶段 | 含义与单位 |
|---|---|---|
| `schema`、`work` | 通用 | 契约标识 `"b03.rules.patch.v2"` 与当前工单 ID |
| `snapshot.nodes` | 通用 | 节点在线状态字典：`{nodeId: {"_online": bool}}` |
| `snapshot.links` | 通用 | 链路状态与参数字典，含 `srcNodeId`、`dstNodeId`、`_online` 及 `params`：`bandwidthBps` 为可分配线路带宽（**bit/s**，已扣除控制余量与快照外预留），`delayMs`、`jitterMs` 单位为 **毫秒（ms）**，`ber`、`packetLossRate` 为小数比例 |
| `snapshot.requests` | 通用 | 业务需求字典，含 `serviceId`、`srcNodeId`、`dstNodeId`、`priority`、`category`、`serviceType`、`requiredPayloadBps`（原载荷速率，**bit/s**）、`_deleted` 及 `requirement`（线路速率 `bandwidthBps` 为 **bit/s**，时延/抖动上限为 **ms**，误码/丢包上限为小数比例） |
| `snapshot.assignments` | 通用 | 当前已确认设备配置 `{serviceId: {"nodes", "linkIds", "allocatedBps"} \| null}`；已有路径仍占用对应链路容量 |
| `snapshot` 版本字段 | 通用 | `observedAtSec`、`stateVersion`、`configVersion`、`objectiveVersion` |
| `authority` | 通用 | 授权边界，含 `policyId`、`policyVersion`、`readableServiceIds`、`executableServiceIds`、`frozenHealthyServiceIds`、`protectedServiceIds`、`allowedMethods` |
| `budgets` | 通用 | 输出硬约束 `maxHops`、`maxChangedServices`（及可选实际运行耗时上限 `maxSolverWallMs`），以及参考搜索工作量参数 `path_limit`、`path_expansion_budget`、`search_budget`、`search_service_limit` |
| `candidates` | 仅 `enumerate_actions` | 本单已授权候选工具列表，每项含 `id`、`tool`、`scope`（`kind`、`serviceIds`）、`stage`、`pool`、`budget` |
| `action` | 仅 `solve_action` | 本次选中的工具描述对象（结构同 `candidates` 单项） |
| `conditionalReservations` | 仅 `solve_action`（可选） | 新需求修订到达后，旧配置版本（`epoch`）仍占用的路径与线路速率字典；须显式释放或替换后方可复用其链路容量 |
| `knowledgeContext` | 仅 `solve_action`（可选） | 当所选工具由本插件处理、配置声明 `rules.options.acceptKnowledge: true` 且 `knowledge.bindings.rules` 已绑定查询时，按检索顺序传入本版知识条目列表 |

**路径 QoS 与容量聚合规则**：端到端时延线性相加；抖动取平方和开方；误码率线性相加并以 `1` 封顶；端到端丢包率为 `1 - prod(1 - packetLossRate)`。容量方面，正式提案校验允许保持或减轻链路已有超载（例如链路降容导致受保护业务已占带宽高于新容量时保持原配置），但禁止新增或加重超载；离线样例检查器 `check.py` 对总占用采用更严格的不超过 `params.bandwidthBps` 校验。

---

## 三、两个调用阶段：候选宣告与差量求解

### 阶段 A：候选工具宣告 `enumerate_actions(request) -> list[str]`

- **输入**：包含 `candidates`、`snapshot`、`authority`、`budgets` 的请求字典。
- **返回**：本组件可受理的去重工具 ID 列表（`list[str]`），所有元素须出自 `request["candidates"]` 中的 `id`，随后由宿主纳入合法目录进入策略选择。

### 阶段 B：差量配置求解 `solve_action(request) -> dict`

- **返回格式**：严格可 JSON 序列化的字典（禁止 `NaN` / `Infinity`，顶层键仅允许 `patch` 与可选 `audit`）：
  ```text
  {
    "patch": {
      "<serviceId>": {"nodes": [...], "linkIds": [...], "allocatedBps": <requirement.bandwidthBps>} | null
    },
    "audit": {
      "usedKnowledge": ["<entryId>", ...],
      ...
    }
  }
  ```
- **差量语义与校验约束**：
  1. **省略即保持，`null` 为释放**：未出现在 `patch` 中的业务维持当前设备配置；值为 `null` 表示申请释放（业务到期由现有生命周期工单释放）。
  2. **写权限边界**：`patch` 的键必须属于 `action.scope.serviceIds` 与 `authority.executableServiceIds` 的交集，且排除 `frozenHealthyServiceIds` 与 `protectedServiceIds`，不得增删业务列表。
  3. **路径与速率约束**：每条非空路径须为无环连通路径（首尾分别等于业务源/宿节点、`len(nodes) == len(linkIds) + 1`、沿途节点与链路均在线、跳数 `<= budgets.maxHops`），且 `allocatedBps` 须严格等于原需求 `requirement.bandwidthBps`。
  4. **变更预算与耗时**：实际变更业务数量不得超过 `budgets.maxChangedServices`；若为工具配置了 `maxSolverWallMs`，主控在返回时检查实际运行耗时，并统一将求解耗时计入业务时钟。
  5. **审计与复核**：`audit.usedKnowledge` 若提供，须为去重列表且仅引用本次传入的 `knowledgeContext` 中的条目 ID；其余审计计数字段可按算法实际工作量自报。主控补齐未变业务后统一复核五项 QoS、共享容量与设备下发顺序。

---

## 四、配置替换、工具登记与仓内实现索引

### 1. 注册配置结构

复制 [config/collab-rules.json](../../config/collab-rules.json) 并修改 `rules` 与 `catalog` 字段即可在启动时替换规则组件：

```json
{
  "extends": "components.json",
  "catalog": {
    "version": "collab.ranked-route.v2",
    "symbols": ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "X"]
  },
  "model": { "catalogVersion": "collab.ranked-route.v2" },
  "rules": {
    "id": "example.patch-router",
    "version": "2.0.0",
    "entrypoint": "collab_patch_rule:build_rules",
    "contractVersion": "b03.rules.patch.v2",
    "catalogVersion": "collab.ranked-route.v2",
    "options": {
      "customTools": {
        "X": {
          "tool": "ranked_feasible_path",
          "scope": "writable_active",
          "permit": "priority_greedy",
          "pool": "reported_neighbors",
          "budget": "workorder"
        }
      },
      "acceptKnowledge": true
    }
  }
}
```

- **新增工具 ID**：`extends` 会整体替换数组，因此须在 `catalog.symbols` 中完整保留原有工具目录（`A–L`）并追加新 ID（整段配置与 [config/collab-rules.json](../../config/collab-rules.json) 一致），同时在 `rules.options.customTools` 登记新 ID（声明 `tool`、`scope` 取 `"writable_active"` 或 `"retain"`、`permit` 取原生已授权方法、`pool`、`budget`）并使用独立 `catalog.version`（不可复用 `b03.tools.v1`）。若需模型调用新 ID，须同步更新并验证模型映射。
- **替换既有 ID 算法**：除声明独立 `catalogVersion` 外，须在 `rules.options.providedNative` 中列出由本插件接管的既有工具 ID。
- **可选命名输出预算**：可在 `rules.options.outputBudgets` 声明命名限制（如 `{"bounded": {"maxSolverWallMs": 800, "maxHops": 6, "maxChangedServices": 3}}`），并让工具注册的 `budget` 指向该名称。
- 独立依赖与资产登记见 [docs/ASSETS.md](../../docs/ASSETS.md)、[docs/PROVENANCE.md](../../docs/PROVENANCE.md) 及完整规则契约 [docs/RULES.md](../../docs/RULES.md)。

### 2. 仓内完整实现索引

| 用途与模式 | 参考源码 | 配套配置 |
|---|---|---|
| 纯标准库差量求解参考实现（`b03.rules.patch.v2`） | [collab_patch_rule.py](../../src/collab_patch_rule.py) | [collab-rules.json](../../config/collab-rules.json) |
| 差量规则 + 知识提示（优先时延）联调 | [collab_patch_rule.py](../../src/collab_patch_rule.py) | [collab-rules-knowledge.json](../../config/collab-rules-knowledge.json) |
| 差量规则 + 知识提示（优先跳数）对照 | [collab_patch_rule.py](../../src/collab_patch_rule.py) | [collab-ranked-hops.json](../../config/collab-ranked-hops.json) |
| 独立工具 `M` 规则扩展示例 | [branch_rules.py](../../src/branch_rules.py) | [branch-rules.json](../../config/branch-rules.json) / [branch-rules-knowledge.json](../../config/branch-rules-knowledge.json) |
| 原生规则外层封装示例 | [example_rules.py](../../src/example_rules.py) | [example-rules.json](../../config/example-rules.json) |

---

## 五、离线自测与真实工单联调命令

### 1. 本地离线自测（仅需 Python 3.10+ 标准库，不依赖赛事数据）

在仓库根目录执行，使用 [case.json](case.json) 与 [check.py](check.py) 验证候选宣告、差量输出、QoS/容量约束及知识排序响应。需要在仓库外运行时，用 `--export-kit` 导出自测脚本、样例、参考规则和锁定公式到一个新目录，再从该目录运行相同命令。

```sh
python -I collab/rules/check.py
python -I collab/rules/check.py --provider path/to/your_rule.py --registration path/to/registration.json
python -I collab/rules/check.py --export-kit ./rule-kit
```

### 2. 真实工单运行与严格核验（需配置基础依赖与 `B03_DATA`）

```sh
python run.py run --policy X --components config/collab-rules-knowledge.json --output runs/rule-x
python run.py verify runs/rule-x --components config/collab-rules-knowledge.json --strict-source
```

在同一授权工单上，可分别使用 `config/collab-rules.json`（默认排序）、`config/collab-rules-knowledge.json`（时延优先）与 `config/collab-ranked-hops.json`（跳数优先）对照效果；回执 `solver_audit.rulePatch` 记录原请求、实际补丁、审计字段及实测耗时。
