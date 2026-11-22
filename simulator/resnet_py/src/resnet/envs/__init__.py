"""多智能体实验接口。导入网络内核时不必安装这些可选训练依赖。"""

from .marl_env import NetworkParallelEnv, parallel_env

__all__ = ["NetworkParallelEnv", "parallel_env"]
