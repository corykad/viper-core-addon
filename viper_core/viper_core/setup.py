"""First-run setup for a new household, with explicit device selection."""

import asyncio
import time
from copy import deepcopy
from urllib.parse import urlsplit

from . import vision
from .ha import HomeAssistantClient
from .ring_camera import inventory


class SetupService:
    def __init__(self, controls, ha, config, events, listener):
        self.controls, self.ha, self.config = controls, ha, config
        self.events, self.listener = events, listener
        self._entities = []
        self._checked_at = 0
        self._discovery_error = ""
        self._native_ring_ids = None

    def discover(self, force=False):
        if force or time.monotonic() - self._checked_at > 20:
            try:
                states = self.ha.get_states()
                self._entities = [{"id": item["entity_id"], "name": (item.get("attributes") or {}).get("friendly_name") or item["entity_id"],
                                   "state": item.get("state", "unknown")}
                                  for item in states if isinstance(item, dict) and str(item.get("entity_id", "")).startswith(("event.", "binary_sensor.", "media_player.", "tts.", "switch.", "camera."))]
                self._discovery_error = ""
                if isinstance(self.ha, HomeAssistantClient) and self.ha.available():
                    try:
                        self._native_ring_ids = {item["entity_id"] for item in asyncio.run(
                            inventory(self.ha.websocket_url(), self.ha.token))}
                    except Exception:
                        self._native_ring_ids = set()
                        self._discovery_error = "Ring entities could not be verified. Check the Ring integration, then refresh devices."
            except Exception:
                self._entities = []
                self._discovery_error = "Home Assistant entities could not be loaded. Check HA Status, then refresh devices."
            self._checked_at = time.monotonic()
        return self._entities

    def snapshot(self):
        settings = self.controls.state.get("settings", {})
        required = []
        if not settings.get("doorbell_listener_enabled"):
            required.append("Enable automatic doorbell alerts.")
        if not self.controls.state.get("armed", True):
            required.append("Arm Viper on the Dashboard before testing doorbells.")
        for door in ("front", "back"):
            if door == "back" and not settings.get("back_door_enabled"):
                continue
            if not settings.get(f"{door}_door_trigger"):
                required.append(f"Select the {door} doorbell event.")
            source = settings.get(f"{door}_door_video_source") or "rtsp"
            key, label = ("camera_entity", "Ring live-view camera") if source == "ring_native" else ("stream_url", "live RTSP stream")
            if not settings.get(f"{door}_door_{key}"):
                required.append(f"Select the {door} {label}.")
        if not self.controls.speaker_targets("doorbell")["ha"] and not self.controls.speaker_targets("doorbell")["alexa"]:
            required.append("Select a speaker and make sure Viper is not muted.")
        effective = self.controls.effective_config(self.config)
        provider = settings.get("ai_provider", "openai")
        if not getattr(effective, f"{provider}_api_key", ""):
            required.append(f"Add your {provider.title()} API key for image descriptions.")
        if settings.get("tts_engine") == "home_assistant" and not settings.get("tts_entity") and self.controls.speaker_targets("doorbell")["ha"]:
            required.append("Select a Home Assistant speech provider, or choose AI speech on the Voice page.")
        return {"entities": self.discover(), "discovery_error": self._discovery_error,
                "missing": required, "checks": deepcopy(self.controls.state.get("setup_checks", {})),
                "listener": dict(self.listener.status), "complete": bool(self.controls.state.get("setup_complete"))}

    def handle(self, action, payload):
        try:
            if action == "refresh":
                self.discover(True)
                return {"ok": not self._discovery_error, "message": self._discovery_error or "Device list refreshed.", "state": self.controls.public_state()}
            if action == "save":
                return self.save(payload)
            if action == "speaker-test":
                result = self.events._speak("Viper Setup", "This is your doorbell speaker test. Your speaker is ready.", "doorbell") or {}
                ok = bool(result.get("sent"))
                message = "Test accepted by the speaker service. Confirm that you heard it before finishing setup." if ok else result.get("error") or "No speaker accepted the test."
                return self._record("speaker", ok, message)
            if action in {"front-test", "back-test"}:
                door = action.split("-")[0]
                if door == "back" and not self.controls.state["settings"].get("back_door_enabled"):
                    raise ValueError("Enable the back door before testing it.")
                description = vision.describe_doorbell(self.controls.effective_config(self.config), self.ha, door)
                return self._record(door, bool(description), description or "No image description returned. Check the selected camera, API key and model on the Doorbells page.")
            if action == "finish":
                state = self.snapshot()
                checks = state["checks"]
                needed = ["speaker", "front"] + (["back"] if self.controls.state["settings"].get("back_door_enabled") else [])
                if state["missing"] or any(not checks.get(key, {}).get("ok") for key in needed):
                    raise ValueError("Complete the missing settings and pass the speaker and camera tests first.")
                if self.listener.status.get("connection") != "connected" or not self.listener.status.get("last_event_at") or self.listener.status["last_event_at"] < self.controls.state.get("setup_saved_at", 0):
                    raise ValueError("Press the real doorbell and confirm its event appears before finishing setup.")
                if "back" in needed and any((self.listener.status.get("door_events") or {}).get(door, 0) < self.controls.state.get("setup_saved_at", 0) for door in ("front", "back")):
                    raise ValueError("Press both doorbells after saving setup and confirm both announcements.")
                if payload.get("heard") != "true":
                    raise ValueError("Confirm that you heard the speaker test and real doorbell announcement.")
                previous = deepcopy(self.controls.state)
                self.controls.state["setup_complete"] = True
                self._persist(previous)
                return {"ok": True, "message": "Setup complete. Create a Home Assistant backup now.", "state": self.controls.public_state()}
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": False, "message": "Unknown setup action."}

    def _record(self, name, ok, message):
        previous = deepcopy(self.controls.state)
        self.controls.state.setdefault("setup_checks", {})[name] = {"ok": ok, "message": message, "timestamp": int(time.time())}
        self._persist(previous)
        return {"ok": ok, "message": message, "state": self.controls.public_state()}

    def _persist(self, previous):
        if self.controls._save() is False:
            self.controls.state = previous
            raise ValueError("Setup could not be saved. Check available storage in HA Status and try again.")

    def save(self, payload):
        settings = deepcopy(self.controls.state["settings"])
        known = {item["id"] for item in self.discover(True)}
        back = payload.get("back_door_enabled") == "true"
        motion_enabled = payload.get("doorbell_motion_enabled") == "true"
        for door in ("front", "back"):
            source = str(payload.get(f"{door}_door_video_source") or settings.get(f"{door}_door_video_source") or "rtsp")
            if source not in {"rtsp", "ring_native"}:
                raise ValueError(f"Select a valid video source for the {door} door.")
            camera = str(payload.get(f"{door}_door_camera_entity") or "").strip()
            if camera and (camera not in known or not camera.startswith("camera.")):
                raise ValueError(f"Select an available {door} camera.")
            if source == "ring_native" and (door == "front" or back) and camera:
                if not camera.endswith("_live_view") or (self._native_ring_ids is not None and camera not in self._native_ring_ids):
                    raise ValueError(f"Select the built-in Ring live-view camera for the {door} door.")
            if source == "ring_native" and not camera and (door == "front" or back):
                raise ValueError(f"Select a Ring live-view camera for the {door} door.")
            settings[f"{door}_door_video_source"] = source
            settings[f"{door}_door_camera_entity"] = camera
            trigger = str(payload.get(f"{door}_door_trigger") or "").strip()
            if trigger and (trigger not in known or not trigger.startswith(("event.", "binary_sensor."))):
                raise ValueError(f"Select an available {door} doorbell event.")
            if source == "ring_native" and (door == "front" or back) and trigger:
                if not trigger.startswith("event.") or not trigger.endswith("_ding") or (self._native_ring_ids is not None and trigger not in self._native_ring_ids):
                    raise ValueError(f"Select the built-in Ring ding event for the {door} door.")
            switch = str(payload.get(f"{door}_door_live_stream_switch") or "").strip()
            if switch and (switch not in known or not switch.startswith("switch.")):
                raise ValueError(f"Select an available {door} stream switch or leave it blank.")
            settings[f"{door}_door_trigger"] = trigger
            settings[f"{door}_door_live_stream_switch"] = switch
            motion = str(payload.get(f"{door}_door_motion_trigger") or "").strip()
            if motion and (motion not in known or not motion.startswith("event.") or not motion.endswith("_motion")):
                raise ValueError(f"Select an available Ring motion event for the {door} door.")
            if motion_enabled and source == "ring_native" and (door == "front" or back):
                if not motion or (self._native_ring_ids is not None and motion not in self._native_ring_ids):
                    raise ValueError(f"Select a built-in Ring motion event for the {door} door.")
            settings[f"{door}_door_motion_trigger"] = motion
            stream = str(payload.get(f"{door}_door_stream_url") or "").strip()
            if stream:
                parsed = urlsplit(stream)
                if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
                    raise ValueError(f"Enter a valid RTSP URL for the {door} door.")
                settings[f"{door}_door_stream_url"] = stream
        if back and settings["front_door_trigger"] and settings["front_door_trigger"] == settings["back_door_trigger"]:
            raise ValueError("Choose different events for the front and back doors.")
        settings["back_door_enabled"] = back
        settings["doorbell_listener_enabled"] = payload.get("doorbell_listener_enabled") == "true"
        settings["doorbell_motion_enabled"] = motion_enabled
        tts = str(payload.get("tts_entity") or "")
        if tts and (tts not in known or not tts.startswith("tts.")):
            raise ValueError("Select an available speech provider.")
        settings["tts_entity"] = tts
        provider = str(payload.get("ai_provider") or "openai")
        if provider not in {"openai", "gemini"}:
            raise ValueError("Select an AI provider.")
        settings["ai_provider"] = provider
        for key in ("gemini_api_key", "openai_api_key"):
            if str(payload.get(key) or "").strip():
                settings[key] = str(payload[key]).strip()
        speaker = str(payload.get("speaker_entity") or "")
        if speaker and (speaker not in known or not speaker.startswith("media_player.")):
            raise ValueError("Select an available media player.")
        speaker_type = "alexa" if payload.get("speaker_type") == "alexa" else "ha"
        previous = deepcopy(self.controls.state)
        self.controls.state["settings"] = settings
        if speaker:
            self.controls.state["speakers"]["doorbell speaker"] = {"id": speaker, "type": speaker_type, "enabled": True, "doorbell": True, "fridge": False, "utilities": True}
        else:
            self.controls.state["speakers"].pop("doorbell speaker", None)
        self.controls.state.update(setup_complete=False, setup_checks={}, setup_saved_at=int(time.time()))
        self._persist(previous)
        return {"ok": True, "message": "Setup saved. Run the speaker and camera tests, then press the real doorbell.", "state": self.controls.public_state()}
