import sys
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ha_addons" / "viper_core"))
from viper_core import vision
from viper_core.ha import HomeAssistantClient
from viper_core.ring_camera import CameraError


class NativeVisionTests(unittest.TestCase):
    def config(self, **extra):
        return SimpleNamespace(front_door_video_source="ring_native",
                               front_door_camera_entity="camera.front_live",
                               ai_provider="gemini", gemini_api_key="test-key", **extra)

    def test_native_frames_reach_description_without_rtsp(self):
        result = {"frames": [{"jpeg": b"fresh-1"}, {"jpeg": b"fresh-2"}], "elapsed_seconds": 2.5}
        with patch("viper_core.ring_camera.capture", new_callable=AsyncMock, return_value=result) as capture, \
                patch.object(vision, "describe_images_with_gemini", return_value="A delivery is at the door.") as describe, \
                patch.object(vision, "_capture_stream_frames") as rtsp:
            text = vision.describe_doorbell(self.config(), HomeAssistantClient("http://supervisor/core/api", "test-token"), "front")
        self.assertEqual(text, "A delivery is at the door.")
        capture.assert_awaited_once_with("ws://supervisor/core/websocket", "test-token", "camera.front_live")
        self.assertEqual(describe.call_args.args[0], [(b"fresh-1", "image/jpeg"), (b"fresh-2", "image/jpeg")])
        rtsp.assert_not_called()

    def test_native_failure_never_falls_back_to_rtsp(self):
        with patch("viper_core.ring_camera.capture", new_callable=AsyncMock, side_effect=CameraError("No live frames")), \
                patch.object(vision, "_capture_stream_frames") as rtsp, \
                patch.object(vision, "describe_images_with_gemini") as describe:
            text = vision.describe_doorbell(self.config(front_door_stream_url="rtsp://old-source"),
                                           HomeAssistantClient("http://supervisor/core/api", "test-token"), "front")
        self.assertEqual(text, "")
        rtsp.assert_not_called()
        describe.assert_not_called()

    def test_ordinary_ha_keeps_api_prefix(self):
        result = {"frames": [], "elapsed_seconds": 0}
        with patch("viper_core.ring_camera.capture", new_callable=AsyncMock, return_value=result) as capture:
            vision.capture_doorbell_frames(self.config(), HomeAssistantClient("https://ha.example/api", "test"), "front")
        capture.assert_awaited_once_with("wss://ha.example/api/websocket", "test", "camera.front_live")

    def test_supervisor_websocket_does_not_use_rest_api_path(self):
        self.assertEqual(HomeAssistantClient("http://supervisor/core/api", "test").websocket_url(),
                         "ws://supervisor/core/websocket")

    def test_unconfigured_native_camera_does_not_connect(self):
        config = self.config()
        config.front_door_camera_entity = ""
        with patch("viper_core.ring_camera.capture", new_callable=AsyncMock) as capture:
            with self.assertRaises(CameraError):
                vision.capture_doorbell_frames(config, HomeAssistantClient("http://ha/api", "test"), "front")
        capture.assert_not_called()

    def test_legacy_source_still_cleans_up_switch(self):
        config = SimpleNamespace(front_door_stream_url="rtsp://legacy")
        ha = HomeAssistantClient("http://ha/api", "test")
        with patch.object(vision.shutil, "which", return_value="ffmpeg"), \
                patch.object(vision, "_prepare_live_stream", return_value="switch.live"), \
                patch.object(vision, "_capture_stream_frames", side_effect=RuntimeError("capture failed")), \
                patch.object(vision, "_cleanup_live_stream") as cleanup:
            with self.assertRaises(RuntimeError):
                vision.capture_doorbell_frames(config, ha, "front")
        cleanup.assert_called_once_with(ha, "switch.live")

    def test_native_manual_video_uses_timed_webrtc_frames(self):
        async def fake_stream(url, token, entity, seconds, callback, fps=1):
            self.assertEqual((url, token, entity, seconds, fps),
                             ("ws://supervisor/core/websocket", "test-token", "camera.front_live", 4, 1))
            await callback({"jpeg": b"frame-one"})
            await callback({"jpeg": b"frame-two"})
            return {"frame_count": 2}

        with patch("viper_core.ring_camera.stream", side_effect=fake_stream) as stream, \
                patch.object(vision, "_encode_native_video", return_value=b"mp4") as encode, \
                patch.object(vision, "describe_video_with_gemini", return_value="A person approaches.") as describe, \
                patch.object(vision, "_capture_stream_frames") as rtsp:
            text = vision.describe_live_doorbell(
                self.config(doorbell_live_video_seconds=4),
                HomeAssistantClient("http://supervisor/core/api", "test-token"), "front", mode="manual",
            )
        self.assertEqual(text, "A person approaches.")
        stream.assert_called_once()
        encode.assert_called_once_with([b"frame-one", b"frame-two"])
        self.assertEqual(describe.call_args.args[0:2], (b"mp4", "video/mp4"))
        rtsp.assert_not_called()

    def test_native_video_encoder_produces_mp4(self):
        from PIL import Image
        frames = []
        for shade in (20, 100, 180):
            output = BytesIO()
            Image.new("RGB", (128, 72), (shade, 0, 0)).save(output, format="JPEG")
            frames.append(output.getvalue())
        self.assertIn(b"ftyp", vision._encode_native_video(frames)[:32])
