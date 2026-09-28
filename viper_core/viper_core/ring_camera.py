"""Receive current Ring video through HA's camera WebRTC signaling API."""
import asyncio
import io
import json
import time
from contextlib import asynccontextmanager, suppress

import websockets


class CameraError(RuntimeError):
    pass


@asynccontextmanager
async def connection(url, token):
    async with websockets.connect(url, open_timeout=10, close_timeout=2, max_size=4 * 1024 * 1024) as socket:
        greeting = json.loads(await asyncio.wait_for(socket.recv(), 10))
        if greeting.get("type") != "auth_required":
            raise CameraError("Unexpected Home Assistant authentication response.")
        await socket.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(await asyncio.wait_for(socket.recv(), 10))
        if auth.get("type") != "auth_ok":
            raise CameraError("Home Assistant authentication failed.")
        yield socket


async def request(socket, request_id, command):
    await socket.send(json.dumps({"id": request_id, **command}))
    response = json.loads(await asyncio.wait_for(socket.recv(), 10))
    if response.get("id") != request_id or not response.get("success"):
        code = (response.get("error") or {}).get("code", "request_failed")
        raise CameraError(f"Home Assistant request failed ({code}).")
    return response.get("result")


async def inventory(url, token):
    async with connection(url, token) as socket:
        registry = await request(socket, 1, {"type": "config/entity_registry/list"})
        states = await request(socket, 2, {"type": "get_states"})
    ring_ids = {item["entity_id"] for item in registry if item.get("platform") == "ring" and not item.get("disabled_by")}
    return [{"entity_id": item["entity_id"], "state": item.get("state"),
             "name": (item.get("attributes") or {}).get("friendly_name", item["entity_id"])}
            for item in states if item["entity_id"] in ring_ids and item["entity_id"].startswith(("camera.", "event."))]


async def capture(url, token, entity_id, timeout=40):
    if not entity_id.startswith("camera."):
        raise CameraError("Choose a camera entity.")
    progress = {"stage": "authentication", "connection": "new"}
    try:
        async with asyncio.timeout(timeout):
            return await _capture(url, token, entity_id, progress)
    except TimeoutError:
        raise CameraError(f"Timed out waiting for live video (stage: {progress['stage']}, connection: {progress['connection']}, ICE: {progress.get('ice', 'unknown')}, decoded: {progress.get('decoded', 0)}, packets: {progress.get('packets', {})}).") from None


