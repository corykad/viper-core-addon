import json
import asyncio
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ha_addons" / "viper_core"))
from viper_core.config import CoreConfig
from viper_core.control import ControlState, ControlApi
from viper_core.doorbell_listener import doorbell_transition
from viper_core.doorbell_listener import DoorbellListener
from viper_core.setup import SetupService
from viper_core.events import EventProcessor
from viper_core.ha import HomeAssistantClient
from viper_core.web_ui import render_page


class DoorbellLatencyTests(unittest.TestCase):
    def _processor(self, muted=False):
        settings = {"doorbell_listener_enabled": True, "front_door_video_source": "ring_native"}
        config = SimpleNamespace(doorbell_video_mode="fast", doorbell_dedupe_seconds=30,
                                 doorbell_speaker_service="")
        controls = SimpleNamespace(
            state={"settings": settings}, feature_enabled=lambda _feature: True,
            effective_config=lambda _config: config,
            public_state=lambda: {"armed": True, "global_mute": muted},
        )
        return EventProcessor(config, SimpleNamespace(available=lambda: True), controls)

    def test_real_press_starts_one_chime_before_vision(self):
        processor = self._processor()
        order = []
        processor._play_event_chime = Mock(side_effect=lambda *_args: order.append("chime"))
        processor._notify = Mock(side_effect=lambda *_args, **_kwargs: order.append("notify"))

        def thread_factory(*, target, args, **_kwargs):
            return SimpleNamespace(start=lambda: target(*args))

        with patch("viper_core.events.threading.Thread", side_effect=thread_factory), \
                patch("viper_core.events.vision.describe_doorbell", side_effect=lambda *_args: order.append("vision") or "Clear porch"):
            processor.handle("doorbell", {"door": "front", "action": "pressed", "source": "ha_listener"})

        self.assertEqual(order, ["chime", "vision", "notify"])
        self.assertFalse(processor._notify.call_args.kwargs["play_chime"])

    def test_muted_press_does_not_start_early_chime(self):
        processor = self._processor(muted=True)
        processor._play_event_chime = Mock()
        processor._notify = Mock()
        with patch("viper_core.events.vision.describe_doorbell", return_value="Clear porch"):
            processor.handle("doorbell", {"door": "front", "action": "pressed", "source": "ha_listener"})
        processor._play_event_chime.assert_not_called()
        self.assertTrue(processor._notify.call_args.kwargs["play_chime"])


class CleanSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        self.controls = ControlState(self.path)
        self.ha = Mock()
        self.ha.get_states.return_value = [
            {"entity_id": entity, "state": "off", "attributes": {"friendly_name": entity}}
            for entity in ("event.front_ding", "event.back_ding", "event.front_motion", "event.back_motion",
                           "camera.front_live_view", "media_player.speaker", "tts.speech")
        ]
        self.events = Mock()
        self.events._speak.return_value = {"sent": True}
        self.listener = SimpleNamespace(status={"connection": "connected"})
        self.service = SetupService(self.controls, self.ha, CoreConfig(), self.events, self.listener)
        self.payload = {"front_door_trigger": "event.front_ding", "front_door_stream_url": "rtsp://example.test/front",
                        "front_door_video_source": "rtsp",
                        "speaker_entity": "media_player.speaker", "tts_entity": "tts.speech",
                        "openai_api_key": "test-only", "doorbell_listener_enabled": "true"}

    def test_new_install_is_empty_and_doorbell_only(self):
        self.assertEqual(self.controls.state["speakers"], {})
        self.assertFalse(self.controls.state["setup_complete"])
        self.assertEqual([key for key, value in self.controls.state["features"].items() if value], ["doorbell"])
        self.assertFalse(self.controls.state["settings"]["doorbell_listener_enabled"])
        self.assertEqual(self.controls.state["settings"]["front_door_live_stream_switch"], "")
        self.assertEqual(self.controls.state["settings"]["front_door_video_source"], "ring_native")

    def test_corrupt_state_fails_to_empty_defaults(self):
        self.path.write_text("{bad", encoding="utf-8")
        state = ControlState(self.path)
        self.assertEqual(state.state["speakers"], {})
        self.assertFalse(state.feature_enabled("hvac"))
        self.assertEqual(self.path.read_text(), "{bad")

    def test_empty_or_invalid_state_structure_uses_clean_defaults(self):
        for payload in ({}, {"speakers": []}, {"settings": "broken"}, {"chimes": {"events": []}}):
            with self.subTest(payload=payload):
                self.path.write_text(json.dumps(payload))
                state = ControlState(self.path)
                self.assertEqual(state.state["speakers"], {})
                self.assertFalse(state.feature_enabled("hvac"))

    def test_legacy_saved_state_preserved_without_inserting_speakers(self):
        self.path.write_text(json.dumps({"settings": {"front_door_stream_url": "rtsp://example.test/old"}, "speakers": {}}))
        state = ControlState(self.path)
        self.assertTrue(state.feature_enabled("hvac"))
        self.assertTrue(state.state["setup_complete"])
        self.assertFalse(state.state["settings"].get("doorbell_listener_enabled", False))
        self.assertEqual(state.state["speakers"], {})
        self.assertEqual(state.state["settings"]["front_door_stream_url"], "rtsp://example.test/old")
        self.assertEqual(state.state["settings"]["front_door_video_source"], "rtsp")

    def test_saved_clean_install_stays_clean(self):
        self.controls._save()
        state = ControlState(self.path)
        self.assertFalse(state.feature_enabled("hvac"))
        self.assertEqual(state.state["speakers"], {})

    def test_public_state_does_not_expose_stream_credentials(self):
        self.controls.state["settings"]["front_door_stream_url"] = "rtsp://private:secret@example.test/front"
        public = json.dumps(self.controls.public_state())
        self.assertNotIn("private:secret", public)
        self.assertEqual(self.controls.state["settings"]["front_door_stream_url"], "rtsp://private:secret@example.test/front")

    def test_disabled_devices_not_read_or_controlled(self):
        api = ControlApi(self.controls, self.ha)
        api.device_status()
        self.ha.get_state.assert_not_called()
        self.assertFalse(api.handle_post("/api/control/hvac", {"mode": "cool"})["ok"])
        self.ha.call_service.assert_not_called()

    def test_invalid_save_does_not_partially_mutate(self):
        before = self.controls.public_state()
        self.assertFalse(self.service.handle("save", {**self.payload, "speaker_entity": "media_player.missing"})["ok"])
        self.assertEqual(before, self.controls.public_state())

    def test_failed_disk_save_is_reported_and_does_not_apply_setup(self):
        before = self.controls.public_state()
        with patch.object(self.controls, "_save", return_value=False):
            result = self.service.handle("save", self.payload)
        self.assertFalse(result["ok"])
        self.assertIn("could not be saved", result["message"])
        self.assertEqual(before, self.controls.public_state())

    def test_setup_must_test_camera_speaker_and_real_press(self):
        self.assertTrue(self.service.handle("save", self.payload)["ok"])
        self.assertFalse(self.service.handle("finish", {"heard": "true"})["ok"])
        self.service.handle("speaker-test", {})
        with patch("viper_core.setup.vision.describe_doorbell", return_value="A person at the door."):
            self.assertTrue(self.service.handle("front-test", {})["ok"])
        self.assertFalse(self.service.handle("finish", {"heard": "true"})["ok"])
        self.listener.status["last_event_at"] = int(time.time())
        self.assertFalse(self.service.handle("finish", {})["ok"])
        self.assertTrue(self.service.handle("finish", {"heard": "true"})["ok"])

    def test_disabling_listener_prevents_finish(self):
        self.service.handle("save", {**self.payload, "doorbell_listener_enabled": "false"})
        self.assertIn("Enable automatic doorbell alerts.", self.service.snapshot()["missing"])

    def test_unselecting_speaker_removes_setup_route(self):
        self.service.handle("save", self.payload)
        self.service.handle("save", {**self.payload, "speaker_entity": ""})
        self.assertNotIn("doorbell speaker", self.controls.state["speakers"])

    def test_native_camera_setup_does_not_require_rtsp(self):
        payload = {**self.payload, "front_door_video_source": "ring_native",
                   "front_door_camera_entity": "camera.front_live_view", "front_door_stream_url": ""}
        result = self.service.handle("save", payload)
        self.assertTrue(result["ok"], result.get("message"))
        self.assertNotIn("Select the front live RTSP stream.", self.service.snapshot()["missing"])
        effective = self.controls.effective_config(CoreConfig())
        self.assertEqual(effective.front_door_video_source, "ring_native")
        self.assertEqual(effective.front_door_camera_entity, "camera.front_live_view")

    def test_native_motion_is_optional_and_requires_ring_event(self):
        payload = {**self.payload, "front_door_video_source": "ring_native",
                   "front_door_camera_entity": "camera.front_live_view",
                   "doorbell_motion_enabled": "true"}
        self.assertFalse(self.service.handle("save", payload)["ok"])
        self.assertFalse(self.service.handle("save", {**payload, "front_door_motion_trigger": "event.front_ding"})["ok"])
        self.assertTrue(self.service.handle("save", {**payload, "front_door_motion_trigger": "event.front_motion"})["ok"])
        self.assertTrue(self.controls.state["settings"]["doorbell_motion_enabled"])

    def test_native_camera_requires_valid_selection_in_live_mode(self):
        payload = {**self.payload, "front_door_video_source": "ring_native", "front_door_stream_url": ""}
        self.assertFalse(self.service.handle("save", payload)["ok"])
        self.assertFalse(self.service.handle("save", {**payload, "front_door_camera_entity": "camera.missing"})["ok"])
        self.controls.state["settings"]["doorbell_video_mode"] = "live"
        self.assertTrue(self.service.handle("save", {**payload, "front_door_camera_entity": "camera.front_live_view"})["ok"])

    def test_native_mode_can_be_changed_after_setup(self):
        payload = {**self.payload, "front_door_video_source": "ring_native",
                   "front_door_camera_entity": "camera.front_live_view"}
        self.assertTrue(self.service.handle("save", payload)["ok"])
        result = ControlApi(self.controls, self.ha).handle_post("/api/control/settings", {"doorbell_video_mode": "live"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.controls.state["settings"]["doorbell_video_mode"], "live")

    def test_native_setup_rejects_mqtt_press_and_non_ring_camera(self):
        ha = HomeAssistantClient("http://supervisor/core/api", "test-token")
        ha.get_states = self.ha.get_states
        ha.get_states.return_value += [
            {"entity_id": entity, "state": "off", "attributes": {}}
            for entity in ("binary_sensor.viper_front_door_ring_motion", "camera.other_live_view")
        ]
        self.service.ha = ha
        native = [{"entity_id": "event.front_ding"}, {"entity_id": "camera.front_live_view"}]
        payload = {**self.payload, "front_door_video_source": "ring_native",
                   "front_door_camera_entity": "camera.front_live_view"}
        with patch("viper_core.setup.inventory", new=AsyncMock(return_value=native)):
            self.assertTrue(self.service.handle("save", payload)["ok"])
            self.assertFalse(self.service.handle("save", {**payload,
                "front_door_trigger": "binary_sensor.viper_front_door_ring_motion"})["ok"])
            self.assertFalse(self.service.handle("save", {**payload,
                "front_door_camera_entity": "camera.other_live_view"})["ok"])
        self.assertEqual(self.controls.state["settings"]["front_door_trigger"], "event.front_ding")

    def test_legacy_ring_router_cannot_preempt_native_press(self):
        settings = self.controls.state["settings"]
        settings.update(doorbell_listener_enabled=True, front_door_video_source="ring_native",
                        front_door_trigger="event.front_door_ding")
        processor = EventProcessor(CoreConfig(), self.ha, self.controls)
        with patch("viper_core.events.vision.describe_doorbell", return_value="A person is at the door.") as describe, \
                patch.object(processor, "_notify") as notify:
            legacy = processor.handle("doorbell", {"door": "front", "action": "motion",
                                                   "entity_id": "binary_sensor.viper_front_door_ring_motion"})
            self.assertTrue(legacy["duplicate"])
            self.assertNotIn("doorbell:front", processor.last_event_by_key)
            native = processor.handle("doorbell", {"door": "front", "action": "pressed",
                                                   "entity_id": "event.front_door_ding", "source": "ha_listener"})
        self.assertTrue(native["ok"])
        describe.assert_called_once()
        notify.assert_called_once()

    def test_existing_household_can_migrate_only_doorbells(self):
        legacy = ControlState(self.path, profile="legacy")
        legacy.state["settings"].update(front_door_stream_url="rtsp://front", back_door_stream_url="rtsp://back",
                                         doorbell_video_mode="fast", ai_provider="gemini")
        speakers = deepcopy(legacy.state["speakers"])
        features = deepcopy(legacy.state["features"])
        payload = {
            "front_door_video_source": "ring_native", "back_door_video_source": "ring_native",
            "front_door_camera_entity": "camera.front_door_live_view",
            "back_door_camera_entity": "camera.back_door_live_view",
            "front_door_trigger": "event.front_door_ding", "back_door_trigger": "event.back_door_ding",
            "back_door_enabled": True, "doorbell_listener_enabled": True,
        }
        result = ControlApi(legacy, self.ha).handle_post("/api/control/settings", payload)
        self.assertTrue(result["ok"], result.get("message"))
        self.assertTrue(legacy.state["setup_complete"])
        self.assertEqual(legacy.state["speakers"], speakers)
        self.assertEqual(legacy.state["features"], features)
        self.assertEqual(legacy.state["settings"]["front_door_stream_url"], "rtsp://front")
        self.assertEqual(legacy.state["settings"]["back_door_stream_url"], "rtsp://back")
        for key, value in payload.items():
            self.assertEqual(legacy.state["settings"][key], value)

    def test_invalid_migration_keeps_saved_settings(self):
        before = deepcopy(self.controls.state["settings"])
        result = ControlApi(self.controls, self.ha).handle_post("/api/control/settings", {
            "front_door_trigger": "binary_sensor.mqtt_ding", "doorbell_listener_enabled": True})
        self.assertFalse(result["ok"])
        self.assertEqual(self.controls.state["settings"], before)

    def test_fresh_page_has_no_optional_device_navigation(self):
        html = render_page({"control": self.controls.public_state(), "setup": self.service.snapshot()})
        self.assertIn("Doorbell Setup", html)
        self.assertNotIn('href="?page=hvac"', html)
        self.assertNotIn('href="?page=vacuum"', html)

    def test_transition_filters_restore_attributes_and_second_bell(self):
        settings = {"front_door_trigger": "event.front_ding", "back_door_trigger": "binary_sensor.back_ding"}
        data = {"entity_id": "event.front_ding", "old_state": {"state": "old"}, "new_state": {"state": "new"}}
        self.assertEqual(doorbell_transition(data, settings)["door"], "front")
        self.assertIsNone(doorbell_transition({**data, "old_state": {"state": "new"}}, settings))
        self.assertIsNone(doorbell_transition({**data, "old_state": None}, settings))
        back = {"entity_id": "binary_sensor.back_ding", "old_state": {"state": "off"}, "new_state": {"state": "on"}}
        self.assertIsNone(doorbell_transition(back, settings))
        settings["back_door_enabled"] = True
        self.assertEqual(doorbell_transition(back, settings)["door"], "back")
        self.assertIsNone(doorbell_transition({**back, "new_state": {"state": "off"}}, settings))

    def test_first_ring_event_is_accepted_but_old_restore_is_not(self):
        settings = {"front_door_trigger": "event.front_ding"}
        data = {"entity_id": "event.front_ding", "old_state": {"state": "unknown"},
                "new_state": {"state": datetime.now(timezone.utc).isoformat()}}
        self.assertEqual(doorbell_transition(data, settings)["door"], "front")
        data["new_state"]["state"] = "2020-01-01T00:00:00+00:00"
        self.assertIsNone(doorbell_transition(data, settings))

    def test_native_motion_event_is_distinct_from_a_press(self):
        settings = {"front_door_trigger": "event.front_ding",
                    "front_door_motion_trigger": "event.front_motion", "doorbell_motion_enabled": True}
        data = {"entity_id": "event.front_motion", "old_state": {"state": "unknown"},
                "new_state": {"state": datetime.now(timezone.utc).isoformat()}}
        self.assertEqual(doorbell_transition(data, settings)["action"], "motion")
        settings["doorbell_motion_enabled"] = False
        self.assertIsNone(doorbell_transition(data, settings))


class ListenerConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_subscription_and_selected_event_over_websocket(self):
        from websockets.asyncio.server import serve
        controls = SimpleNamespace(state={"settings": {"doorbell_listener_enabled": True, "front_door_trigger": "event.front_ding"}}, feature_enabled=lambda feature: True)
        requests = []
        finished = asyncio.Event()

        async def endpoint(socket):
            await socket.send(json.dumps({"type": "auth_required"}))
            requests.append(json.loads(await socket.recv()))
            await socket.send(json.dumps({"type": "auth_ok"}))
            requests.append(json.loads(await socket.recv()))
            await socket.send(json.dumps({"id": 1, "type": "result", "success": True}))
            await socket.send(json.dumps({"type": "event", "event": {"data": {
                "entity_id": "event.front_ding", "old_state": {"state": "earlier"}, "new_state": {"state": "now"}}}}))
            await finished.wait()

        async with serve(endpoint, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            listener = DoorbellListener(SimpleNamespace(ha_url=f"http://127.0.0.1:{port}/api", ha_token="test-token"), controls, Mock())
            task = asyncio.create_task(listener.run())
            try:
                async def received():
                    while listener.pending.empty():
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(received(), 3)
                self.assertEqual(listener.pending.get_nowait()[0]["door"], "front")
                self.assertEqual(listener.status["connection"], "connected")
                self.assertEqual(requests, [{"type": "auth", "access_token": "test-token"}, {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}])
            finally:
                listener.stop_event.set()
                finished.set()
                await asyncio.wait_for(task, 3)


if __name__ == "__main__":
    unittest.main()
