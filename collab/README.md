# 四组协作入口

第一步用本组的合成用例在本地检查接口，再把组件配置接入真实工单。四种自测只需 Python 标准库。正式工单使用项目 README 列出的运行依赖及本地提供的场景数据。

| 小组 | 系统交付 | 本组交付 | 开始开发 |
|---|---|---|---|
| 模型 | RC 因果观测、原业务优先级、合法工具与资源预算 | 一个合法工具 ID、策略版本，在线抽样另记真实行为概率 | [完整观测、字段单位与回合接口](model/README.md) |
| 规则 | 观测快照、授权、已选工具、预算及可选知识 | 受权业务的配置变更及审计信息。主控保留未变业务并核验原速率、路径与容量 | [纯数据输入与差量方案](rules/README.md) |
| 知识库 | 条目 ID 或主题、指定知识版本、启用的消费环节 | 本版条目、来源、适用条件和排序。OM、规则、模型可分别选用 | [只读检索与接入配置](knowledge/README.md) |
| 仿真 | 版本化的事件、观测、设备与计量端口 | 通过公开接口响应事件或消费记录，现有分组引擎负责推进时钟 | [四类端口与示例组件](simulator/README.md) |

```sh
python collab/model/check.py
python collab/rules/check.py
python collab/knowledge/check.py
python collab/simulator/check.py
```

**系统集成组**维护接口版本、目录映射、回执和公平对照。同一场景比较时固定数据、授权、原始需求、时钟窗口、随机种子和设备流程；将组件清单传给 `run.py run --components ...`，再用 `run.py verify RUN --strict-source --components ...` 复核。双规格的默认工具、OM/RC、A2A、设备写入与读回都走标准运行入口。新工单以 `b03.packet.workorder.v3` 封存使用的公式版、参数和成员索引。[口径查询](../docs/KNOWLEDGE.md) · [历史回执核验](../docs/PROVENANCE.md)
