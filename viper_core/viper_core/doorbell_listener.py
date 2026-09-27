"""HA state-change listener; no per-house YAML required."""

import asyncio
import json
import queue
import threading
import time
from datetime import datetime

import websockets


def doorbell_transition(data, settings):
    entity = data.get("entity_id", "")
    old, new = data.get("old_state") or {}, data.get("new_state") or {}
    before, after = old.get("state"), new.get("state")
    if before is None or after in {None, "unknown", "unavailable"} or before == after:
        return None
    if before in {"unknown", "unavailable"}:
        # Event entities can stay unknown until the first press after installation.
        # Accept only a current timestamp, not an old event restored on reconnect.
        if not entity.startswith("event."):
            return None
        try:
            event_time = datetime.fromisoformat(str(after).replace("Z", "+00:00")).timestamp()
            if not -2 <= time.time() - event_time <= 5:
                return None
        except (ValueError, TypeError):
            return None
    for door in ("front", "back"):
        if door == "back" and not settings.get("back_door_enabled"):
            continue
        if entity != settings.get(f"{door}_door_trigger"):
            continue
        if entity.startswith("binary_sensor.") and after != "on":
            continue
        if not entity.startswith(("event.", "binary_sensor.")):
            continue
        return {"door": door, "action": "pressed", "entity_id": entity, "source": "ha_listener"}
    return None


class DoorbellListener:
    def __init__(self, config, controls, handler):
        self.config, self.controls, self.handler = config, controls, handler
        self.stop_event = threading.Event()
        self.pending = queue.Queue(maxsize=8)
        self.status = {"connection": "disabled", "last_event_at": None, "message": "Enable automatic doorbell alerts in Setup."}

    def start(self):
        threading.Thread(target=lambda: asyncio.run(self.run()), daemon=True, name="doorbell-listener").start()
        threading.Thread(target=self._worker, daemon=True, name="doorbell-events").start()

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                payload, received = self.pending.get(timeout=1)
            except queue.Empty:
                continue
            try:
                settings = self.controls.state.get("settings", {})
                selected = settings.get(f"{payload['door']}_door_trigger") == payload.get("entity_id")
                if time.monotonic() - received < 30 and settings.get("doorbell_listener_enabled") and selected:
                    self.handler("doorbell", payload)
            except Exception:
                self.status["message"] = "A doorbell event could not be processed. Run the setup tests."
            finally:
                self.pending.task_done()

    async def run(self):
        base = self.config.ha_url.rstrip("/")
        url = base.removesuffix("/api") + "/websocket"
        url = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        while not self.stop_event.is_set():
            settings = self.controls.state.get("settings", {})
            if not settings.get("doorbell_listener_enabled") or not self.controls.feature_enabled("doorbell"):
                self.status.update(connection="disabled", message="Automatic doorbell alerts are disabled.")
                await asyncio.sleep(1)
                continue
            try:
                async with websockets.connect(url, open_timeout=10, ping_interval=20, ping_timeout=20, max_size=4 * 1024 * 1024) as socket:
                    await asyncio.wait_for(socket.recv(), 10)
                    await socket.send(json.dumps({"type": "auth", "access_token": self.config.ha_token}))
                    auth = json.loads(await asyncio.wait_for(socket.recv(), 10))
                    if auth.get("type") != "auth_ok":
                        raise RuntimeError("Authentication failed")
                    await socket.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
                    subscribed = json.loads(await asyncio.wait_for(socket.recv(), 10))
                    if not subscribed.get("success"):
                        raise RuntimeError("Subscription failed")
                    self.status.update(connection="connected", message="Listening for doorbell presses.")
                    while not self.stop_event.is_set():
                        settings = self.controls.state.get("settings", {})
                        if not settings.get("doorbell_listener_enabled") or not self.controls.feature_enabled("doorbell"):
                            break
                        try:
                            message = json.loads(await asyncio.wait_for(socket.recv(), 1))
                        except asyncio.TimeoutError:
                            continue
                        payload = doorbell_transition((message.get("event") or {}).get("data") or {}, settings)
                        if payload:
                            received_at = int(time.time())
                            self.status.update(last_event_at=received_at, last_door=payload["door"])
                            self.status.setdefault("door_events", {})[payload["door"]] = received_at
                            try:
                                self.pending.put_nowait((payload, time.monotonic()))
                            except queue.Full:
                                self.status["message"] = "Doorbell processing is busy; an event was skipped."
            except Exception:
                self.status.update(connection="reconnecting", message="Home Assistant event connection unavailable; retrying.")
                for _ in range(10):
                    if self.stop_event.is_set():
                        break
                    await asyncio.sleep(1)
