# 知识库组 · 版本化只读查询

`python -I collab/knowledge/check.py` 从合成知识版运行编号和主题查询。检索器可采用关键词、语义索引或远端服务。[输入与输出](case.json) · [标准库参考实现](../../src/collab_ranked_knowledge.py)

**初始化**　`build_knowledge(*, config, library)` 提供组件注册信息与所选知识版的独立副本。工厂返回对象，实现 `lookup(*, identifier=None, topic=None)`。请求二选一。

| 请求 | 提供方返回 | 系统交付给消费者 |
|---|---|---|
| 指定编号 | 本版存在时返回单个同名 ID，否则返回空列表 | 条目正文、编号、主题、来源、适用条件及版本 |
| 指定主题 | 自主召回与排序的本版唯一 ID 列表；允许空结果 | 按返回顺序组装同版条目并绑定来源摘要 |

**换插件**　运行 `python -I collab/knowledge/check.py --provider my_knowledge.py`。在组件配置中设置 `knowledge.entrypoint`、`source`、`version` 与 `contractVersion: b03.knowledge.lookup.v2`。同一进程沿用本版索引；更换知识版需更新来源与版本。独立安装的插件按[资产说明](../../docs/ASSETS.md)登记来源。

**选择消费入口**　`knowledge.bindings` 可以分别配置 `om`、`rules`、`model`；每项使用 `{"topic":"查询词"}` 或 `{"identifier":"条目ID"}`。留空即沿用默认流程。OM 在完成权限诊断后、将工单交给 RC 前添加 `diagnosis.knowledgeContext`，授权和 QoS 仍从已送达观测计算。规则 v2 在所选工具由本插件处理且声明 `rules.options.acceptKnowledge: true` 时收到 `request.knowledgeContext`，模型插件须显式声明 `model.options.acceptKnowledge: true` 才会收到 `observation.knowledgeContext`。内置冻结模型继续使用原始输入。

```console
python run.py knowledge --components config/collab-rules-knowledge.json --topic "detour guidance"
python run.py run --policy X --components config/collab-rules-knowledge.json --output ranked
python run.py verify ranked --components config/collab-rules-knowledge.json --strict-source
```

参考配置在规则求解前查询路径排序建议。将配置换为 `config/collab-ranked-hops.json`，同一查询和同一知识版会让规则优先比较跳数。工单 `knowledge_contexts` 记录每个入口的查询、排序结果、来源、绑定快照和耗时；`solver_audit.rulePatch.output.audit` 记录规则实际采用的排序。回执核验按归档知识版核对内容、授权快照和消费位置。
