# 规则组 · 纯数据配置提案

`python -I collab/rules/check.py` 从合成的小网络运行一次规则。参考实现仅用 Python 标准库。替换时运行 `python -I collab/rules/check.py --provider my_rule.py`。单独复制 `check.py`、`case.json` 和自己的插件也可自测；用 `--registration registration.json` 指定自己的规则注册参数。样例中的一条业务保持原配置，另一条可以走直达或绕行链路。调换两条知识提示的顺序，会在满足原始 QoS 的两条路径间切换。[输入与输出](case.json) · [参考实现](../../src/collab_patch_rule.py)

**初始化**　`build_rules(*, config)` 接收注册信息，返回含以下方法的对象。每次调用得到独立的 JSON 数据副本。

| 方法 | 输入 | 返回 |
|---|---|---|
| `enumerate_actions(request)` | 本单已授权的 `candidates`、快照、权限和预算 | 可以受理的 ID 列表，按注册目录进入后续选择 |
| `solve_action(request)` | 同一快照与权限、选中的 `action`、本单 `budgets`，启用 `rules.options.acceptKnowledge` 并绑定查询后附上排序的 `knowledgeContext` | `{"patch":{"服务ID":{"nodes":[…],"linkIds":[…],"allocatedBps":…}},"audit":{…}}` |

`snapshot.nodes/links` 是已送达证据构造的可用网络；`requests` 提供优先级、原业务速率 `requiredPayloadBps` 及线路需求 `requirement.bandwidthBps`。求解和配置使用线路 bit/s，时延与抖动使用 ms，误码与丢包为比例。时延相加、抖动平方和开方、误码率求和后以 1 封顶、交付概率连乘。链路可分配容量已扣除控制余量和快照之外的预留，`assignments` 中已有路径仍占用该链路。

`patch` 省略的服务维持当前配置，值 `null` 表示申请释放。只可修改 `scope.serviceIds` 与 `authority.executableServiceIds` 的交集；健康冻结和受保护项保持原样。主控补齐未变业务，绑定工单与快照摘要，验证五项 QoS、容量、跳数、修改数量及设备操作顺序。业务到期由现有生命周期工单释放。

**预算与审计**　`maxHops/maxChangedServices` 约束实际输出。`path_expansion_budget/search_budget` 等是参考搜索器的工作量参数；其他算法可报告自己真实使用的计数。`audit.usedKnowledge` 可列出实际使用的条目 ID。主控独立计时并将成本计入业务时钟。可在 `rules.options.outputBudgets` 声明命名预算，例如 `{"bounded":{"maxSolverWallMs":800,"maxHops":6}}`，让工具的 `budget` 指向它。主控在返回时检查壁钟期限。

**注册和联调**　从示例配置复制一份，设置 `rules.entrypoint` 为可导入的 `模块:工厂`，契约版本使用 `b03.rules.patch.v2`。新 ID 在 `catalog.symbols`、新目录版本和 `rules.options.customTools` 中登记；`permit` 是原工单必须具备的方法权限，`scope` 为 `writable_active` 或 `retain`。模型选择新 ID 时同步更新并验证模型映射。沿用现有 ID 替换算法也需另行登记目录版本和 `providedNative`。独立安装的插件按[资产说明](../../docs/ASSETS.md)登记来源。

```console
python run.py run --policy X --components config/collab-rules-knowledge.json --output run-X
python run.py verify run-X --components config/collab-rules-knowledge.json --strict-source
```

在同一授权工单上，`config/collab-ranked-hops.json` 仅更换知识排名，`config/collab-rules.json` 使用同一算法的默认排序。回执 `solver_audit.rulePatch` 保存原请求、真实补丁、算法审计及实测耗时；`decisions` 和设备读回用于核对实际生效结果。
