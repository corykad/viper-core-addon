import asyncio
import sys
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ha_addons" / "viper_core"))
from viper_core import live


class NativeLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_true_live_events_finish_after_worker_exits(self):
        with patch.object(live, "_run_gemini_true_live", new_callable=AsyncMock):
            events = list(live.gemini_true_live_doorbell_events(
                SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), lambda *_: None,
                "front", session_id="test-native-live-done",
            ))
        self.assertEqual([item["event"] for item in events], ["session", "done"])
        self.assertNotIn("test-native-live-done", live._LIVE_SESSION_COMMANDS)

    async def test_true_live_does_not_repeat_worker_done_event(self):
        async def finish(*args):
            args[-2].put(live._event("done", "Finished."))

        with patch.object(live, "_run_gemini_true_live", side_effect=finish):
            events = list(live.gemini_true_live_doorbell_events(
                SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), lambda *_: None,
                "front", session_id="test-native-live-single-done",
            ))
        self.assertEqual([item["event"] for item in events], ["session", "done"])

    async def test_true_live_uses_native_frames_without_rtsp(self):
        async def fake_stream(url, token, entity, seconds, callback, fps=1):
            self.assertEqual((url, token, entity, fps),
                             ("ws://supervisor/core/websocket", "test-token", "camera.front_live", 1))
            await callback({"jpeg": b"frame-one"})
            await callback({"jpeg": b"frame-two"})
            return {"frame_count": 2}

        session = SimpleNamespace(send_realtime_input=AsyncMock())
        ha = SimpleNamespace(websocket_url=lambda: "ws://supervisor/core/websocket", token="test-token")
        config = SimpleNamespace(front_door_video_source="ring_native", front_door_camera_entity="camera.front_live")
        with patch("viper_core.ring_camera.stream", side_effect=fake_stream) as stream, \
                patch.object(live, "_start_live_video_process") as rtsp, \
                patch.object(live, "_send_live_visual_task", new_callable=AsyncMock), \
                patch.object(live, "LIVE_VIDEO_WARMUP_SECONDS", 0), \
                patch.object(live, "LIVE_VIDEO_WARMUP_FRAMES", 0), \
                patch.object(live, "LIVE_INITIAL_STABLE_FRAMES", 0):
            stats = await live._send_live_video_stream(
                session, SimpleNamespace(Blob=lambda **kwargs: kwargs), "", "front", False,
                time.monotonic() + 3, bytearray(), effective_config=config, ha_client=ha,
            )
        self.assertEqual(stats["sent_frames"], 2)
        self.assertEqual(session.send_realtime_input.await_count, 2)
        stream.assert_called_once()
        rtsp.assert_not_called()

    def test_native_diagnostics_capture_video_without_rtsp(self):
        from PIL import Image

        frames = []
        for shade in (20, 60, 100, 140):
            output = BytesIO()
            Image.new("RGB", (128, 72), (shade, 0, 0)).save(output, format="JPEG")
            frames.append(output.getvalue())
        config = SimpleNamespace(front_door_video_source="ring_native", front_door_camera_entity="camera.front_live")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(live, "LIVE_DEBUG_DIR", Path(directory)), \
                patch.object(live.vision, "capture_native_sequence", return_value=frames) as capture, \
                patch.object(live, "_start_live_video_process") as rtsp:
            result = live.capture_diagnostic_frames(config, SimpleNamespace(), "front", seconds=4)
            self.assertTrue(result["ok"])
            self.assertEqual(result["captured_frames"], 2)
            self.assertTrue(Path(result["artifacts"]["video"]["path"]).read_bytes())
        capture.assert_called_once()
        rtsp.assert_not_called()

    def test_rtsp_diagnostics_reuse_recording_for_frames(self):
        frames = [b"\xff\xd8one\xff\xd9", b"\xff\xd8two\xff\xd9", b"\xff\xd8three\xff\xd9"]
        config = SimpleNamespace(front_door_video_source="rtsp", front_door_stream_url="rtsp://camera/live",
                                 front_door_live_stream_switch="")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(live, "LIVE_DEBUG_DIR", Path(directory)), \
                patch.object(live, "_capture_debug_video") as record, \
                patch.object(live, "_frames_from_debug_video", return_value=frames) as extract, \
                patch.object(live, "_start_live_video_process") as second_session, \
                patch.object(live, "_write_debug_contact_sheet", return_value=None):
            result = live.capture_diagnostic_frames(config, SimpleNamespace(), "front", seconds=4)
        self.assertTrue(result["ok"])
        self.assertEqual(result["captured_frames"], 1)
        record.assert_called_once()
        extract.assert_called_once()
        second_session.assert_not_called()

    def test_rtsp_recording_frame_extraction(self):
        from PIL import Image

        frames = []
        for shade in (20, 60, 100, 140):
            output = BytesIO()
            Image.new("RGB", (128, 72), (shade, 0, 0)).save(output, format="JPEG")
            frames.append(output.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.mp4"
            path.write_bytes(live.vision._encode_native_video(frames))
            extracted = live._frames_from_debug_video(path, 4)
        self.assertEqual(len(extracted), 4)
        self.assertTrue(all(frame.startswith(b"\xff\xd8") for frame in extracted))

    def test_rtsp_diagnostic_timeout_is_reported(self):
        config = SimpleNamespace(front_door_video_source="rtsp", front_door_stream_url="rtsp://camera/live",
                                 front_door_live_stream_switch="")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(live, "LIVE_DEBUG_DIR", Path(directory)), \
                patch.object(live, "_capture_debug_video", side_effect=live.subprocess.TimeoutExpired("ffmpeg", 30)):
            result = live.capture_diagnostic_frames(config, SimpleNamespace(), "front", seconds=4)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["message"])


if __name__ == "__main__":
    unittest.main()
