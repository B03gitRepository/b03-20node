"""网络中的一个分组。

这里保存一封“消息”的收发地址、大小和发出时间。它不是网卡，也不
直接知道是否能送到；每经过一条链路，都必须排队并等待仿真时钟推进。

输入：业务源或管控端点构造下列字段。输出：在节点之间逐跳传递的 Packet。
依赖：Python dataclass。payload_bytes 用于业务统计，wire_bytes 用于占用链路；
后者应已包含本项目保留的协议开销，链路不能再加一次。
"""
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Packet:
    """可变的转发记录；slots 减少大量分组同时存在时的对象内存开销。

    uid 在同一轮仿真中唯一；created_at_ns 是源端生成时刻，转发时不重置。
    epoch 标识受控业务的路径版本。hop_limit 是允许经过的最多链路数，
    hops 由转发模块递增，以防错误路由形成无限循环。
    """

    uid: int
    src_node_id: str
    dst_node_id: str
    payload_bytes: int
    wire_bytes: int
    created_at_ns: int
    kind: str = "data"
    service_id: str = ""
    epoch: int = 0
    message: Any = None
    hop_limit: int = 64
    hops: int = 0

