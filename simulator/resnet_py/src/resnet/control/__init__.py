"""带内控制。

入口 controller.NetworkController；主流程 loop.ControlLoop.tick。
可替换函数：diagnosis.diagnose（运维分析）、policy.decide（资源决策）。
报告入库 observation；节点校验执行 executor；接口契约 interfaces。
"""
