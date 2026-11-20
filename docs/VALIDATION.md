# 运行核验

源码自检可在仓库根目录执行。

```bash
python -m unittest discover -s tests -v
```

按首页说明提供本地数据及分组引擎，然后核验同一次运行。

```bash
python run.py run --output runs/check
python run.py verify runs/check --report runs/check-report.json
```

每次运行选择新的输出目录。核验器会比对本地原始文件的校验值，复查工单授权、设备指令生效、读回证据、原始带宽和五项 QoS，并核对数据包的生成、交付、丢弃和在途数。终端返回业务保障、分组计量和实际模型调用数。完整报告与逐业务记录保存在本次本地输出目录及指定的报告路径。
