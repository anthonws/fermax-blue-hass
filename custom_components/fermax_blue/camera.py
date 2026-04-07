"""Camera platform for Fermax Blue."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from aiohttp import web
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_DOORBELL_RING
from .coordinator import FermaxBlueCoordinator
from .entity import FermaxBlueEntity

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.components.camera.webrtc import (
        WebRTCAnswer,
        WebRTCError,
        WebRTCSendMessage,
    )
    _WEBRTC_AVAILABLE = True
except ImportError:
    _WEBRTC_AVAILABLE = False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Fermax Blue cameras."""
    coordinators: list[FermaxBlueCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[Camera] = []

    for coordinator in coordinators:
        entities.append(FermaxCamera(coordinator))

    async_add_entities(entities)


class _MicTrackSource:
    """Adapts a browser mic MediaStreamTrack to the switchable track set_source() interface."""

    def __init__(self, track: Any) -> None:
        self._track = track

    async def recv(self) -> Any:
        """Return the next audio frame from the browser mic."""
        try:
            return await self._track.recv()
        except Exception:
            raise StopIteration from None


class FermaxCamera(FermaxBlueEntity, Camera):
    """Camera entity with live video streaming and visitor photo capture.

    Supports two modes:
    - Still image: shows the last captured visitor photo (from doorbell ring)
    - Live stream: connects to the intercom camera via mediasoup and serves
      MJPEG frames in real-time (triggered by turn_on / camera preview button)
    """

    _attr_translation_key = "visitor"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: FermaxBlueCoordinator) -> None:
        FermaxBlueEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._attr_unique_id = f"{self._device_id}_camera"
        # PCs created during setup but not yet answered (keyed by session_id).
        # Allows async_on_webrtc_candidate to buffer browser ICE candidates
        # that arrive before we finish waiting for the mediasoup relay.
        self._pending_pcs: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        """Register for doorbell ring events."""
        await super().async_added_to_hass()

        for door_name in self.coordinator.pairing.access_doors:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    SIGNAL_DOORBELL_RING.format(self._device_id, door_name),
                    self._on_doorbell_ring,
                )
            )

    @callback
    def _on_doorbell_ring(self) -> None:
        """Handle doorbell ring - trigger image refresh."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Camera is available if we have any image to serve."""
        if self.coordinator.last_photo:
            return True
        stream = self.coordinator.stream_session
        if stream and stream.latest_frame:
            return True
        return super().available

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest frame: live stream if active, else last captured frame."""
        stream = self.coordinator.stream_session
        if stream and stream.latest_frame:
            return stream.latest_frame
        return self.coordinator.last_photo

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse | None:
        """Serve MJPEG stream: live frames when streaming, last photo otherwise.

        The stream serves continuously — when a live stream starts or stops,
        the MJPEG output switches seamlessly between live frames and the
        static preview without dropping the connection.
        """
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "multipart/x-mixed-replace;boundary=frameboundary",
            },
        )
        await response.prepare(request)

        try:
            while True:
                stream = self.coordinator.stream_session
                frame = None

                # Prefer live stream frame
                if stream and stream.latest_frame:
                    frame = stream.latest_frame
                elif self.coordinator.last_photo:
                    frame = self.coordinator.last_photo

                if frame:
                    await response.write(
                        b"--frameboundary\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: "
                        + str(len(frame)).encode()
                        + b"\r\n\r\n"
                        + frame
                        + b"\r\n"
                    )

                # Fast poll during stream, slow poll for static preview
                if stream and stream.is_active:
                    await asyncio.sleep(0.04)  # ~25fps
                else:
                    await asyncio.sleep(2)  # Refresh preview every 2s
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            pass

        return response

    async def async_turn_on(self) -> None:
        """Start live camera stream via auto-on + mediasoup."""
        result = await self.coordinator.start_camera_preview()
        if result:
            _LOGGER.info("Camera auto-on started: %s", result.description)
        else:
            _LOGGER.error("Failed to start camera auto-on")

    async def async_turn_off(self) -> None:
        """Stop live camera stream."""
        await self.coordinator.stop_stream()

    async def async_handle_async_webrtc_offer(
        self,
        offer_sdp: str,
        session_id: str,
        send_message: "WebRTCSendMessage",
    ) -> None:
        """Handle a WebRTC offer from the browser for live video+audio streaming.

        Creates an RTCPeerConnection, adds relay tracks from the active intercom
        stream, and returns the SDP answer. Also wires the browser mic to the
        intercom send transport so the user can speak to the visitor.
        """
        if not _WEBRTC_AVAILABLE:
            _LOGGER.warning("WebRTC types not available in this HA version")
            return

        from aiortc import RTCPeerConnection, RTCSessionDescription

        # --- Step 1: Create PC and set remote description immediately ---
        # Doing this before waiting for the stream allows HA to route incoming
        # browser ICE candidates to async_on_webrtc_candidate while we wait.
        pc = RTCPeerConnection()
        self._pending_pcs[session_id] = pc

        @pc.on("track")
        def on_track(track: Any) -> None:
            if track.kind == "audio":
                s = self.coordinator.stream_session
                if s and s.is_active and hasattr(s, "_switchable_track") and s._switchable_track:
                    _LOGGER.info("Browser mic track received — forwarding to intercom")
                    s._switchable_track.set_source(_MicTrackSource(track))

        try:
            offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
            await pc.setRemoteDescription(offer)
        except Exception as err:
            _LOGGER.exception("Failed to set remote description for session %s", session_id)
            self._pending_pcs.pop(session_id, None)
            with contextlib.suppress(Exception):
                await pc.close()
            send_message(WebRTCError(code="negotiation_failed", message=str(err)))
            return

        # --- Step 2: Ensure an active stream with relay is available ---
        session = self.coordinator.stream_session
        relay_ready = session and session.is_active and session.video_relay is not None

        if not relay_ready:
            if self.coordinator.stream_session is None:
                _LOGGER.info(
                    "WebRTC offer received — no stream in progress, auto-starting preview"
                )
                await self.coordinator.start_camera_preview()
            else:
                _LOGGER.info(
                    "WebRTC offer received — stream already in progress, waiting for relay"
                )

            for _ in range(50):
                await asyncio.sleep(0.5)
                session = self.coordinator.stream_session
                if session and session.is_active and session.video_relay is not None:
                    _LOGGER.info(
                        "Stream relay ready — proceeding with WebRTC session %s", session_id
                    )
                    break
            else:
                _LOGGER.warning(
                    "Stream relay not ready within 25 s for WebRTC session %s", session_id
                )
                self._pending_pcs.pop(session_id, None)
                with contextlib.suppress(Exception):
                    await pc.close()
                send_message(
                    WebRTCError(
                        code="preview_timeout",
                        message="Camera preview timed out. Try again.",
                    )
                )
                return

        # --- Step 3: Add relay tracks and complete negotiation ---
        video_track = session.video_relay.create_consumer_track()
        pc.addTrack(video_track)

        if session.audio_relay is not None:
            audio_track = session.audio_relay.create_consumer_track()
            pc.addTrack(audio_track)

        try:
            answer = await pc.createAnswer()

            gather_complete: asyncio.Event = asyncio.Event()

            @pc.on("icegatheringstatechange")
            def on_ice_state() -> None:
                if pc.iceGatheringState == "complete":
                    gather_complete.set()

            await pc.setLocalDescription(answer)
            try:
                await asyncio.wait_for(gather_complete.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "ICE gathering timed out for WebRTC session %s, sending partial SDP",
                    session_id,
                )
        except Exception as err:
            _LOGGER.exception("WebRTC negotiation failed for session %s", session_id)
            self._pending_pcs.pop(session_id, None)
            with contextlib.suppress(Exception):
                await pc.close()
            send_message(WebRTCError(code="negotiation_failed", message=str(err)))
            return

        # Move from pending → registered, then send answer
        self._pending_pcs.pop(session_id, None)
        self.coordinator.register_webrtc_peer(session_id, pc)
        send_message(WebRTCAnswer(answer=pc.localDescription.sdp))
        _LOGGER.info("WebRTC answer sent for session %s", session_id)

    async def async_on_webrtc_candidate(self, session_id: str, candidate: Any) -> None:
        """Handle a browser ICE candidate (trickle ICE).

        Called by HA's WebSocket layer for each candidate the browser sends
        after the offer.  We look up the RTCPeerConnection — which may still
        be in the pending dict while we wait for the mediasoup relay — and
        add the candidate so ICE negotiation can proceed in parallel.
        """
        pc = self._pending_pcs.get(session_id) or self.coordinator._webrtc_peers.get(session_id)
        if pc is None:
            return
        try:
            if candidate is None:
                return  # end-of-candidates signal — nothing to do
            from aiortc.sdp import candidate_from_sdp

            # HA may pass a dataclass, dict, or bare string
            if hasattr(candidate, "candidate"):
                candidate_str = candidate.candidate
                sdp_mid = getattr(candidate, "sdpMid", None)
                sdp_mline_index = getattr(candidate, "sdpMLineIndex", None)
            elif isinstance(candidate, dict):
                candidate_str = candidate.get("candidate", "")
                sdp_mid = candidate.get("sdpMid")
                sdp_mline_index = candidate.get("sdpMLineIndex")
            else:
                candidate_str = str(candidate)
                sdp_mid = None
                sdp_mline_index = None

            if not candidate_str or not candidate_str.strip():
                return

            # Strip the "candidate:" prefix that browsers include
            sdp_part = candidate_str
            if sdp_part.startswith("candidate:"):
                sdp_part = sdp_part[len("candidate:"):]

            ice = candidate_from_sdp(sdp_part)
            ice.sdpMid = sdp_mid
            ice.sdpMLineIndex = sdp_mline_index
            await pc.addIceCandidate(ice)
        except Exception:
            _LOGGER.debug(
                "Failed to add ICE candidate for session %s", session_id, exc_info=True
            )

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Called by HA when the browser closes a WebRTC session."""
        _LOGGER.debug("Closing WebRTC session %s", session_id)
        # Clean up a pending PC that never completed negotiation
        pc = self._pending_pcs.pop(session_id, None)
        if pc:
            asyncio.ensure_future(pc.close())
        self.coordinator.close_webrtc_peer(session_id)

    @property
    def is_streaming(self) -> bool:
        """Return True if live video stream is active."""
        stream = self.coordinator.stream_session
        return bool(stream and stream.is_active)

    @property
    def is_on(self) -> bool:
        """Return True if the camera can serve an image."""
        if self.is_streaming:
            return True
        return self.coordinator.last_photo is not None
