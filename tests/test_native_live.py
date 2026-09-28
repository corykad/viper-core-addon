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


if __name__ == "__main__":
    unittest.main()
