"""多重图静态路由：保留每条链路 ID，不创建 IPv4 网卡或子网。

输入 ScenarioSpec；输出 routes[(本机,目的)] = (下一跳,链路ID)。
依赖标准库 heapq。初始选择依次比较传播时延、跳数、从目的展开的链路序列，
容量与质量变化不在此处触发全局重路由。
"""
import heapq


def initial_routes(scenario):
    """按初始链路时延计算所有目的节点的下一跳表。

    为每个目的节点反向展开一次 Dijkstra，平局比较反向的链路 ID 序列。
    control.policy 从业务源正向搜索，还要过滤观测失效和容量不足的边；
    两者的输入、输出和并列路径选择顺序不同，因此分别保留算法入口。
    扩展固定路由时保持 routes[(本节点, 目的节点)] = (下一跳, 链路 ID) 接口。
    """
    graph = {n.node_id: [] for n in scenario.nodes}
    for link in sorted(scenario.links, key=lambda row: row.link_id):
        a, b = link.src_node_id, link.dst_node_id
        if a not in graph or b not in graph or a == b:
            raise ValueError("链路必须连接两个已登记的不同节点")
        weight = round(link.delay_s * 1_000_000_000)
        graph[a].append((b, link.link_id, weight))
        graph[b].append((a, link.link_id, weight))
    routes = {}
    for destination in sorted(graph):
        best = {destination: (0, 0, ())}
        pending = [(0, 0, (), destination)]
        while pending:
            delay, hops, sequence, current = heapq.heappop(pending)
            if best[current] != (delay, hops, sequence):
                continue
            for neighbor, lid, weight in sorted(graph[current]):
                candidate = (delay + weight, hops + 1, sequence + (lid,))
                if neighbor not in best or candidate < best[neighbor]:
                    best[neighbor] = candidate
                    routes[neighbor, destination] = (current, lid)
                    heapq.heappush(pending, (*candidate, neighbor))
    return routes


def stable_link_seed(seed, link_id):
    """Python hash 默认每进程随机化；SHA256 可保证跨进程链路种子一致。"""
    import hashlib
    return int.from_bytes(hashlib.sha256(f"{seed}:{link_id}".encode()).digest()[:8], "big")
