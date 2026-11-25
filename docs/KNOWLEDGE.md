# 只读技术知识与计算口径

v2 接口、可运行示例与独立测试见[知识库组手册](../collab/knowledge/README.md)。下文也说明原有插件的登记方式。

知识负责人按版本发布 JSON。条目包含稳定编号、主题、正文、来源和适用条件。默认检索器读取 `1.0.0`，示例检索器为 `1.1.0` 在内存建立索引。

## 查询知识版

在仓库根目录执行。独立查询只需 Python 和所选知识文件。

```bash
python run.py knowledge --id K044
python run.py knowledge --components config/knowledge-v2.json --topic clock
python run.py knowledge --components config/example-knowledge.json --id K044
```

命令返回 JSON 数组。K044 的主题为 `admission`，来源包括 `src/opening.py`，适用于已送达且允许写入的残余接入开局。返回条目的 `version` 标识知识版，未知编号或未命中的主题返回空数组。

## 替换检索器或知识源

建立继承随仓注册清单的配置。知识源路径相对于声明它的清单解析。

```json
{
  "extends": "components.json",
  "knowledge": {
    "id": "example.topic-index",
    "version": "1.1.0",
    "source": "../knowledge/technical-v2.json",
    "entrypoint": "example_knowledge:build_knowledge"
  }
}
```

宿主将独立拷贝的注册信息和已解析的知识版传给 `build_knowledge(*, config, library)`。返回对象实现 `lookup(*, identifier=None, topic=None)`，每次使用一个选择条件并返回编号列表。编号精确匹配。原有接口的主题匹配忽略大小写、查找子串，v2 插件可自行召回和排序。本地模块或已安装包可维护自己的索引。宿主按锁定知识版复核条目、版本、必需字段和编号唯一性。OM 依据已送达证据掌握写权限，设备与计量沿用工单流程。

独立更新知识源时发布新的 `b03.knowledge.v1` 文件，相同概念沿用稳定编号，同时更新来源和版本。发行包与辅助文件按溯源格式登记。[检索器示例](../src/example_knowledge.py) · [切换知识版](../config/knowledge-v2.json) · [切换检索器](../config/example-knowledge.json) · [依赖登记](PROVENANCE.md)

## 检查 RC 实际查阅的知识

首次获准调用求解器时处理初配。在残余接入模式下，OM 交付可写的待接入业务或需求修订后，RC 于工具选定后、初配求解前查询 K044。回执记录工单、快照、工具，以及触发业务的需求版本、观测时刻和到达时刻。

命中 K044 时，RC 对照原求解提案核查初配。已提出的路径保持固定，新增业务须属于获准的漏接集合，待接入和新增业务均保持原线路速率。初配、分组 QoS 核查和设备读回分别执行。回执按业务记录所选规则是否提出接入、是否补搜、是否补接和是否发出安装请求。所选知识版没有 K044 时，记录空结果并沿配置继续。

```bash
python run.py run --policy I --components config/example-knowledge.json --record-component-io --output runs/index-work
python run.py verify runs/index-work --strict-source --components config/example-knowledge.json --report runs/index-check.json
python run.py examples runs/index-work --output runs/index-io.json
```

按首页设置输入数据与引擎即可复现。`b03_rc_plan.knowledge_io` 保存检索器、知识源摘要、问题、返回条目、技术核查及处理结果。`examples` 在 `knowledge` 字段导出真实回执。`verify` 按归档知识版、OM 授权、送达需求、初配审计和最终设备计划核验。归档核验可在没有原检索器的机器上运行。严格核验使用原组件与依赖。场景产生的明细轨迹随授权运行资产保存。[归档核验](PROVENANCE.md)

## 查询计算公式

`python run.py knowledge --formulas --id packet.wire-rate` 返回公式的操作标识、参数、单位、适用条件、来源、版本和摘要。可用 `--topic 容量` 按主题查询，或用 `--edition 1.0.0` 指定版本。11 个公式编号与定义保存在[计算口径知识版](../knowledge/formulas-v1.json)。控制器、规则、计量与核验绑定同一版[计算函数](../src/b03_formulas.py)。新工单随回执封存公式声明和生效参数。公式查询与 OM、规则、模型的可选知识注入独立。新增口径时登记新编号或版本，并补齐相应计算实现、数值用例与回执读取分支，保留旧版声明及历史读验。后续智能体可通过相同的编号或主题查询取得带来源的结果，是否采纳仍由工单权限和所选策略决定。
