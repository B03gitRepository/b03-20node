# 模型组 · 因果观测选工具与统一回合采样

先运行 `python collab/model/check.py`（仅需 Python 3.10+ 标准库）。[完整输入与预期输出](case.json) 由 3 个虚构节点、2 条业务和 1 条已确认路径经生产观测构造器生成。[参考实现](../../src/standalone_selector.py) 仅读取已送达数据。替换自研模块时运行 `python collab/model/check.py --adapter path/to/actor.py`，随后复制一份 [示例配置](../../config/collab-selector.json)，修改其中的 `model.entrypoint` 和有序列表 `symbols` 即可接入真实工单。外部依赖包与资产登记见 [模型详细契约](../../docs/MODEL.md)。

**初始化与单步接口**　工厂函数 `build_model(*, checkpoint, tokenizer, device, config)` 返回策略对象；启用模型且工单需要选择工具时，RC 调用 `choose(observation)`，返回 `(legal_tool_id, {"policyVersion": your_version})`。冻结策略保持单一版本；在线采样策略另须记录真实行为概率并实现保存/恢复钩子（详见 [docs/MODEL.md](../../docs/MODEL.md)）。选定工具后的求解、校验、设备写入、逐跳读回和计分均由系统自动完成。

| 观测字段 | 含义与单位 |
|---|---|
| `timeNs`、`nodes` | 仿真纳秒时间戳；节点索引及观测到的在线状态。表内实体索引均对应本帧观测。 |
| `links` | 两端节点索引、观测状态与可用标志、求解可用线路带宽、实测容量与已占用线路带宽（**bit/s**）；链路时延与抖动单位为 **毫秒（ms）**，误码率与丢包率为小数比例。 |
| `services` | 源/宿节点、业务类型与类别、原始优先级、载荷速率（**bit/s**）；时延与抖动上限单位为 **秒（s）**，误码率与丢包率上限为小数比例；另含新鲜度、活跃状态、需求修订号、诊断结论、可写与受保护标志。 |
| `configs` | 业务索引、已确认路径（用链路索引列表表示）、预留载荷/线路速率（**bit/s**）、配置代际与核验状态。 |
| `actionCatalog`、`legalActions` | 从 `legalActions` 中选出一个合法工具 ID。`actionCatalog` 给出每个工具的方法、作用范围、候选池、预算与阶段；`serviceSet` 指向 `authorizedServiceSets` 中的业务索引列表。 |
| `budgets` | 路径候选数、路径扩展预算、搜索预算与联合调整业务上限；按本张工单下发值使用。 |

每张压缩表（`nodes` / `links` / `services` / `configs`）的每行首元素为实体 ID，其余列先取 `defaults` 默认值，再按第 2 个元素 `mask` 的二进制位从低到高，将置 `1` 对应列依次替换为后续覆盖值；独立解码器见 [check.py](check.py)。

## 采样或评价完整回合（20 / 100 节点通用）

按项目 README 安装基础依赖并设置 `B03_DATA` 后，在仓库根目录执行：

```sh
python episode.py sample --script 04 --components config/collab-selector.json --output runs/sample > transitions.json
python episode.py evaluate runs/sample > evaluation.json
```

两种规模提供完全一致的命令行和 Python 调用：`episode.sample(script, output, ...)` 与 `episode.evaluate(directory)`。训练脚本白名单为 20 节点 `01/02/04/05`、100 节点 `04/05`，可通过 `--services`、`--start`、`--end`、`--drain` 指定训练窗口；开发集验证直接用 `run.py run` 再配合 `episode.py evaluate RUN`。

每条训练转移记录包含已送达观测、所选动作、实际策略版本、采样概率（如有）、区间结束时刻，以及至下一次决策或计分窗结束前的**负未加权未保障业务纳秒数**（除以 `1e9` 即为秒）。`prefixUnweightedServiceNs` 覆盖首次有效决策前的区间，它与各转移奖励取反求和即可精确还原整回合 `U`（同时刻决策可出现零长度区间）。`evaluate` 单独输出 `U / W`、五项赛题指标、工具选择、读回数和实测耗时。参考选择器可在 CPU 上跑通整条通路，模型组可自由替换任意冻结/在线模型与自研训练器。