async def _capture(url, token, entity_id, progress):
    from aiortc import RTCBundlePolicy, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
    from aiortc.sdp import SessionDescription, candidate_from_sdp, candidate_to_sdp

    started = time.monotonic()
    tasks = []
    peer = None
    frames = []
    done = asyncio.get_running_loop().create_future()
    async with connection(url, token) as socket:
        config = await request(socket, 1, {"type": "camera/webrtc/get_client_config", "entity_id": entity_id})
        progress["stage"] = "local network candidates"
        servers = [RTCIceServer(urls=item["urls"], username=item.get("username"), credential=item.get("credential"))
                   for item in (config.get("configuration") or {}).get("iceServers", [])]
        peer = RTCPeerConnection(RTCConfiguration(iceServers=servers, bundlePolicy=RTCBundlePolicy.MAX_BUNDLE))
        try:
            peer.addTransceiver("audio", direction="recvonly")
            peer.addTransceiver("video", direction="recvonly")
            if config.get("dataChannel"):
                peer.createDataChannel(config["dataChannel"])

            async def receive_video(track):
                last_pts = None
                try:
                    while not done.done():
                        frame = await track.recv()
                        progress["decoded"] = progress.get("decoded", 0) + 1
                        if frames and frame.pts == last_pts:
                            continue
                        image = frame.to_image()
                        image.thumbnail((1280, 720))
                        output = io.BytesIO()
                        image.save(output, format="JPEG", quality=85)
                        frames.append({"jpeg": output.getvalue(), "width": frame.width, "height": frame.height,
                                       "received_at": time.time(), "pts": frame.pts})
                        last_pts = frame.pts
                        if len(frames) == 2:
                            done.set_result(None)
                except Exception:
                    if not done.done():
                        done.set_exception(CameraError("The live video track ended before two frames arrived."))

            @peer.on("track")
            def on_track(track):
                if track.kind == "video":
                    tasks.append(asyncio.create_task(receive_video(track)))

            async def request_keyframes():
                while not done.done():
                    await asyncio.sleep(2)
                    for receiver in peer.getReceivers():
                        if receiver.track and receiver.track.kind == "video":
                            for source in receiver.getSynchronizationSources():
                                # aiortc 1.14 is pinned: request a fresh keyframe after joining mid-stream.
                                await receiver._send_rtcp_pli(source.source)

            tasks.append(asyncio.create_task(request_keyframes()))

            @peer.on("connectionstatechange")
            async def on_state():
                if peer.connectionState != "closed":
                    progress["connection"] = peer.connectionState
                if peer.connectionState == "failed" and not done.done():
                    done.set_exception(CameraError("The live WebRTC connection failed."))

            await peer.setLocalDescription(await peer.createOffer())
            local_sdp = SessionDescription.parse(peer.localDescription.sdp)
            progress["stage"] = "Ring offer"
            await socket.send(json.dumps({"id": 2, "type": "camera/webrtc/offer", "entity_id": entity_id,
                                          "offer": peer.localDescription.sdp}))

            async def signaling():
                pending_candidates = []
                try:
                    while not done.done():
                        message = json.loads(await socket.recv())
                        if message.get("type") == "result" and message.get("id", 0) >= 10 and not message.get("success"):
                            raise CameraError("Home Assistant rejected a live-video network candidate.")
                        if message.get("id") != 2:
                            continue
                        if message.get("type") == "result" and not message.get("success"):
                            raise CameraError("Home Assistant rejected the live-video request.")
                        event = message.get("event") or {}
                        if event.get("type") == "session":
                            request_id = 10
                            for index, media in enumerate(local_sdp.media):
                                for candidate in media.ice_candidates:
                                    await socket.send(json.dumps({"id": request_id, "type": "camera/webrtc/candidate",
                                        "entity_id": entity_id, "session_id": event["session_id"], "candidate": {
                                            "candidate": "candidate:" + candidate_to_sdp(candidate),
                                            "sdpMid": media.rtp.muxId, "sdpMLineIndex": index}}))
                                    request_id += 1
                            progress["stage"] = "Ring answer"
                        if event.get("type") == "error":
                            raise CameraError("Ring could not start the live-video session.")
                        if event.get("type") == "answer":
                            await peer.setRemoteDescription(RTCSessionDescription(sdp=event["answer"], type="answer"))
                            progress["stage"] = "video frames"
                        if event.get("type") == "candidate":
                            candidate = event.get("candidate") or {}
                            value = candidate.get("candidate") or ""
                            if value:
                                parsed = candidate_from_sdp(value.removeprefix("candidate:"))
                                parsed.sdpMid = candidate.get("sdpMid")
                                parsed.sdpMLineIndex = candidate.get("sdpMLineIndex")
                                pending_candidates.append(parsed)
                        if peer.remoteDescription:
                            for candidate in pending_candidates:
                                await peer.addIceCandidate(candidate)
                            pending_candidates.clear()
                except Exception as exc:
                    if not done.done():
                        done.set_exception(exc if isinstance(exc, CameraError) else CameraError("Live-video signaling disconnected."))

            tasks.append(asyncio.create_task(signaling()))
            await done
            return {"frames": frames, "elapsed_seconds": round(time.monotonic() - started, 2),
                    "connection": peer.connectionState, "source": "home_assistant_ring_webrtc"}
        finally:
            progress["ice"] = peer.iceConnectionState
            with suppress(Exception):
                stats = await peer.getStats()
                progress["packets"] = {item.kind: item.packetsReceived for item in stats.values() if item.type == "inbound-rtp"}
            # Unsubscribing closes Ring's session; closing the socket is the fallback.
            with suppress(Exception):
                await socket.send(json.dumps({"id": 3, "type": "unsubscribe_events", "subscription": 2}))
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await peer.close()
