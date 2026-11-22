"""不训练神经网络也能运行的多智能体基线示例。

运行：python -m resnet.envs.demo --scenario data/EXT20-02 --steps 10
输入：场景目录、seed、决策步数及基线策略；输出：每步动作/奖励和最终网络统计。
full 始终全速，random 随机选档，queue_aware 仅使用本地观测按队列占用降速。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from .marl_env import NetworkParallelEnv


def choose_actions(env, observations, policy):
    """由可见观测产生动作；不读取 env.sim 的未来脚本或全网状态。"""
    if policy == "random":
        return {a: int(env.action_space(a).sample()) for a in env.agents}
    if policy == "full":
        return {a: 2 for a in env.agents}
    if policy != "queue_aware":
        raise ValueError("未知基线策略")
    result = {}
    for agent in env.agents:
        obs = observations[agent]
        if not obs[0]:
            result[agent] = 0
            continue
        ports = obs[4:].reshape((-1, 4))
        occupancy = max((max(float(port[2]), float(port[3])) for port in ports), default=0)
        # 两个阈值是示例启发式参数，不是官方规定，也不代表最优控制。
        result[agent] = 2 if occupancy < .5 else 1 if occupancy < .9 else 0
    return result


def rollout(scenario, *, steps=10, seed=73, interval_s=1.0, policy="random", config=None):
    """运行一轮短实验并返回可编码为 JSON 的字典；finally 确保资源关闭。"""
    env = NetworkParallelEnv(scenario, config=config, decision_interval_s=interval_s,
                             max_steps=steps)
    started = time.perf_counter()
    records = []
    try:
        observations, _ = env.reset(seed=seed)
        while env.agents:
            actions = choose_actions(env, observations, policy)
            observations, rewards, terminations, truncations, _ = env.step(actions)
            records.append({"time_ns": env.sim.now, "actions": actions, "rewards": rewards,
                            "terminated": terminations, "truncated": truncations})
        return {"experiment": "marl_local_rate_baseline", "policy": policy, "seed": seed,
                "decision_interval_s": interval_s, "steps": records,
                "network_summary": env.sim.summary(),
                "wall_seconds": time.perf_counter() - started,
                "scope": "environment_baseline_no_neural_network_training"}
    finally:
        env.close()


def main():
    parser = argparse.ArgumentParser(description="运行多智能体本地速率控制基线")
    parser.add_argument("--scenario", default="data/EXT20-02")
    parser.add_argument("--steps", type=int, default=10)
    parser.add_argument("--seed", type=int, default=73)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--policy", choices=("random", "full", "queue_aware"), default="random")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is not None and args.output.exists():
        parser.error("输出目录已经存在，请使用新目录以保留旧实验")
    result = rollout(args.scenario, steps=args.steps, seed=args.seed,
                     interval_s=args.interval, policy=args.policy)
    if args.output is not None:
        args.output.mkdir(parents=True)
        (args.output / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(f"结果已保存：{args.output.resolve() / 'summary.json'}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
