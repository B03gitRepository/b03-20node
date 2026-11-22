# 资源与运行输出

| 资源 | 本地用法 |
|---|---|
| 赛事数据包 | 按[首页的数据准备说明](../README.md)解压，设置 `B03_DATA`。程序从所选场景读取拓扑、业务和事件，并校验原始字节。 |
| 分组仿真引擎 | 默认读取仓内 `simulator/resnet_py/`。可用 `--simulator` 或 `B03_SIMULATOR` 指定外部引擎，命令行优先。两种位置均按 `config/simulator.lock.json` 校验。 |
| Python 依赖 | 固定规则使用 Python 3.12 和 `requirements.txt`。模型模式使用 `requirements-model.txt`，并提供本地模型与分词器路径。 |
| 运行输出 | `runs/<运行名>/` 保存本次输入副本、控制工单、设备执行与读回、逐业务保障区间、分组计数及文件校验表。此目录仅在本地生成。 |
