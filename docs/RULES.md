# 替换调度规则求解器

v2 契约、可运行样例及独立测试见[规则组开工手册](../collab/rules/README.md)。本页同时介绍仍可使用的原插件登记方式。

规则组件接收基于已送达证据的 OM/RC 工单，返回配置提案。分组控制器校验提案、下发设备指令并核验回执。默认清单使用仓内已验收工具。下面的示例保持相同工具目录及固定 I 选择，只切换规则组件。

按仓库首页准备 Python 环境、仿真器与官方数据，在仓库根目录执行。每次使用新输出目录。

```bash
python run.py run --policy I --components config/example-rules.json --record-component-io --output runs/residual-example
python run.py verify runs/residual-example
python run.py examples runs/residual-example --components config/example-rules.json --output residual-example.json
```

`src/example_rules.py` 调整 I 的有界成对搜索。在同一原始优先级内先考虑可行路径较少的业务。先前分配占用容量后，按预计链路利用率给候选路径排序，再使用 I 原有的单业务及双业务邻域、路径数量和评估上限。其他符号沿用原求解器。源码回归例给一项业务两条可行并行路径，原 I 选时延较低的链路，示例选剩余容量较多的链路。

## 提供方接口

在继承 `config/components.json` 的清单中设置 `rules.id`、`rules.version` 和 `rules.entrypoint`。示例使用 `example_rules:build_rules`。运行器导入受信任的本地 Python 模块，为每个决策器调用一次 `build_rules(*, config)`。工厂接收解析后的规则描述及 `options`，返回的对象提供以下两个方法。

| 方法 | 输入与返回值 |
| --- | --- |
| `enumerate_actions(snapshot, authority, config, history=None)` | 枚举可用的结构化动作，逐项声明登记 ID、方法、含业务 ID 的作用域、候选池、预算和阶段。控制器使用本次已送达报告及权限构造动作目录并核对这些字段。 |
| `solve_action(action, bound, context=None)` | 返回 `(target, audit)`。`bound['snapshot']['requests']` 中每项业务均须出现，取值为 `None` 或含有 `nodes`、`linkIds`、`allocatedBps` 的路径。以 `rule_component.bind_proposal(action, bound, target, audit)` 将结果绑定于本工单、快照、权限、预算配置、所选动作及目标。 |

`bound` 携带 `identity`、`snapshot`、`authority` 和 `config`。快照包含已观测节点和链路、请求、当前分配及观测时刻。分配单位为线速 bit/s，每项请求还保留原始有效载荷速率。返回完整所需线速并满足时延、抖动、BER、丢包约束。可用容量已扣留既有预留和控制余量。健康及受保护业务保持原配置，只能更改所选工具获准写入的业务。提供方可实现工具目录的子集。使用固定或组合策略时应提供该策略在活动工单中选择的符号。

控制器传递隔离的输入，为每个决策器保存私有求解上下文。枚举时提供上下文副本。求解器可以跨工单缓存路径。控制器依据本次快照独立核验业务名单、可写范围、路径契约、变更预算及声明的搜索次数。设备下发前仍执行初配准入与原生分组 QoS 检查。

公开目录 `b03.tools.v1` 定义 A–L。为已授权原生模板增加别名时，应在 `catalog.symbols` 登记新符号、提升目录版本并在 `rules.options.toolAliases` 声明映射，例如 `{"M":"I"}`。模型与规则使用同一新版目录。需要模型选择时，将符号纳入模型映射。别名沿用原模板的授权方法、作用域、候选池和预算。

## 登记独立规则

M 示例由规则提供方实现确定性选路。优先队列先取剩余瓶颈容量较大的可用链路，以时延和链路序列打破并列。每接入一项业务先扣除其预留，再考虑下一项获准业务，并检查原带宽、时延、抖动、BER、丢包和跳数限制。控制器校验、分组安装与逐跳读回沿用默认接口。[求解器源码](../src/branch_rules.py) · [登记清单](../config/branch-rules.json)

```bash
python run.py run --components config/branch-rules.json --policy M --record-component-io --output runs/branch-m
python run.py verify runs/branch-m --strict-source --components config/branch-rules.json --report runs/branch-check.json
python run.py examples runs/branch-m --output runs/branch-io.json
```

组件负责人实现 `build_rules(*, config)` 和上述两个方法。联调负责人在新版目录中登记额外符号，同步模型与规则的目录版本，保留既有模型符号顺序。以下为示例加入 `rules.options` 的声明。

```json
{
  "customTools": {
    "M": {
      "template": "L",
      "tool": "widest_residual",
      "pool": "delivered_neighbors",
      "budget": "medium"
    }
  }
}
```

控制器根据模板 L 和 OM 已送达的授权推导 M 的可写业务，登记项固定方法、候选池及模板的搜索预算。只有当前快照允许 L 时才能提供 M。注册自定义工具后，控制器继续用原实现求解已登记的 A–L。示例模型保持训练时的符号映射，可用 `--policy M` 单独运行新规则。

M 返回完整业务名单与绑定审计。作用域内每项活动业务以自身 ID 为键，报告整数 `pathSearch.expanded`、`pathSearch.selectedCandidates`，空结果也填零。示例每项业务最多扩展 8,000 次并选定一条路径。提供方收到隔离的输入与私有上下文副本。提案和分组检查全部通过后才沿用该上下文。控制器核验搜索次数，对实际返回配置检查权限、原始线速及物理约束，并测量调用耗时。

从归档样例复跑 M 时，在仓库根目录使用同一规则依赖环境执行下面的代码。

