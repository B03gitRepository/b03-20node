"""Two Agent2Agent 1.0 JSON-RPC agents for the B03 packet work order.

The role payload is a raw JSON Part. Protobuf Value would coerce integer
nanoseconds to doubles. Each role has its own official SDK task store.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import threading
import time
import uuid
from importlib.metadata import version

SCHEMA = "b03.packet.role.v1"
ROUTES = {
    "observe": ("controller", "agent_om"),
    "control_request": ("agent_om", "agent_rc"),
    "verify": ("controller", "agent_om"),
}


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _message_id(kind, work_id, snapshot_id, parent):
    return uuid.uuid5(uuid.NAMESPACE_URL, digest([SCHEMA, kind, work_id, snapshot_id, parent])).hex


def _app(role, url, handle):
    from a2a.helpers import new_task_from_user_message
    from a2a.server.agent_execution import AgentExecutor
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
    from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
    from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Part, Role
    from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError
    from starlette.applications import Starlette

    class PacketExecutor(AgentExecutor):
        async def execute(self, context, event_queue):
            message = context.message
            if message.role != Role.ROLE_USER or len(message.parts) != 1:
                raise ValueError("One A2A user message with a raw JSON Part is required")
            part = message.parts[0]
            if part.media_type != "application/json" or not part.raw:
                raise ValueError("Expected a raw application/json Part")
            raw = bytes(part.raw)
            request = json.loads(raw)
            if (canonical(request) != raw or request["schema"] != SCHEMA
                    or ROUTES.get(request["kind"]) != (request["sender"], role)
                    or request["receiver"] != role or request["message_id"] != message.message_id
                    or request["work_id"] != message.context_id):
                raise ValueError("Invalid role, context or request binding")
            task = context.current_task or new_task_from_user_message(message)
            if context.current_task is None:
                await event_queue.enqueue_event(task)
            updater = TaskUpdater(event_queue, task.id, task.context_id)
            await updater.start_work()
            result = await asyncio.to_thread(handle, request)
            artifact = {
                "schema": SCHEMA, "sender": role, "receiver": request["sender"],
                "message_id": request["message_id"] + "/result",
                "parent_message_id": request["message_id"], "work_id": request["work_id"],
                "snapshot_id": request["snapshot_id"], "request_sha256": digest(request),
                "payload": result,
            }
            await updater.add_artifact(parts=[Part(raw=canonical(artifact), media_type="application/json")],
                                       artifact_id=request["message_id"] + "/artifact",
                                       name="B03 packet role result", append=False, last_chunk=True)
            await updater.complete()

        async def cancel(self, context, event_queue):
            raise UnsupportedOperationError(message="Packet role requests use blocking SendMessage")

    class PacketHandler(DefaultRequestHandler):
        # Adapt the T079 asynchronous send binding to this run's packet capability.
        # The role TaskStores and immutable message bindings have process lifetime.
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.bindings = {}
            self.binding_lock = asyncio.Lock()

        async def on_message_send(self, params, context):
            message = params.message
            if len(message.parts) != 1:
                raise InvalidParamsError(message="Expected one packet role Part")
            try:
                body = json.loads(bytes(message.parts[0].raw))
                expected = _message_id(body["kind"], body["work_id"], body["snapshot_id"],
                                       body["parent_message_id"])
                if (expected != message.message_id or body["message_id"] != expected
                        or message.context_id != body["work_id"] or body["receiver"] != role):
                    raise ValueError("Packet logical operation or role mismatch")
            except (KeyError, ValueError, TypeError) as exc:
                raise InvalidParamsError(message=str(exc)) from exc
            signature = hashlib.sha256(params.SerializeToString(deterministic=True)).digest()
            async with self.binding_lock:
                row = self.bindings.get(expected)
                if row is None:
                    future = asyncio.create_task(super().on_message_send(params, context))
                    self.bindings[expected] = (signature, future)
                else:
                    prior, future = row
                    if prior != signature:
                        raise InvalidParamsError(message="Conflicting duplicate packet SendMessage")
            return await asyncio.shield(future)

    description = ("Diagnose received observations and verify device query receipts"
                   if role == "agent_om" else "Propose an authorized B03 joint allocation")
    card = AgentCard(name="B03 packet " + role, description=description, version="1",
                     default_input_modes=["application/json"], default_output_modes=["application/json"],
                     capabilities=AgentCapabilities(streaming=False),
                     supported_interfaces=[AgentInterface(protocol_binding="JSONRPC",
                                                          protocol_version="1.0", url=url)],
                     skills=[AgentSkill(id="b03.packet." + role, name=role,
                                        description=description, tags=["b03", "packet"])])
    handler = PacketHandler(agent_executor=PacketExecutor(), agent_card=card,
                                    task_store=InMemoryTaskStore())
    return Starlette(routes=[*create_agent_card_routes(card),
                             *create_jsonrpc_routes(handler, "/", enable_v0_3_compat=False)])


class PacketA2A:
    """Own two local listeners, or connect to already configured role endpoints."""

    def __init__(self, handle, urls=None):
        import httpx

        self.client = httpx.Client(trust_env=False, timeout=60)
        self.handles = []
        self.sdk = version("a2a-sdk")
        self.urls = dict(urls or {})
        try:
            if urls is None:
                import uvicorn

                for role in ("agent_om", "agent_rc"):
                    sock = socket.socket()
                    try:
                        sock.bind(("127.0.0.1", 0))
                        sock.setblocking(False)
                        url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
                        self.urls[role] = url
                        server = uvicorn.Server(uvicorn.Config(
                            _app(role, url, handle), log_level="warning", access_log=False,
                            lifespan="off", timeout_graceful_shutdown=10))
                        worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                                                  name="b03-packet-" + role, daemon=True)
                        worker.start()
                        self.handles.append((server, worker, sock))
                    except BaseException:
                        sock.close()
                        raise
                deadline = time.monotonic() + 15
                while not all(server.started for server, _, _ in self.handles):
                    if time.monotonic() >= deadline or not all(w.is_alive() for _, w, _ in self.handles):
                        raise RuntimeError("A2A role listeners did not start")
                    time.sleep(.01)
            if set(self.urls) != {"agent_om", "agent_rc"}:
                raise ValueError("urls must supply agent_om and agent_rc")
        except BaseException:
            self.close()
            raise

    def _rpc(self, url, method, params):
        request_id = uuid.uuid4().hex
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        import httpx
        raw = canonical(request)
        for attempt in (1, 2):
            try:
                response = self.client.post(url, content=raw,
                                            headers={"Content-Type": "application/json", "A2A-Version": "1.0"})
                break
            except httpx.TransportError:
                if attempt == 2:
                    raise
        response.raise_for_status()
        obj = response.json()
        if obj.get("jsonrpc") != "2.0" or obj.get("id") != request_id or "error" in obj:
            raise RuntimeError(f"A2A {method} failed: {obj.get('error', 'response binding')}")
        return obj["result"], {"method": method, "http_status": response.status_code, "rpc_id": request_id,
                              "attempts": attempt, "request_bytes": len(raw), "response_bytes": len(response.content)}

    def exchange(self, kind, work_id, snapshot_id, payload, *, parent_message_id=None):
        sender, receiver = ROUTES[kind]
        message_id = _message_id(kind, work_id, snapshot_id, parent_message_id)
        request = {
            "schema": SCHEMA, "kind": kind, "sender": sender, "receiver": receiver,
            "work_id": work_id, "snapshot_id": snapshot_id, "message_id": message_id,
            "parent_message_id": parent_message_id, "payload": payload,
        }
        raw = canonical(request)
        params = {"message": {"messageId": message_id, "contextId": work_id, "role": "ROLE_USER",
                              "parts": [{"raw": base64.b64encode(raw).decode("ascii"),
                                         "mediaType": "application/json"}]},
                  "configuration": {"returnImmediately": False}}
        started = time.perf_counter_ns()
        result, send = self._rpc(self.urls[receiver], "SendMessage", params)
        sent = result["task"]
        actual, get = self._rpc(self.urls[receiver], "GetTask", {"id": sent["id"]})
        if (sent != actual or actual.get("contextId") != work_id
                or actual.get("status", {}).get("state") != "TASK_STATE_COMPLETED"):
            raise RuntimeError("A2A task did not return a matching completed GetTask readback")
        artifacts = actual.get("artifacts", [])
        if len(artifacts) != 1 or len(artifacts[0].get("parts", [])) != 1:
            raise ValueError("Expected one packet role artifact")
        item = artifacts[0]
        part = item["parts"][0]
        if part.get("mediaType") != "application/json" or item["artifactId"] != message_id + "/artifact":
            raise ValueError("Unexpected packet role artifact type or identity")
        encoded = base64.b64decode(part["raw"], validate=True)
        artifact = json.loads(encoded)
        binding = {
            "schema": SCHEMA, "sender": receiver, "receiver": sender, "work_id": work_id,
            "snapshot_id": snapshot_id, "parent_message_id": message_id,
            "message_id": message_id + "/result", "request_sha256": digest(request),
        }
        if canonical(artifact) != encoded or any(artifact.get(k) != v for k, v in binding.items()):
            raise ValueError("A2A artifact is not bound to the request")
        receipt = {
            "kind": kind, "work_id": work_id, "snapshot_id": snapshot_id, "message_id": message_id,
            "parent_message_id": parent_message_id, "sender": sender, "receiver": receiver,
            "request_sha256": binding["request_sha256"], "artifact_sha256": digest(artifact),
            "request_bytes": len(raw), "artifact_bytes": len(encoded), "task_id": sent["id"],
            "artifact_id": item["artifactId"], "task_readback_matched": True,
            "protocol": "A2A 1.0", "binding": "JSONRPC", "sdk_version": self.sdk,
            "idempotency": "in_memory_message_binding", "restart_recovery": False,
            "calls": [send, get], "wall_ns": time.perf_counter_ns() - started,
        }
        return artifact["payload"], receipt

    def close(self):
        for server, _, _ in self.handles:
            server.should_exit = True
        for _, worker, sock in self.handles:
            worker.join(12)
            sock.close()
        self.handles.clear()
        self.client.close()
