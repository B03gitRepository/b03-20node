"""可暂停的业务源，每个包由发送速率与包长共同决定生成时刻。

业务源是按节拍往网络放包的水龙头。关掉它只停止后续放包，已经进入
队列的包继续流动。输入为业务定义、速率、活动状态；输出调用 Simulation.emit_data。
底层只依赖整数纳秒定时器，不使用 Socket，也不直接修改接收统计。
"""


# 速率动作的唯一取值定义；环境动作空间和内核校验共同引用，避免两处不一致。
RATE_FACTORS = (0.0, 0.5, 1.0)


class TrafficSource:
    def __init__(self, sim, service, rate_bps):
        self.sim, self.service = sim, service
        self.rate_bps = int(rate_bps)
        self.active = False
        self.online = sim.state.node_online[service.src_node_id]
        self.epoch = 0
        self._timer = None

    def interval_ns(self):
        """向上取整，避免舍入后实际发送速率略高于配置速率。"""
        if not self.rate_bps:
            return 0
        numerator = self.sim.config['payload_bytes'] * 8 * 1_000_000_000
        return (numerator + self.rate_bps - 1) // self.rate_bps

    def _reschedule(self, delay_ns):
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        if self.active and self.online and self.rate_bps and self.sim.now < self.sim.end_ns:
            self._timer = self.sim.schedule(self.sim.now + delay_ns, self._send)

    def set_active(self, value):
        if bool(value) != self.active:
            self.active = bool(value)
            self._reschedule(0)

    def refresh_online(self):
        online = self.sim.state.node_online[self.service.src_node_id]
        if online != self.online:
            self.online = online
            self._reschedule(0)

    def set_rate(self, bps):
        """本地速率修改真实改变发包间隔，0 表示暂停；不修改任何远端队列。"""
        if type(bps) is not int or bps < 0:
            raise ValueError('速率必须是非负整数 bit/s')
        if bps > self.sim.config['payload_bytes'] * 8 * 1_000_000_000:
            raise ValueError('该速率超过整数纳秒时钟精度')
        self.rate_bps = bps
        self.sim.log('source-state', {'event': 'rate_update', 'time_ns': self.sim.now,
                     'service_id': self.service.service_id, 'rate_bps': bps, 'epoch': self.epoch})
        self._reschedule(self.interval_ns())  # 调速后等一个新间隔，不额外补发一个包。

    def _send(self):
        self._timer = None
        if not (self.active and self.rate_bps and self.sim.now < self.sim.end_ns
                and self.sim.state.node_online[self.service.src_node_id]):
            return
        self.sim.emit_data(self.service.service_id, self.epoch)
        self._timer = self.sim.schedule(self.sim.now + self.interval_ns(), self._send)
