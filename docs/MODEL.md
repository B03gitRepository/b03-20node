# 模型适配器

从[独立适配器开发包与回合 API](../collab/model/README.md)开始。

`choose(observation)` 接收由 OM/RC 根据已送达报告构造的 `b03.packet.rc-choice.v1` 观测。`legalActions` 是当前工单获准使用且已注册的模型符号。`actionCatalog` 逐项给出工具、作用域、业务集合引用、候选池、预算及阶段。其余表格保存上报的拓扑、测量值、业务需求与已确认的配置。

## 适配器接口

在组件清单中将 `model.entrypoint` 设为 `模块路径:初始化函数名`（如 `module:build_model`）。运行器为每个策略实例调用一次该初始化函数。

```python
def build_model(*, checkpoint, tokenizer, device, config):
    ...
```

`config` 为解析后的模型登记项，包含 `id`、适配器 `version`、`mode`、`catalogVersion` 和有序 `symbols`。策略若声明符号映射，必须与登记一致。初始化函数返回的策略通过 `choose` 返回 `(tool_id, details)`。最小 `details` 只需 `policyVersion`，取标识实际策略的非空字符串或非负整数。冻结模型在同一运行中保持版本固定。原生字段 `parameterVersion` 也可使用。同时提供两个字段时取值须一致。

主控记录已送达的观测、所选工具、适配器 ID 和调用耗时。概率、哈希与 token 数均按实际计算结果填报。`validate_model_choice(choice, policy_identity)` 依据 `CONDITIONS.rcPolicy` 核验归档的 `tool_choice`。

独立模型声明自己的策略版本与已登记符号。身份 `frozen-model` 会启用指定检查点、分词器及贪心策略的原生核验，仅供完整实现该策略的适配器使用。

每张 `indexed_table` 有 `columns`、`defaults`（标识列之后各列的默认值）和 `rows`。每行为 `[id, mask, *values]`。先填默认值，再按位号从小到大替换置位列，最低位对应 ID 后第一列。例如默认值 `[true, 10, 0]` 配合 `[7, 5, false, 3]`，还原为 `[7, false, 10, 3]`。

## 冻结参考模型与工单样例

使用 `--policy model --components config/example-model.json`，指定与内置模型相同的检查点、分词器和设备。`example_model:build_model` 加载已登记的 `FrozenActor`，调用原有前向和贪心选择。两种适配器都记录权重版本、输入 token 数与哈希、合法动作的 softmax 分布、行为概率及各自的 `adapterId`。

用 `--record-component-io` 跑完工单后，把 `RUN` 换成已完成的运行目录，并指定生成它的组件清单。

```console
python run.py examples RUN --components config/example-model.json
```

输出的 `model.input` 和 `model.output` 分别为实际观测和决策，附工单标识与时间。运行方在本地提供赛事输入。

## 在线 PPO 契约

按已送达合法动作上的实际行为分布采样。返回所选工具的 `policyVersion`、完整 `behaviorProbabilities` 和大于零的 `behaviorProbability`。若有合法集上的 softmax，可另存为 `legalProbabilities`。若提供 `oldLogProbability`，其值必须等于所选行为概率的自然对数。

策略实现 `save_checkpoint(path)` 和 `restore_checkpoint(path)`。提供检查点时，主控在构造策略后、首次选择前调用恢复。训练器在完整更新边界存档。检查点应能恢复参数、优化器、版本、采样器状态，以及边界处仍待处理的轨迹或更新数据。通过复现恢复后的下一次采样与更新验收。下文参考训练器在实际分组工单上使用这两个接口。

## CPU 动态 INT8 推理

在分组仿真环境中安装[模型依赖](../requirements-model.txt)，提供冻结策略使用的检查点及分词器。PyTorch 需要支持 x86 或 FBGEMM 量化算子。以下示例使用四个 CPU 线程。

```sh
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 python run.py run \
  --policy model --components config/int8-model.json --device cpu \
  --checkpoint "$B03_CHECKPOINT" --tokenizer "$B03_TOKENIZER" \
  --data "$B03_DATA" --output runs/int8
python run.py verify runs/int8 --strict-source --components config/int8-model.json \
  --checkpoint "$B03_CHECKPOINT" --tokenizer "$B03_TOKENIZER"
```

