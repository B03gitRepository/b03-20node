"""把连续发生的网络事件包装成 PettingZoo 的联合决策环境。

网络在两次决策之间照常发送和排队；到决策时刻，每个源节点决定自己
下一段时间发送多少数据。环境一次收齐所有节点的动作，再推进同一个 SimPy 时钟。

输入：ScenarioSpec 或场景目录、仿真配置、决策间隔、受管源节点集合。
依赖：Simulation 仿真实例、Gymnasium 的空间定义、PettingZoo ParallelEnv。
输出：reset 返回观测和说明；step 返回观测、奖励、终止、截断和说明五个字典。

本版动作是本机源端整形，采用固定路由；普通规则的远程带内管控在独立模式中。
这里不让学习策略直接修改远端节点，也不向策略提供未来事件表。
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Callable

import numpy as np
from gymnasium import spaces
from pettingzoo import ParallelEnv

from resnet.contracts.scenario import ScenarioSpec
from resnet.io import load_scenario
from resnet.simulation.traffic import RATE_FACTORS


def _finite_positive(value, name):
    """提前拒绝零、负数和 NaN，避免出现不推进时钟的训练循环。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须为正数")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须为正数")
    return value


class NetworkParallelEnv(ParallelEnv):
    """一个 agent 对应一个业务源节点，仅有权调整该节点的发送速率。

    action=0 暂停，1 以需求速率的一半发送，2 以需求速率发送。
    同一节点的所有本地业务使用相同倍数；是否登记和活动仍由场景控制。
    节点离线时 agent 不被删除；上线后继续使用最后选定的倍数。
    """

    metadata = {"name": "resnet_local_rate_v1", "render_modes": ["ansi"],
                "is_parallelizable": True}

    def __init__(self, scenario, config=None, *, decision_interval_s=1.0,
                 managed_nodes=None, max_steps=None, render_mode=None,
                 simulation_factory: Callable | None = None):
        self.scenario = (scenario if isinstance(scenario, ScenarioSpec)
                         else load_scenario(Path(scenario)))
        self.config = deepcopy(config or {})
        self.config["mode"] = "marl"
        # 训练回合不自动推进排空期。终点仍在途的包必须保留并在结果中列明。
        self.config["drain_s"] = 0
        self.config.setdefault("start_s", 0)
        self.config.setdefault("end_s", self.scenario.duration_s)
        self.config.setdefault("seed", 73)
        self.config.setdefault("payload_bytes", 1000)
        self.config.setdefault("rate_scale", 1.0)
        self.interval_ns = round(_finite_positive(decision_interval_s, "决策间隔") * 1e9)
        if self.interval_ns <= 0:
            raise ValueError("决策间隔应至少为 1 纳秒")
        if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
            raise ValueError("max_steps 必须为正整数或 None")
        if render_mode not in (None, "ansi"):
            raise ValueError("仅支持 render_mode='ansi'")
        self.max_steps, self.render_mode = max_steps, render_mode
        self._factory = simulation_factory
        services = self.scenario.services + self.scenario.added_services
        self._services = {s.service_id: s for s in services}
        source_nodes = sorted({s.src_node_id for s in services})
        selected = source_nodes if managed_nodes is None else list(managed_nodes)
        if not selected or len(set(selected)) != len(selected) or not set(selected) <= set(source_nodes):
            raise ValueError("managed_nodes 必须是互不重复的业务源节点")
        self.possible_agents = sorted(selected)
        self.agents = []
        # 节点 ID 即 agent ID，资源归属一眼可查；一个源节点不会被两个 agent 重复控制。
        self._owned = {n: tuple(sorted(s.service_id for s in services if s.src_node_id == n))
                       for n in self.possible_agents}
        self._incident = {n: tuple(sorted(l.link_id for l in self.scenario.links
                                          if n in (l.src_node_id, l.dst_node_id)))
                          for n in self.possible_agents}
        # 每条本地端口用四个数：可用、容量、等待包占用、等待字节占用。
        # 不向观测放未来业务数、登记时刻、活动表或远端流量统计。
        self.observation_spaces = {n: spaces.Box(0.0, 1.0,
            shape=(4 + 4 * len(self._incident[n]),), dtype=np.float32)
            for n in self.possible_agents}
        self.action_spaces = {n: spaces.Discrete(len(RATE_FACTORS)) for n in self.possible_agents}
        self.sim = None
        self._factors = {}
        self._steps = 0

    def observation_space(self, agent):
        return self.observation_spaces[agent]

    def action_space(self, agent):
        return self.action_spaces[agent]

    def reset(self, seed=None, options=None):
        """创建全新的网络；同一 seed 和动作序列应得到相同观测及奖励。

        seed 仅决定仿真与动作空间的随机数。reset 不会恢复上轮的队列、路由或统计。
        """
        # PettingZoo 会传入探测用 options；本版保留接口但不据此修改实验配置。
        if options is not None and not isinstance(options, dict):
            raise ValueError("options 必须是字典或 None")
        self.close()
        config = deepcopy(self.config)
        if seed is not None:
            if type(seed) is not int or seed < 0:
                raise ValueError("seed 必须是非负整数")
            config["seed"] = seed
        if self._factory is None:
            # 到 reset 创建网络时才加载仿真内核；测试可注入同接口的工厂。
            from resnet.simulation.simulation import Simulation
            factory = Simulation
        else:
            factory = self._factory
        self.sim = factory(self.scenario, config, output_dir=None)
        self.agents = self.possible_agents.copy()
        self._factors = {agent: 0.0 for agent in self.agents}
        self._steps = 0
        for index, agent in enumerate(self.agents):
            self.action_spaces[agent].seed(config["seed"] + index)
            self.observation_spaces[agent].seed(config["seed"] + index)
        # 先执行零时刻/窗口起点批次，第一份观测才反映已经生效的状态。
        self.sim.advance(self.sim.start_ns)
        observations = {agent: self._observe(agent) for agent in self.agents}
        return observations, {agent: self._info(agent) for agent in self.agents}

    def _observe(self, node):
        """仅读取本机需求与直连端口。归一化使用公开固定刻度，不泄露未来峰值。"""
        state = self.sim.state
        selected = self.config.get("service_ids")
        local_active = [sid for sid in self._owned[node]
                        if sid in state.registered and sid in state.active
                        and (selected is None or sid in selected)]
        requested_bps = sum(self._services[sid].required_bps for sid in local_active)
        requested_bps *= self.config["rate_scale"]
        # 业务条数用 x/(1+x) 压缩，避免以未来新增业务总数作归一化分母。
        values = [float(state.node_online[node]), len(local_active) / (1 + len(local_active)),
                  requested_bps / (100_000_000 + requested_bps), self._factors[node]]
        for lid in self._incident[node]:
            snapshot = self.sim.links[lid].snapshot()
            parameters = snapshot.get("parameters", snapshot)
            cap = parameters["capacity_bps"]
            values.extend([float(snapshot["effective_up"]), cap / (100_000_000 + cap),
                snapshot["queue_packets"] / self.sim.config["queue_packets"],
                snapshot["queued_bytes"] / self.sim.config["queue_bytes"]])
        return np.clip(np.asarray(values, dtype=np.float32), 0.0, 1.0)

    def _counters(self, node):
        """奖励计算器读累计实际收包及需求；这些接收端统计不会放进策略观测。"""
        demanded = sum(self.sim.demand_bits_by_service.get(sid, 0.0) for sid in self._owned[node])
        stats = self.sim.metrics.service_stats
        timely = sum(stats.get(sid, {}).get("deadline_success_payload_bytes", 0)
                     for sid in self._owned[node]) * 8
        return float(demanded), timely

    def _info(self, agent):
        """说明中也不附带完整未来事件或全网真值，避免被训练脚本误当观测。"""
        return {"time_ns": int(self.sim.now), "agent_node_id": agent,
                "action_scope": "local_source_rate", "rate_factor": self._factors[agent]}

    def step(self, actions):
        """收齐本轮动作，再统一推进，避免先处理的 agent 多跑一段网络。

        返回的终止字典含本次参与决策的所有 agent；结束后 self.agents 置空。
        不因为某节点暂时离线就结束它的训练回合。
        """
        if self.sim is None:
            raise RuntimeError("请先调用 reset()")
        if not self.agents:
            if actions:
                raise ValueError("回合已结束，请 reset() 开始下一回合")
            return {}, {}, {}, {}, {}
        current = self.agents.copy()
        if not isinstance(actions, dict) or set(actions) != set(current):
            raise ValueError("每一步必须同时提供全部当前 agent 的动作")
        # 全部通过检查后才修改状态；不接受一半动作后再抛异常。
        if any(isinstance(actions[a], (bool, np.bool_)) or
               not self.action_spaces[a].contains(actions[a]) for a in current):
            raise ValueError("动作必须为整数 0、1 或 2")
        before = {a: self._counters(a) for a in current}
        for agent in current:
            factor = RATE_FACTORS[int(actions[agent])]
            self._factors[agent] = factor
            # 本地动作立即改变真实业务生成速率；网络中的旧包仍由队列继续处理。
            self.sim.set_local_rate_multiplier(agent, factor)
        target = min(self.sim.now + self.interval_ns, self.sim.end_ns)
        self.sim.advance(target)
        self._steps += 1
        reached_end = self.sim.now >= self.sim.end_ns
        complete_scenario = self.sim.end_ns >= round(self.scenario.duration_s * 1e9)
        natural = bool(reached_end and complete_scenario)
        truncated = bool((reached_end and not natural) or
                         (self.max_steps is not None and self._steps >= self.max_steps and not natural))
        observations = {a: self._observe(a) for a in current}
        rewards, infos = {}, {}
        for agent in current:
            after = self._counters(agent)
            demand_bits = max(0.0, after[0] - before[agent][0])
            timely_bits = max(0, after[1] - before[agent][1])
            # 需求来自场景，不能靠少发/不发把分母缩小；活动需求未满足就有负奖励。
            rewards[agent] = (2 * min(timely_bits / demand_bits, 1.0) - 1.0
                              if demand_bits > 0 else 0.0)
            infos[agent] = self._info(agent) | {"reward_scope": "training_evaluator",
                "demand_bits": demand_bits, "timely_delivered_bits": timely_bits,
                "episode_end_reason": ("scenario_end" if natural else
                    "time_limit" if truncated else None)}
        terminations = {a: natural for a in current}
        truncations = {a: truncated for a in current}
        if natural or truncated:
            # 保留 sim 供调用者检查 pending；这里只结束决策接口，不清空网络队列。
            self.agents = []
        return observations, rewards, terminations, truncations, infos

    def render(self):
        if self.sim is None:
            return "环境尚未 reset"
        return f"t={self.sim.now / 1e9:.3f}s | agents={len(self.agents)} | step={self._steps}"

    def close(self):
        if self.sim is not None and hasattr(self.sim, "close"):
            self.sim.close()
        self.sim = None
        self.agents = []


def parallel_env(**kwargs):
    """PettingZoo 常用工厂入口：env = parallel_env(scenario=目录)。"""
    return NetworkParallelEnv(**kwargs)
