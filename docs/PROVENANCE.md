# 工单证据与源码核验

新工单以 `b03.packet.workorder.v3` 写入 `CONDITIONS.receiptContract`，并以 `b03.packet.run-files.v3` 封存成员索引。`formula-edition.json` 保留本次执行的完整公式版本，运行条件绑定其摘要、实现摘要及实际生效参数。归档核验按记录的版本解释 v1、v2 和 v3 回执。要严格复现旧版本，请在原提交、原引擎和已启用资产下运行 `run.py verify RUN --strict-source`。


`python run.py verify RUN` 按分组时钟核验已保存的工单，检查档案成员摘要、官方场景及引擎身份、业务权限、QoS、路径生效、逐跳设备读回、分组守恒和 U/W 积分。可在同规模的后续版本执行归档核验，无需安装当时的模型适配器、权重或求解器软件包。需要外部可信参照时，将原 `RUN_FILES.json` 的摘要与版本记录一同保存。

## 检查已保存工单

```console
python run.py verify RUN --report audit.json
python run.py examples RUN --output samples.json
```

报告标出核验方式及 `RUN_FILES.json` 的 SHA-256。样例导出器从 `b03.jsonl` 读取真实 RC 选择与求解提案。需要完整求解输入输出时，在创建工单时加 `--record-component-io`。`CONDITIONS.provenance.knowledge` 记录所选技术知识版本的 UTF-8 原文与摘要，可据此核对随附的 K045 查询。规范公式的版本及参数由独立的公式回执绑定。

`components-v1.0.0` 运行已经记录提供方入口模块摘要、完整仓库文件表及选定的知识摘要。更换或移除插件后，当前核验器仍可审计归档业务结果。导出器仅在本地知识文件与归档摘要一致时使用它。若原文件不可用，知识样例标为 `legacy-source-unavailable`，已归档的工单样例仍可读取。早于组件登记的运行使用 `knowledge-unrecorded`。

## 对照原实现与环境

恢复原 Git 版本、Python 环境和所需外部资产，使用原运行选择提供方的清单。

```console
python run.py verify RUN --strict-source --components /path/to/partner.json --simulator /path/to/resnet_py
python run.py verify MODEL_RUN --strict-source --components /path/to/model.json --checkpoint /private/weights --tokenizer /private/tokenizer
```

严格模式核对仓内 Python 源码（包括合作方可调用的辅助模块）、已解析组件配置、Python 版本和平台、安装包及启用的模型资产。登记 `simulatorRuntime` 的工单逐文件核对所选引擎，默认取仓内引擎，`--simulator` 或 `B03_SIMULATOR` 可覆盖，报告显示 `installed-files`。较早未登记运行时的工单，未显式指定外部引擎时按原发布锁核验，显示 `release-lock-only`。差异按组件、发行包、文件或资产列出。固定工具工单无需模型权重。未启用的知识版本及未选清单不进入生效配置对照。源码归档保留文件摘要，报告在可用时列出原提交和当前提交。合并或仅改未用清单后，生效内容等价仍可通过核验。

内置 Qwen 策略按 `config/model.json` 逐项校验分词器文件与权重。20 节点用登记的 safetensors 分片及索引，100 节点用登记的原生检查点和元数据。声明相同原生策略身份的包装器沿用这些检查。独立模型可以使用单个检查点、权重分片目录及单独的分词器。活动模型运行实际提供的资产均按原始字节计算摘要。在线适配器应使用运行前稳定保存的检查点。

v1 工单保留原标签及核验命令。

```console
git worktree add ../b03-original components-v1.0.0
cd ../b03-original
python run.py verify /path/to/original-run
```

从头重演仿真时，在原标签下使用当时文档中的 `run` 命令和原场景、引擎、清单、权重、业务范围及时间窗口。较早回执仅登记入口模块时，未登记的同包文件或依赖需从原实验的环境或 wheel 恢复。

## 登记外部源码及资产

在 B03 所用 Python 环境中安装合作方 wheel。导入名可以与发行包名不同。B03 按实际入口文件定位所属发行包，名称有歧义时填写 `distribution`。`model` 与 `rules` 均支持下述来源字段。

```json
{
  "extends": "components.json",
  "model": {
    "id": "partner.policy",
    "version": "2.1.0",
    "entrypoint": "partner_policy.adapter:build",
    "provenance": {
      "distribution": "partner-policy",
      "distributions": ["partner-kernels[fast]==1.4.0"],
      "assets": {"calibration": "../private/calibration.bin"}
    }
  }
}
```

将清单放在基础 `components.json` 旁，或调整 `extends` 指向基础清单。所有资产路径相对声明它的清单解析，继承项同样适用。回执记录资产的逻辑名及内容摘要。请登记实现实际读取的软件包外配置、查询表和模型文件。动态选择的依赖可在 `distributions` 列出发行包名或 PEP 508 要求。

采集器按已安装的实际内容计算摘要，包括辅助模块、编译扩展、包数据和元数据。按 Python 与平台标记和已启用 extras 递归解析生效的 `Requires-Dist`。自动生成的字节码及安装记录中的本地下载地址等不计入。即使版本或入口未变，也可检出辅助模块及数据文件的变化。pip 留有原 wheel 摘要时，`installationWheelSha256` 标识该下载。通过索引安装时，这项可选记录可能缺失。严格核验始终对照本地安装内容，包含可导入包旁的附加文件。采集及核验均无需在线获取依赖。

本地多模块包或可编辑安装应声明实现及其本地导入依赖的顶层根。每棵包树纳入子模块和资源文件，单文件模块单独收录。根目录外的数据用 `assets` 登记。可编辑安装漏登源码根时会报告需补的配置。跨本地目录的命名空间可分装为多个 wheel。

```json
{
  "extends": "components.json",
  "rules": {
    "id": "partner.local-solver",
    "version": "0.4.0",
    "entrypoint": "partner_solver.factory:build",
    "provenance": {
      "localModules": ["partner_solver", "partner_costs"],
      "assets": {"coefficients": "../private/coefficients.bin"}
    }
  }
}
```

回执保存私有模型资产的哈希。权重原件、官方输入及完整轨迹由运行方持有，原 Git 版本、wheel、检查点和原始档案按实验索引保管。
