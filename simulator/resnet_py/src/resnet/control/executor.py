"""节点执行端：收到命令 → 校验 → 修改本地配置 → 返回执行回执。

只有仿真网络实际送达命令后才调用 receive。这里不选路径、不获取远端状态，
也不直接通知控制端；返回的回执仍须通过网络发送。

输入为命令、网络来源、接收时刻与包编号。输出为 ACK 字典，同时记录执行日志。
本节点的路由安装和业务启停由构造时绑定的本地函数完成。业务需求通过
demand_state(service_id) 读取；这些函数只允许访问当前节点。
"""
from __future__ import annotations

from copy import deepcopy
import json

from resnet.contracts.control import command_precheck, require_int, validate_command


class NodeExecutor:
    """保存一个节点的命令执行记录，统一处理校验、执行和回执。

    service_specs 是共享的业务定义目录，本类只读；它不保存活动状态或发送速率。
    prepared/installed 按 (业务编号, 路由版本) 保存准备和安装记录。
    source_enabled 表示本地发送授权，active_epochs 保留最近启用的路由版本；
    停源后仍保留版本，供查询和旧规则清理使用。实际发包由 TrafficSource 负责。
    _history 保存第一次执行回执，重复命令返回该结果，不能再次修改节点。
    """

    def __init__(self, node_id, controller_id, services, neighbors, install_rule,
                 remove_rule, activate_source, logger, now_fn, demand_state, deactivate_source):
        self.node_id = node_id
        self.controller_id = controller_id
        self.service_specs = services  # 引用同一份静态定义，不创建每个节点各自的业务副本。
        self.neighbors = dict(neighbors)
        self.install_rule = install_rule
        self.remove_rule = remove_rule
        self.activate_source = activate_source
        self.deactivate_source = deactivate_source
        self.demand_state = demand_state
        self.logger = logger
        self.now_fn = now_fn

        self.version = 0  # 本节点已执行命令的配置版本，用于拦截过时的修改。
        self.prepared: dict[tuple[str, int], dict] = {}
        self.installed: dict[tuple[str, int], dict] = {}
        self.source_enabled: dict[str, bool] = {}
        self.active_epochs: dict[str, int] = {}
        self._history: dict[str, tuple[str, dict]] = {}

    @staticmethod
    def _identity(command):
        """生成可比较的命令内容；字典字段排列不影响比较结果。"""
        return json.dumps(command, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _parameters(action):
        """准备与安装的操作名称不同，其余参数应完全相同。"""
        return {key: value for key, value in action.items() if key != "op"}

    def receive(self, command, received_at_ns, packet_uid, source_node_id):
        """处理一次实际送达，依次完成来源检查、重复识别、校验、执行和记账。

        检查使用当前执行时刻 now；消息在队列中等待的时间不会被扣除。
        返回 ACK 只表示本节点结果，控制端收到回执后才能确认远端状态。
        """
        require_int(received_at_ns, "received_at_ns")
        require_int(packet_uid, "packet_uid")
        now = require_int(self.now_fn(), "now_fn返回时间")
        before = self.version
        action = command.get("action", {}) if isinstance(command, dict) else {}
        sid = action.get("service_id") if isinstance(action, dict) else None
        if not isinstance(sid, str):
            sid = None
        signature, remembered = None, False
        original = None
        executed_at = None
        status, reason = "rejected", None

        # 1. 先检查网络实际来源；未经授权的报文不能污染命令历史。
        if source_node_id != self.controller_id:
            reason = "unauthorized_controller"
        elif received_at_ns > now:
            reason = "future_received_time"
        else:
            try:
                validate_command(command)
                signature = self._identity(command)
            except (ValueError, TypeError):
                reason = "malformed_command"
            if reason is None:
                # 2. 相同编号和内容是重传；相同编号却不同内容则拒收。
                previous = self._history.get(command["command_id"])
                if previous is not None:
                    if previous[0] != signature:
                        reason = "command_id_conflict"
                    else:
                        status, reason = "duplicate", "already_processed"
                        original = deepcopy(previous[1])
                    remembered = True
                else:
                    # 3. 检查命令有效期、目标、配置版本和本地动作前置条件。
                    reason = command_precheck(command, now_ns=now, target_node_id=self.node_id,
                                              current_version=self.version)
                    if reason is None:
                        reason = self._validate_local(action, now)
                    if reason is None:
                        # 4. 本地修改成功后再更新配置版本。适配函数失败时必须不改设备。
                        try:
                            self._apply_local(action)
                        except Exception as error:
                            reason = f"adapter_failure:{type(error).__name__}:{error}"
                        else:
                            status, reason, executed_at = "applied", action["op"], now
                            if action["op"] != "query":
                                self.version = command["new_version"]

        # 5. 生成回执。重传回执保留第一次的版本、授权和执行结果。
        ack = {"kind": "ack", "node_id": self.node_id,
               "command_id": command.get("command_id", "") if isinstance(command, dict) else "",
               "plan_id": command.get("plan_id", "") if isinstance(command, dict) else "",
               "status": status, "reason": reason, "version": self.version,
               "executed_at_ns": executed_at, "active_epoch": self.active_epochs.get(sid, 0)}
        if original is not None:
            ack.update(version=original["version"], active_epoch=original["active_epoch"],
                       original_status=original["status"], original_reason=original["reason"],
                       original_executed_at_ns=original["executed_at_ns"])
        ack["source_enabled"] = (original["source_enabled"] if original is not None
                                 else self.source_enabled.get(sid, False))
        if signature is not None and not remembered:
            self._history[command["command_id"]] = (signature, deepcopy(ack))

        # 日志记录当前本地状态；重复命令的回执与此刻状态可能不同。
        record = {"time_ns": now, "node_id": self.node_id, "command": deepcopy(command),
                  "received_at_ns": received_at_ns, "packet_uid": packet_uid,
                  "before_version": before, "after_version": self.version,
                  "status": status, "reason": reason, "executed_at_ns": executed_at,
                  "active_epoch": self.active_epochs.get(sid, 0)}
        if original is not None:
            record.update(original_status=original["status"],
                          original_executed_at_ns=original["executed_at_ns"])
        record["source_enabled"] = self.source_enabled.get(sid, False)
        self.logger(record)
        return ack

    def _validate_local(self, action, now):
        """检查本节点业务、需求版本、邻接关系以及准备/安装记录。"""
        op, sid, epoch = action["op"], action["service_id"], action["epoch"]
        if sid not in self.service_specs:
            return "unknown_service"
        if op in {"activate", "deactivate"}:
            if self.service_specs[sid].src_node_id != self.node_id:
                return "not_service_source"
        if op == "activate":
            demand = self.demand_state(sid)
            if not demand["registered"] or not demand["active"]:
                return "demand_not_active"
            if action.get("demand_revision") != demand["revision"]:
                return "demand_revision_conflict"
        if op == "deactivate":
            if epoch <= 0 or self.active_epochs.get(sid, 0) != epoch:
                return "source_epoch_mismatch"
            return None
        if op in {"cleanup", "abort"} and not self.source_enabled.get(sid, False):
            return None if epoch > 0 else "invalid_epoch"
        if op == "query":
            return None
        if epoch <= 0:
            return "invalid_epoch"

        key = (sid, epoch)
        if op in {"prepare", "install"}:
            if self.neighbors.get(action["link_id"]) != action["next_node_id"]:
                return "invalid_local_next_hop"
            if action["lease_until_ns"] <= now:
                return "prepare_lease_expired"
            if action["reserved_bps"] <= 0:
                return "invalid_reservation"
        if op == "prepare":
            prior = self.prepared.get(key) or self.installed.get(key)
            if prior is not None and self._parameters(prior) != self._parameters(action):
                return "epoch_parameter_conflict"
        elif op == "install":
            prepared = self.prepared.get(key)
            if prepared is None:
                return "not_prepared"
            if prepared["lease_until_ns"] <= now:
                return "prepare_lease_expired"
            if self._parameters(prepared) != self._parameters(action):
                return "prepare_parameter_mismatch"
        elif op == "activate":
            prepared, installed = self.prepared.get(key), self.installed.get(key)
            if prepared is None or installed is None:
                return "epoch_not_installed"
            if action["reserved_bps"] != prepared["reserved_bps"]:
                return "reservation_mismatch"
        elif op in {"cleanup", "abort"}:
            if self.active_epochs.get(sid) == epoch:
                return "epoch_is_active_at_source"
        return None

    def _apply_local(self, action):
        """调用已绑定到本节点的修改函数，成功后更新执行记录。"""
        op, sid, epoch = action["op"], action["service_id"], action["epoch"]
        key = (sid, epoch)
        if op == "prepare":
            self.prepared[key] = deepcopy(action)
        elif op == "install":
            self.install_rule(deepcopy(action))
            self.installed[key] = deepcopy(action)
        elif op == "activate":
            self.activate_source(deepcopy(action))
            self.active_epochs[sid] = epoch
            self.source_enabled[sid] = True
        elif op == "deactivate":
            self.deactivate_source(deepcopy(action))
            self.source_enabled[sid] = False
        elif op in {"cleanup", "abort"}:
            # 目的节点虽无下一跳规则，也须退役版本以拦截仍在传播的旧包。
            # 不同编号的重复清理由本地移除函数保证幂等；相同编号已在 receive 拦截。
            self.remove_rule(deepcopy(action))
            self.prepared.pop(key, None)
            self.installed.pop(key, None)
        # query 只读取状态并生成回执，不修改本地配置。