适配器先核对原权重与分词器摘要，再把各 Linear 权重在内存中转为 qint8。每次前向记录实际调用的量化模块数、完整预填充耗时和端到端选择耗时。`CONDITIONS.json` 的 `rcPolicy.quantization` 给出后端、线程数、转换耗时、层数和权重元素数。嵌入、归一化等运算保留 FP32。共享嵌入与输出矩阵可分别计入浮点嵌入和量化输出投影，计算转换覆盖率时采用 Linear 元素数。分组时钟计入实测选择耗时，较慢的 CPU 推理会占用评价窗口。A2A 默认每次读取超时为 300 秒，可将 `B03_A2A_TIMEOUT_S` 设为有限正秒数。传输重试复用同一消息，RPC 回执记录读取超时、尝试次数和重试错误类型。

## 超过 token 上限的完整输入

未超上限时沿用原聊天模板及观测。超限时将连续的默认节点行、各授权业务集内连续的编号编码为闭区间，解码后逐项核对规范化 JSON 完全相等，再分词。回执记录原始及送入模型的 token 数、消息哈希、token 哈希和观测哈希。可逆编码后仍超限时，选择报错并给出两次计数。工单轨迹保存原始已送达观测。

## 在线分组训练与恢复

参考训练入口使用 **100 节点配置**、指定的原生 v374 检查点和配套分词器。已验证的训练路径使用支持 BF16 的 CUDA 设备，仅使用获准的 100 节点训练脚本 `04`、`05`。工单核验命令也可读取已归档运行。

`B03_BASE` 指向 v374 检查点，`B03_TOKENIZER` 指向有使用许可的分词器，`B03_DATA` 指向官方 100 节点数据。需要覆盖仓内分组仿真器时设置 `B03_SIMULATOR`。在私有训练目录中使用相同的原业务名单和评价窗口做配对对照。下面的示例跑两个小训练窗口，并在新进程中核验第二个检查点。

```sh
: "${B03_TRAIN_SERVICES:?请填写已授权训练名单中的业务 ID，以逗号分隔}"
for round in 1 2; do
  if [ "$round" = 1 ]; then script=04; else script=05; fi
  python train.py step --workspace runs/ppo --round "$round" --script "$script" \
    --components config/packet-ppo.json --checkpoint "$B03_BASE" --tokenizer "$B03_TOKENIZER" \
    --data "$B03_DATA" --device cuda \
    --services "$B03_TRAIN_SERVICES" --start 168 --end 212 --drain 60 || exit 1
done
python train.py restore --workspace runs/ppo --round 2 --checkpoint "$B03_BASE" \
  --tokenizer "$B03_TOKENIZER" --device cuda --report runs/ppo/fresh-restore.json
```

各轮分别用固定 I 与重新采样的策略运行标准的 A2A、OM/RC 分组工单。初始化函数接收登记的合法符号。参考行为分布由 75% 策略概率和 25% 合法动作均匀概率组成。训练日志保存旧分布、所选概率、决策版本、完整观测和 token ID。

仅评价窗口内至少有两个合法工具的采样参与更新。原始回报为从该次决策至评价终点的未保障业务数积分取负，单位为业务秒。同窗口固定 I 回报作为基线，对全参数执行一步带裁剪 PPO。优化器只接收两组未加权 U 回报，W 单独用于业务评价。

每轮保存归档的 `sampled` 工单和 `fixed-I` 对照、各自的核验结果及 `round-NNN.json` 回执。回执的相对路径可定位匹配档案。用 `python run.py verify PATH` 检查归档工单。严格源码核验还需提供该次 `components.json`、仿真器、v374 基础检查点、分词器和已登记的前序重演快照。

`ppo-replay.json` 是原子写入的工作日志。每份 `round-NNN-replay.json` 是不可变重演检查点，包含采样游标、输入、结果、优化器设置和参数哈希，主控写入此类检查点无需额外完整模型副本。用相同基础权重、代码、数值环境和设备重建参数。`restore` 对照更新后的现场记录核验完整指纹、下一次动作、概率和游标。以相同参数重提已完成的轮次会核验原身份。若进程在提交日志后停止，重试可补完回执。

独立适配器可使用自有训练器实现上述观测、选择及保存恢复契约。`src/packet_training.py` 的 `run_episode` 在构造时取得策略实例，并返回工单结束后的核验结果。优化器更改后应登记新适配器版本，并按数据管理要求保存自己的重演格式或完整检查点。

参考重演日志的每条训练谱系最多保留 16 次更新、64 MiB。重建使用登记的基础检查点。改变训练契约或基础权重时，请另建一条训练谱系。
