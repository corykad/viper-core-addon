import asyncio
import json
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ha_addons" / "viper_core"))
from viper_core.ring_camera import CameraError, capture, inventory, stream


@unittest.skipUnless(importlib.util.find_spec("aiortc"), "Run with the isolated Ring trial dependencies")
class RingCameraTests(unittest.IsolatedAsyncioTestCase):
    async def test_timed_stream_delivers_frames_and_closes_session(self):
        from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
        from websockets.asyncio.server import serve
        unsubscribed = asyncio.Event()

        async def endpoint(socket):
            peer = RTCPeerConnection()
            try:
                await socket.send(json.dumps({"type": "auth_required"}))
                await socket.recv()
                await socket.send(json.dumps({"type": "auth_ok"}))
                await socket.recv()
                await socket.send(json.dumps({"id": 1, "type": "result", "success": True, "result": {"configuration": {"iceServers": []}}}))
                offer = json.loads(await socket.recv())
                await peer.setRemoteDescription(RTCSessionDescription(sdp=offer["offer"], type="offer"))
                peer.addTrack(VideoStreamTrack())
                await peer.setLocalDescription(await peer.createAnswer())
                await socket.send(json.dumps({"id": 2, "type": "result", "success": True}))
                await socket.send(json.dumps({"id": 2, "type": "event", "event": {"type": "session", "session_id": "test-session"}}))
                await socket.send(json.dumps({"id": 2, "type": "event", "event": {"type": "answer", "answer": peer.localDescription.sdp}}))
                while True:
                    message = json.loads(await socket.recv())
                    if message["type"] == "unsubscribe_events":
                        unsubscribed.set()
                        break
                    await socket.send(json.dumps({"id": message["id"], "type": "result", "success": True}))
            finally:
                await peer.close()

        frames = []

        async def collect(frame):
            frames.append(frame)

        async with serve(endpoint, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            result = await stream(f"ws://127.0.0.1:{port}", "test-token", "camera.test_live", 2, collect, fps=2)
            await asyncio.wait_for(unsubscribed.wait(), 2)
        self.assertGreaterEqual(result["frame_count"], 2)
        self.assertEqual(result["frame_count"], len(frames))
        self.assertTrue(all(frame["jpeg"].startswith(b"\xff\xd8") for frame in frames))

    async def test_real_webrtc_frames_and_session_cleanup(self):
        from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
        from websockets.asyncio.server import serve
        unsubscribed = asyncio.Event()

        async def endpoint(socket):
            peer = RTCPeerConnection()
            try:
                await socket.send(json.dumps({"type": "auth_required"}))
                auth = json.loads(await socket.recv())
                self.assertEqual(auth["access_token"], "test-token")
                await socket.send(json.dumps({"type": "auth_ok"}))
                config = json.loads(await socket.recv())
                self.assertEqual(config["type"], "camera/webrtc/get_client_config")
                await socket.send(json.dumps({"id": 1, "type": "result", "success": True, "result": {"configuration": {"iceServers": []}}}))
                offer = json.loads(await socket.recv())
                self.assertEqual(offer["type"], "camera/webrtc/offer")
                await peer.setRemoteDescription(RTCSessionDescription(sdp=offer["offer"], type="offer"))
                peer.addTrack(VideoStreamTrack())
                await peer.setLocalDescription(await peer.createAnswer())
                await socket.send(json.dumps({"id": 2, "type": "result", "success": True}))
                await socket.send(json.dumps({"id": 2, "type": "event", "event": {"type": "session", "session_id": "test-session"}}))
                await socket.send(json.dumps({"id": 2, "type": "event", "event": {"type": "answer", "answer": peer.localDescription.sdp}}))
                while True:
                    message = json.loads(await socket.recv())
                    if message["type"] == "unsubscribe_events":
                        unsubscribed.set()
                        break
                    self.assertEqual(message["type"], "camera/webrtc/candidate")
                    self.assertEqual(message["session_id"], "test-session")
                    await socket.send(json.dumps({"id": message["id"], "type": "result", "success": True}))
            finally:
                await peer.close()

        async with serve(endpoint, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            for frame_limit in (1, 2):
                unsubscribed.clear()
                result = await capture(f"ws://127.0.0.1:{port}", "test-token", "camera.test_live",
                                       timeout=30, frame_limit=frame_limit)
                await asyncio.wait_for(unsubscribed.wait(), 2)
                self.assertEqual(result["source"], "home_assistant_ring_webrtc")
                self.assertEqual(len(result["frames"]), frame_limit)
                self.assertTrue(all(frame["jpeg"].startswith(b"\xff\xd8") for frame in result["frames"]))
                if frame_limit == 2:
                    self.assertGreater(result["frames"][1]["pts"], result["frames"][0]["pts"])

    async def test_inventory_excludes_mqtt_cameras(self):
        from websockets.asyncio.server import serve

        async def endpoint(socket):
            await socket.send(json.dumps({"type": "auth_required"}))
            await socket.recv()
            await socket.send(json.dumps({"type": "auth_ok"}))
            await socket.recv()
            await socket.send(json.dumps({"id": 1, "success": True, "result": [
                {"entity_id": "camera.native", "platform": "ring"},
                {"entity_id": "camera.mqtt", "platform": "mqtt"}]}))
            await socket.recv()
            await socket.send(json.dumps({"id": 2, "success": True, "result": [
                {"entity_id": "camera.native", "state": "idle", "attributes": {}},
                {"entity_id": "camera.mqtt", "state": "idle", "attributes": {}}]}))

        async with serve(endpoint, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            result = await inventory(f"ws://127.0.0.1:{port}", "test-token")
            self.assertEqual([item["entity_id"] for item in result], ["camera.native"])

    async def test_second_frame_stall_returns_first_frame_and_closes_session(self):
        from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
        from websockets.asyncio.server import serve
        unsubscribed = asyncio.Event()

        class OneFrameTrack(VideoStreamTrack):
            def __init__(self):
                super().__init__()
                self.sent = 0

            async def recv(self):
                if self.sent >= 2:
                    await asyncio.sleep(30)
                self.sent += 1
                return await super().recv()

        async def endpoint(socket):
            peer = RTCPeerConnection()
            try:
                await socket.send(json.dumps({"type": "auth_required"}))
                await socket.recv()
                await socket.send(json.dumps({"type": "auth_ok"}))
                await socket.recv()
                await socket.send(json.dumps({"id": 1, "type": "result", "success": True, "result": {"configuration": {"iceServers": []}}}))
                offer = json.loads(await socket.recv())
                await peer.setRemoteDescription(RTCSessionDescription(sdp=offer["offer"], type="offer"))
                peer.addTrack(OneFrameTrack())
                await peer.setLocalDescription(await peer.createAnswer())
                await socket.send(json.dumps({"id": 2, "type": "result", "success": True}))
                await socket.send(json.dumps({"id": 2, "type": "event", "event": {"type": "session", "session_id": "test-session"}}))
                await socket.send(json.dumps({"id": 2, "type": "event", "event": {"type": "answer", "answer": peer.localDescription.sdp}}))
                while True:
                    message = json.loads(await socket.recv())
                    if message["type"] == "unsubscribe_events":
                        unsubscribed.set()
                        break
                    await socket.send(json.dumps({"id": message["id"], "type": "result", "success": True}))
            finally:
                await peer.close()

        async with serve(endpoint, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            result = await capture(f"ws://127.0.0.1:{port}", "test-token", "camera.test_live", timeout=15)
            await asyncio.wait_for(unsubscribed.wait(), 2)
        self.assertEqual(result["frame_count"], 1)
        self.assertLess(result["elapsed_seconds"], 15)
        self.assertTrue(result["frames"][0]["jpeg"].startswith(b"\xff\xd8"))

    async def test_no_frames_times_out_instead_of_success(self):
        from websockets.asyncio.server import serve

        async def endpoint(socket):
            try:
                await socket.send(json.dumps({"type": "auth_required"}))
                await socket.recv()
                await socket.send(json.dumps({"type": "auth_ok"}))
                await socket.recv()
                await socket.send(json.dumps({"id": 1, "success": True, "result": {"configuration": {"iceServers": []}}}))
                await socket.recv()
                await socket.recv()
            except Exception:
                pass

        async with serve(endpoint, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with self.assertRaisesRegex(CameraError, "Timed out"):
                await capture(f"ws://127.0.0.1:{port}", "test-token", "camera.test_live", timeout=2)


if __name__ == "__main__":
    unittest.main()
