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

        session = self.coordinator.stream_session
        if not session or not session.is_active or session.video_relay is None:
            # No active stream — auto-start camera preview and wait for relay
            _LOGGER.info(
                "WebRTC offer received but no active stream — auto-starting camera preview"
            )
            await self.coordinator.start_camera_preview()

            # Wait up to 25 s for the mediasoup session + relay to be ready
            for _ in range(50):
                await asyncio.sleep(0.5)
                session = self.coordinator.stream_session
                if session and session.is_active and session.video_relay is not None:
                    _LOGGER.info("Stream ready — proceeding with WebRTC negotiation")
                    break
            else:
                _LOGGER.warning(
                    "Camera preview did not start in time for WebRTC session %s",
                    session_id,
                )
                send_message(
                    WebRTCError(
                        code="preview_timeout",
                        message="Camera preview timed out. Try again.",
                    )
                )
                return

        pc = RTCPeerConnection()

        # Add intercom → browser tracks via relay
        video_track = session.video_relay.create_consumer_track()
        pc.addTrack(video_track)

        if session.audio_relay is not None:
            audio_track = session.audio_relay.create_consumer_track()
            pc.addTrack(audio_track)

        # Receive browser mic → forward to intercom send transport
        @pc.on("track")
        def on_track(track: Any) -> None:
            if track.kind == "audio" and session and session.is_active:
                _LOGGER.info("Browser mic track received — forwarding to intercom")
                if hasattr(session, "_switchable_track") and session._switchable_track:
                    session._switchable_track.set_source(_MicTrackSource(track))

        # SDP negotiation
        try:
            offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()

            # Wait for ICE gathering to complete before sending the answer
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
            with contextlib.suppress(Exception):
                await pc.close()
            send_message(
                WebRTCError(
                    code="negotiation_failed",
                    message=str(err),
                )
            )
            return

        # Register PC with coordinator for lifecycle cleanup
        self.coordinator.register_webrtc_peer(session_id, pc)

        # Send SDP answer to browser
        send_message(WebRTCAnswer(answer=pc.localDescription.sdp))
        _LOGGER.info("WebRTC answer sent for session %s", session_id)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Called by HA when the browser closes a WebRTC session."""
        _LOGGER.debug("Closing WebRTC session %s", session_id)
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