```python
import json
import sys
from pathlib import Path

sys.path[:0] = ["src", "src/rules", "src/rules/vendor/src"]
from component_config import load_components
from rule_component import CheckedRules

sample = json.loads(Path("runs/branch-io.json").read_bytes())["rules"]
components = load_components("config/branch-rules.json").activate("M")
provider = components.factory("rules")(config=components.rules)
rules = CheckedRules(provider, components.rules, components.symbols, sample["context"])
catalog = rules.catalog(sample["bound"], {})
assert catalog == sample["catalog"]
target, audit, checked = rules.solve(sample["action"], [a["id"] for a in catalog], sample["bound"], {})
assert checked["valid"] and {"target": target, "audit": audit} == sample["returned"]
```

`config/branch-rules-knowledge.json` 同时选择这项规则和索引知识版本。可用 `config/branch-rules.json` 单独切换 M，或用 `config/example-knowledge.json` 在原固定 I 上单独切换知识提供方。[知识查询](KNOWLEDGE.md)

## 到期释放与重新准入

送达的需求报告表明可写业务已停止，且设备证据确认旧配置代次时，OM 开释放工单。未核验配置先只读刷新。健康、受保护和白名单以外的业务保持配置。到期和其他活动工单重合时，`lifecycle_release.release_ids` 标出待退役配置，`deferred_active_ids` 标出释放后需重新考虑的写入。

RC 直接生成释放计划。回执中 `tool_choice.method="lifecycle"`、`selected=null`、`modelCalled=false`、`real_solver_calls=0`。实测 `lifecycle_wall_ns` 计入 `decision_wall_ns`。设备环在停用前核对送达的修订号，经过旧规则保留期，向路径全部节点下发清理，再独立查询各节点。最后一份匹配查询回到控制端前，旧配置继续占用线速。OM 核验回执后，下一张工单以 `reassessment_after_release` 关联签收。空闲决策也保留送达快照，并由配置的策略重新评估当前需求。

## 搜索与变更预算

在 `b03.tools.v1` 中，I 使用 `src/rules/strong_tools.py` 的 `pair_local`、`low_overlap` 候选池及 `medium` 预算。示例保留以下上限。

| 操作 | 上限 |
| --- | ---: |
| 每项业务的路径扩展次数 | 8,000 |
| 每项业务向生成器请求的路径数 | 32 |
| 每项业务保留的候选路径数 | 8 |
| 每次局部变更的业务数 | 2 |
| 全部局部搜索的试配次数 | 4,000 |
| 局部搜索轮数 | 3 |

当前路径有效时，可进入八条路径的候选筛选。设置 `config.max_path_hops` 可限制新路径跳数。`config.t096_max_changed_services` 限制包括释放在内的总变更数，默认取可执行业务数。提供方报告 `localEvaluations`、`passes`、`maxChangedServices` 及逐业务 `pathSearch`。控制器依据实际配置核对变更数和预算计数，各受信任的 Python 提供方负责执行自己的搜索上限。

## 工单证据与计时

`--record-component-io` 在 `b03_rc_plan.component_io` 保存实际动作、目录、已绑定输入、调用前上下文及原始目标与审计。该返回值记录在初配准入与分组 QoS 核验前，同一条记录中的求解审计及设备计划显示后续修正。`examples` 从已核验工单提取样例。

对外部求解器，从绑定工单到最终计划组装的 RC 耗时计入下发时钟，可选样例复制耗时单列。`decision_wall_ns` 等于 `tool_choice.selectionWallNs + solver_wall_ns + component_timing.chargedComponentWallNs`。`solver_wall_ns` 含求解器调用和初配准入。其余计费含准备、目录枚举与核验、输入隔离、独立提案核验及最终计划检查。`component_timing` 另列 `setupWallNs`、`catalogHookWallNs`、`catalogGuardWallNs`、`solverHookWallNs`、`proposalGuardWallNs`、`postProposalWallNs`，其中时间已包含在计费总量内。`sampleCopyWallNs` 在下发时钟外记录，提案返回后才进行传输和日志写入。

内置求解器沿用 `decision_wall_ns = tool_choice.selectionWallNs + solver_wall_ns`。外部求解器的 `setupWallNs` 含绑定工单和首次初始化。`catalogGuardWallNs` 和 `proposalGuardWallNs` 含接口调用所需的控制端准备及校验。`postProposalWallNs` 含分组 QoS、设备计划、线速账及快照摘要。汇总时直接用正式计费总量。

## 重演导出的 I 提案

完成上述 `verify` 和 `examples` 后，在仓库根目录使用相同环境执行下列代码。先核对登记身份和工具，再对照确定性示例 I 的完整目标与审计。

```python
import json
from pathlib import Path
import sys

sys.path[:0] = ["src", "src/rules", "src/rules/vendor/src"]
from component_config import load_components
from strong_tools import validate_proposal

sample = json.loads(Path("residual-example.json").read_bytes())["rules"]
conditions = json.loads(Path("runs/residual-example/CONDITIONS.json").read_bytes())
components = load_components("config/example-rules.json")
assert components.identity == conditions["components"]
assert sample["component"] == {k: components.rules[k] for k in sample["component"]}
action, bound = sample["action"], sample["bound"]
assert action["id"] == "I" and action in sample["catalog"]
rules = components.factory("rules")(config=components.rules)
target, audit = rules.solve_action(action, bound, context=sample["context"])
assert {"target": target, "audit": audit} == sample["returned"]
assert validate_proposal(bound["snapshot"], bound["config"], bound["authority"], target)["valid"]
print(sample["work"], audit["targetHash"])
```

其他工具可能在审计中报告实测 CPU 或墙钟耗时。对这类工具应比较目标、绑定及确定性搜索字段。对原工单运行 `verify` 可核验设备确认与分组计账。

规则测试运行 `python -B -m unittest discover -s tests -p test_rule_component.py`。完整测试运行 `python -B -m unittest discover -s tests -v`。
