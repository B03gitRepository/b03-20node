# 知识库组 · 版本化只读检索与多入口挂载接口

## 一、模块职责

知识库组负责提供版本化、只读的技术知识检索组件（契约版本 `b03.knowledge.lookup.v2`）。组件基于锁定的知识版实现按编号精确查找或按主题自主召回与排序；宿主按配置将检索结果组装后注入 OM、规则或模型环节，知识条目不改变写权限或原始 QoS 需求。

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

## 三、两条查询分支与三个接入环节

### 1. `lookup(*, identifier=None, topic=None) -> list[str]` 两条查询分支

每次调用 `identifier` 与 `topic` 严格二选一（传入非空字符串，另一参数为 `None`）：

| 查询分支 | 输入参数 | 组件返回值要求 | 宿主组装与交付内容 |
|---|---|---|---|
| **分支 A：按编号精确查找** | `identifier="<ID>"`, `topic=None` | 本版存在该编号时返回 `[identifier]`，否则返回 `[]` | 返回对应单条 6 字段条目（含 `id`、`topics`、`content`、`sources`、`applicability`、`version`）；知识版摘要记录于工单回执 `knowledge_contexts[port].component.sourceSha256` |
| **分支 B：按主题召回与排序** | `identifier=None`, `topic="<查询词>"` | 自主召回并排序后的本版唯一 ID 列表（允许 `[]`；可接入关键词、向量检索或外部服务） | 严格按返回 ID 顺序组装同版 6 字段条目（含 `version`）；知识版摘要记录于工单回执 `knowledge_contexts[port].component.sourceSha256` |

规范计算口径、单位和适用条件可通过 `python run.py knowledge --formulas --topic 容量` 查询。版本登记及调用方式见[知识说明](../../docs/KNOWLEDGE.md)。

### 2. 三个接入环节挂载规则（`knowledge.bindings`）

通过配置中的 `knowledge.bindings` 分别声明 `om`、`rules`、`model` 入口，每项填写 `{"topic": "<查询词>"}` 或 `{"identifier": "<条目ID>"}`。关闭单个入口须直接省略该键（填入 `{}` 或 `null` 均会报错）；关闭全部入口可配置 `"bindings": {}`，未启用入口沿用既有流程：

| 接入环节 | 触发时机 | 注入位置 | 接入方前置开关与边界 |
|---|---|---|---|
| `om` | OM 完成权限诊断后、将工单交给 RC 前 | `diagnosis.knowledgeContext` | 配置 `knowledge.bindings.om` 即注入；授权范围与原始 QoS 仍按已送达观测独立计算 |
| `rules` | 规则求解调用 `solve_action` 前 | `request["knowledgeContext"]` | 所选工具由 v2 规则插件处理且声明 `rules.options.acceptKnowledge: true` |
| `model` | RC 构造模型因果观测时 | `observation["knowledgeContext"]` | 模型插件须显式声明 `model.options.acceptKnowledge: true`；内置冻结模型保持原始输入不变 |

各入口的查询请求、排序结果、知识版来源、绑定快照与耗时记录在工单 `knowledge_contexts` 中；首单残余准入核查（`K044`）继续保留独立记录 `knowledge_io`。离线 `run.py verify` 会按归档知识版逐项复核条目内容、授权快照与注入位置。

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

| 用途与模式 | 参考源码 / 知识版 | 配套配置 |
|---|---|---|
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
# 1. 单独验证主题召回与排序结果
python run.py knowledge --components config/collab-rules-knowledge.json --topic "detour guidance"

# 2. 接入真实工单并严格核验（可切换 config/collab-ranked-hops.json 对比排序差异）
python run.py run --policy X --components config/collab-rules-knowledge.json --output runs/ranked
python run.py verify runs/ranked --components config/collab-rules-knowledge.json --strict-source
```
