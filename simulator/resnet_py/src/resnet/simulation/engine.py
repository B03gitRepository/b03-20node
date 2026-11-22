"""统一时钟与绝对时间调度。

登记“某时刻做某件事”，时钟直接跳到下一个事件，不真实等待几千秒。
输入：整数纳秒与回调函数。依赖：SimPy。输出：按时间和优先级执行的副作用。
同刻优先级0先执行完整场景批次，普通收发优先级1；批次内部不推进时钟。
"""
from __future__ import annotations

import simpy


class Call:
    """可取消的定时器。取消只使回调失效，不直接操作 SimPy 私有事件堆。"""
    def __init__(self, env, at_ns, callback, priority=1):
        if type(at_ns) is not int or at_ns < env.now:
            raise ValueError("定时器必须使用不早于当前时刻的整数纳秒")
        self.cancelled = False
        event = simpy.Event(env)  # 创建一次性事件；不需要启动一个长期协程。
        event._ok = True  # 自定义调度事件按 SimPy Event 的成功事件约定初始化。
        event._value = None
        event.callbacks.append(lambda _: None if self.cancelled else callback())
        env.schedule(event, priority=priority, delay=at_ns - env.now)

    def cancel(self):
        self.cancelled = True


def advance_inclusive(env: simpy.Environment, until_ns: int) -> None:
    """处理截止时刻及此前全部事件，包括同刻回调再次登记的零延时事件。

    SimPy run(until=数字)默认不处理恰在边界的普通事件。这里明确使用闭区间
    推进，避免训练 step 的边界事件被错放到下一次动作之后。
    """
    if type(until_ns) is not int or until_ns < env.now:
        raise ValueError("推进终点必须是单调递增的整数纳秒")
    # 即使区间内没有网络事件，也安排一个空事件，使时钟真正到达终点。
    Call(env, until_ns, lambda: None, priority=3)
    while env.peek() <= until_ns:
        env.step()
