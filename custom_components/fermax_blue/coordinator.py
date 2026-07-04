"""Data coordinator for Fermax Blue integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CallLogEntry,
    DeviceInfo,
    DivertResponse,
    FermaxApiError,
    FermaxAuthError,
    FermaxBlueApi,
    OpeningRecord,
    Pairing,
)
from .const import (
    CALL_MODE_AUTO_RESPOND,
    CALL_MODE_NOTIFY,
    DEFAULT_STREAM_DURATION,
    DOMAIN,
    RECORDINGS_DIR,
    SIGNAL_CALL_ENDED,
    SIGNAL_CAMERA_ON,
    SIGNAL_DOOR_OPENED,
    SIGNAL_DOORBELL_RING,
)
from .notification import FermaxNotificationListener, _redact_notification
from .streaming import DEFAULT_SIGNALING_URL, FermaxStreamSession

_LOGGER = logging.getLogger(__name__)

_PYMEDIASOUP_AVAILABLE: bool | None = None


def _pymediasoup_available() -> bool:
    """Return True if pymediasoup (and aiortc) can be imported."""
    global _PYMEDIASOUP_AVAILABLE
    if _PYMEDIASOUP_AVAILABLE is None:
        try:
            import pymediasoup  # noqa: F401

            _PYMEDIASOUP_AVAILABLE = True
        except ImportError:
            _PYMEDIASOUP_AVAILABLE = False
            _LOGGER.warning(
                "pymediasoup is not installed (av version conflict with HA 2026.7+). "
                "Live video streaming is disabled; door opening and notifications still work."
            )
    return _PYMEDIASOUP_AVAILABLE


# FCM re-delivers recent notifications when the listener reconnects after a
# reload/restart, causing phantom doorbell rings. Ignore them briefly.
NOTIFICATION_GRACE_PERIOD = 10
ALLOWED_SIGNALING_DOMAIN = ".fermax.io"

DOORBELL_RESET_SECONDS = 30
CAMERA_TIMEOUT_SECONDS = 90


def _is_trusted_signaling_url(url: str) -> bool:
    """Reject signaling URLs that aren't a TLS scheme on a Fermax domain.

    The SocketUrl comes from an FCM push, so treat it as untrusted input:
    require no surrounding whitespace, an https/wss scheme, and a *.fermax.io
    host (the only party that controls that domain).
    """
    try:
        if url != url.strip():
            return False
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("https", "wss"):
            return False
        host = (parsed.hostname or "").lower()
        return host.endswith(ALLOWED_SIGNALING_DOMAIN) or host == "fermax.io"
    except ValueError:
        return False


class FermaxBlueCoordinator(DataUpdateCoordinator):
    """Coordinate data updates and notifications for a Fermax Blue device."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: FermaxBlueApi,
        pairing: Pairing,
        scan_interval: int = 5,
        auto_response_file: str = "",
        firebase_config: dict[str, str | int] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{pairing.device_id}",
            update_interval=timedelta(minutes=scan_interval),
        )
        self.api = api
        self.pairing = pairing
        self.device_info: DeviceInfo | None = None
        self.notification_listener: FermaxNotificationListener | None = None
        self._last_photo: bytes | None = None
        self._last_photo_id: str | None = None
        self._doorbell_ringing: bool = False
        self._camera_active: bool = False

        self._photo_fetch_pending: bool = False
        self._doorbell_reset_unsub: CALLBACK_TYPE | None = None
        self._camera_timeout_unsub: CALLBACK_TYPE | None = None
        self._dnd_enabled: bool | None = None
        self._last_opening: OpeningRecord | None = None
        self._last_call: CallLogEntry | None = None
        self._call_log: list[CallLogEntry] = []
        self._stream_session: FermaxStreamSession | None = None
        self._storage_path: Path | None = None
        self._auto_response_file = auto_response_file
        self._firebase_config = firebase_config or {}
        self._call_mode = CALL_MODE_NOTIFY
        self._stream_duration = DEFAULT_STREAM_DURATION
        self._stream_stop_unsub: CALLBACK_TYPE | None = None
        self._processed_notifications: deque[str] = deque(maxlen=100)
        self._notification_start_time: float | None = None
        # Time-based grace only used on the very first run (no persisted IDs yet);
        # afterwards re-deliveries are recognised by persisted persistent_id.
        self._grace_active = False
        self._stream_lock = asyncio.Lock()  # serialises _start_stream/stop_stream
        self._webrtc_peers: dict[str, Any] = {}  # session_id -> RTCPeerConnection

    @property
    def call_mode(self) -> str:
        """Return the current call mode."""
        return self._call_mode

    @call_mode.setter
    def call_mode(self, value: str) -> None:
        """Set the call mode."""
        self._call_mode = value

    @property
    def stream_duration(self) -> int:
        """Return the configured stream duration in seconds."""
        return self._stream_duration

    @stream_duration.setter
    def stream_duration(self, value: int) -> None:
        """Set the stream duration in seconds."""
        self._stream_duration = value

    @property
    def last_photo(self) -> bytes | None:
        """Return the last captured photo."""
        return self._last_photo

    def _last_frame_path(self) -> Path | None:
        """Return the path for persisting the last camera frame."""
        if self._storage_path:
            return self._storage_path / f"last_frame_{self.pairing.device_id}.jpg"
        return None

    async def _save_last_photo(self) -> None:
        """Persist last photo to disk for survival across restarts."""
        path = self._last_frame_path()
        if path and self._last_photo:
            await asyncio.to_thread(path.write_bytes, self._last_photo)

    async def _save_call_photo(self, photo: bytes) -> None:
        """Save a doorbell call photo to the recordings directory."""
        from datetime import datetime

        media_root = self.hass.config.media_dirs.get("local", "/media")
        recordings_dir = Path(media_root) / RECORDINGS_DIR
        await asyncio.to_thread(recordings_dir.mkdir, parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = recordings_dir / f"{timestamp}_photo.jpg"
        await asyncio.to_thread(path.write_bytes, photo)
        _LOGGER.info("Call photo saved: %s (%d KB)", path, len(photo) // 1024)

    async def ensure_notifications_running(self) -> None:
        """Watchdog hook: revive the FCM listener if it died."""
        if self.notification_listener:
            await self.notification_listener.ensure_running()

    def _processed_ids_path(self) -> Path | None:
        """Path for persisting processed notification IDs across restarts."""
        if self._storage_path:
            return self._storage_path / f"processed_ids_{self.pairing.device_id}.json"
        return None

    async def _load_processed_ids(self) -> int:
        """Load persisted processed notification IDs. Returns how many loaded."""
        path = self._processed_ids_path()
        if not path:
            return 0

        def _read() -> list[str]:
            if not path.exists():
                return []
            try:
                import json

                data = json.loads(path.read_text())
                return [str(x) for x in data] if isinstance(data, list) else []
            except (OSError, ValueError):
                return []

        ids = await asyncio.to_thread(_read)
        self._processed_notifications.extend(ids)
        return len(ids)

    async def _save_processed_ids(self) -> None:
        """Persist processed notification IDs (best-effort)."""
        path = self._processed_ids_path()
        if not path:
            return
        ids = list(self._processed_notifications)

        def _write() -> None:
            import json

            with contextlib.suppress(OSError):
                path.write_text(json.dumps(ids))

        await asyncio.to_thread(_write)

    async def _load_last_photo(self) -> None:
        """Load persisted last photo from disk."""
        path = self._last_frame_path()
        if path:

            def _read() -> bytes | None:
                if path.exists():
                    return path.read_bytes()
                return None

            photo = await asyncio.to_thread(_read)
            if photo:
                self._last_photo = photo
                _LOGGER.info("Loaded persisted camera frame (%d bytes)", len(photo))

    @property
    def doorbell_ringing(self) -> bool:
        """Return True if the doorbell is currently ringing."""
        return self._doorbell_ringing

    @property
    def camera_active(self) -> bool:
        """Return True if camera preview is active."""
        return self._camera_active

    @property
    def dnd_enabled(self) -> bool | None:
        """Return DND state."""
        return self._dnd_enabled

    @property
    def last_opening(self) -> OpeningRecord | None:
        """Return the last door opening record."""
        return self._last_opening

    @property
    def last_call(self) -> CallLogEntry | None:
        """Return the most recent call log entry."""
        return self._last_call

    @property
    def call_log(self) -> list[CallLogEntry]:
        """Return recent call log entries."""
        return self._call_log

    @property
    def stream_session(self) -> FermaxStreamSession | None:
        """Return the active stream session, if any."""
        return self._stream_session

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the API.

        Only fetches device info on each poll (1 API call per 5 min).
        Call log and photos are only fetched after a doorbell ring event
        to minimize unnecessary API requests.
        """
        try:
            device_info = await self.api.get_device_info(self.pairing.device_id)
        except (FermaxAuthError, FermaxApiError) as err:
            raise UpdateFailed(f"Error fetching device info: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

        self.device_info = device_info

        # Fetch call log if FCM token is available
        if self.notification_listener and self.notification_listener.fcm_token:
            try:
                call_log = await self.api.get_call_log(
                    self.notification_listener.fcm_token
                )
                self._call_log = call_log
                if call_log:
                    self._last_call = max(call_log, key=lambda c: c.call_date)

                    # Fetch photo only after a doorbell ring
                    if self._photo_fetch_pending:
                        self._photo_fetch_pending = False
                        latest = self._last_call
                        if latest.photo_id and latest.photo_id != self._last_photo_id:
                            photo = await self.api.get_call_photo(latest.photo_id)
                            if photo:
                                self._last_photo = photo
                                self._last_photo_id = latest.photo_id
            except Exception:
                _LOGGER.warning("Failed to fetch call log/photo", exc_info=True)

        # Fetch latest door opening (1 API call, lightweight)
        try:
            openings = await self.api.get_opening_history(self.pairing.device_id)
            if openings:
                self._last_opening = openings[0]
        except Exception:
            _LOGGER.warning("Failed to fetch opening history", exc_info=True)

        # Fetch DnD status to keep switch state in sync with the server
        if self.notification_listener and self.notification_listener.fcm_token:
            try:
                self._dnd_enabled = await self.api.get_dnd_status(
                    self.pairing.device_id, self.notification_listener.fcm_token,
                )
            except Exception:
                _LOGGER.debug("Failed to fetch DnD status", exc_info=True)

        return {
            "device_id": device_info.device_id,
            "connection_state": device_info.connection_state,
            "status": device_info.status,
            "family": device_info.family,
            "type": device_info.device_type,
            "subtype": device_info.subtype,
            "unit_number": device_info.unit_number,
            "photocaller": device_info.photocaller,
            "streaming_mode": device_info.streaming_mode,
            "is_monitor": device_info.is_monitor,
            "wireless_signal": device_info.wireless_signal,
        }

    async def setup_notifications(self, storage_path: Path) -> None:
        """Set up the FCM notification listener."""
        self._storage_path = storage_path
        self.notification_listener = FermaxNotificationListener(
            hass=self.hass,
            notification_callback=self._handle_notification,
            firebase_api_key=str(self._firebase_config.get("firebase_api_key", "")),
            firebase_sender_id=self._firebase_config.get("firebase_sender_id", 0),
            firebase_app_id=str(self._firebase_config.get("firebase_app_id", "")),
            firebase_project_id=str(
                self._firebase_config.get("firebase_project_id", "")
            ),
            firebase_package_name=str(
                self._firebase_config.get("firebase_package_name", "")
            ),
            token_updated_callback=self._on_fcm_token_rotated,
        )

        # Load persisted last photo for camera preview
        await self._load_last_photo()

        # Load persisted processed-notification IDs so re-deliveries after a
        # restart are recognised by ID (audit #9). The blunt time-based grace is
        # only used on the very first run, before any IDs have been persisted.
        loaded = await self._load_processed_ids()
        self._grace_active = loaded == 0

        fcm_token = await self.notification_listener.register()
        if fcm_token:
            await self.api.register_app_token(fcm_token, active=True)
            await self.notification_listener.start()
            self._notification_start_time = time.monotonic()
            _LOGGER.info(
                "Notification listener started for device %s",
                self.pairing.device_id,
            )

    async def _on_fcm_token_rotated(self, new_token: str) -> None:
        """Re-register a rotated FCM token with Fermax (audit #1).

        Without this, Fermax keeps pushing to the old (dead) token after a
        rotation and the doorbell silently stops working.
        """
        _LOGGER.info("Re-registering rotated FCM token with Fermax")
        with contextlib.suppress(Exception):
            await self.api.register_app_token(new_token, active=True)

    async def stop_notifications(self) -> None:
        """Stop the notification listener."""
        if self.notification_listener:
            if self.notification_listener.fcm_token:
                await self.api.register_app_token(
                    self.notification_listener.fcm_token, active=False
                )
            await self.notification_listener.stop()

    @callback
    def _handle_notification(self, notification: dict, persistent_id: str) -> None:
        """Handle an incoming FCM doorbell notification."""
        # Skip already-processed notifications — recognised by persistent_id
        # across restarts thanks to the persisted ID set (audit #9). This is the
        # primary defence against phantom rings from FCM re-delivery.
        if persistent_id in self._processed_notifications:
            _LOGGER.debug("Skipping duplicate notification: %s", persistent_id)
            return

        # First-run fallback only: with no persisted IDs yet, a blunt time-based
        # grace guards against a re-delivered backlog phantom-ringing on the very
        # first startup. Once IDs are persisted, ID dedup handles re-delivery and
        # a genuine ring right after restart is no longer dropped.
        if (
            self._grace_active
            and self._notification_start_time is not None
            and time.monotonic() - self._notification_start_time < NOTIFICATION_GRACE_PERIOD
        ):
            _LOGGER.debug(
                "Ignoring notification during first-run grace period: %s",
                persistent_id,
            )
            return

        self._processed_notifications.append(persistent_id)
        self.hass.async_create_task(self._save_processed_ids())

        _LOGGER.info(
            "Doorbell notification for %s: %s",
            self.pairing.device_id,
            _redact_notification(notification),
        )

        # Notification data may be nested under "data" key
        data = notification.get("data", notification)

        # ACK the notification for reliability
        fcm_message_id = (
            notification.get("fcmMessageId") or data.get("fcmMessageId") or persistent_id
        )
        notification_type = data.get("FermaxNotificationType", "")
        is_call = notification_type in ("Call", "CallAttend", "CallEnd")
        self.hass.async_create_task(self.api.ack_notification(fcm_message_id, is_call=is_call))

        # Start video stream based on call mode:
        # - Autoon (camera preview button): always start stream
        # - Call (doorbell): depends on call_mode setting
        room_id = data.get("RoomId")
        should_stream = room_id and (
            notification_type == "Autoon"
            or (notification_type == "Call" and self._call_mode != CALL_MODE_NOTIFY)
        )
        if should_stream:
            socket_url = data.get("SocketUrl", DEFAULT_SIGNALING_URL)
            fermax_token = data.get("FermaxToken", "")
            if not _is_trusted_signaling_url(socket_url):
                _LOGGER.warning(
                    "Rejected untrusted signaling URL from notification: %s",
                    socket_url,
                )
                socket_url = DEFAULT_SIGNALING_URL
            # Only record for real doorbell calls, not manual camera previews
            record = notification_type == "Call"
            self.hass.async_create_task(
                self._start_stream(room_id, socket_url, fermax_token, record=record)
            )
            if (
                notification_type == "Call"
                and self._call_mode == CALL_MODE_AUTO_RESPOND
                and self._auto_response_file
            ):
                self.hass.async_create_task(self._auto_respond())

        # Only trigger doorbell ring for actual calls, not auto-on
        if notification_type == "Call":
            self._doorbell_ringing = True
            self._photo_fetch_pending = True

            door_key = data.get("AccessDoorKey", data.get("accessDoorKey", "GENERAL"))
            async_dispatcher_send(
                self.hass,
                SIGNAL_DOORBELL_RING.format(self.pairing.device_id, door_key),
            )

            # Cancel previous reset timer if still pending
            if self._doorbell_reset_unsub:
                self._doorbell_reset_unsub()

            @callback
            def _reset_ringing(_now: Any) -> None:
                """Reset doorbell ringing state."""
                self._doorbell_ringing = False
                async_dispatcher_send(
                    self.hass,
                    SIGNAL_CALL_ENDED.format(self.pairing.device_id),
                )
                self.async_set_updated_data(self.data)
                self._doorbell_reset_unsub = None

            self._doorbell_reset_unsub = async_call_later(
                self.hass, DOORBELL_RESET_SECONDS, _reset_ringing
            )

            # Trigger a data refresh to get any new photos
            self.hass.async_create_task(self.async_request_refresh())

    async def open_door(self, door_name: str = "GENERAL") -> bool:
        """Open a specific door. Uses in-call endpoint if stream is active."""
        success = False

        # Capture the session once to avoid a TOCTOU: a concurrent stop_stream /
        # auto-stop / on-end could null it between the check and use (audit G6).
        session = self._stream_session
        if session and session.is_active:
            fcm_token = (
                self.notification_listener.fcm_token
                if self.notification_listener
                else None
            )
            success = await self.api.open_door_incall(
                device_id=self.pairing.device_id,
                room_id=session.room_id,
                fcm_token=fcm_token,
                call_as=self.pairing.device_id,
            )
        else:
            door = self.pairing.access_doors.get(door_name)
            if not door:
                for d in self.pairing.access_doors.values():
                    door = d
                    break

            if not door:
                _LOGGER.error("No accessible door found for %s", door_name)
                return False

            success = await self.api.open_door(self.pairing.device_id, door.access_id)

        if success:
            async_dispatcher_send(
                self.hass,
                SIGNAL_DOOR_OPENED.format(self.pairing.device_id),
            )

        return success

    async def start_camera_preview(self) -> DivertResponse | None:
        """Start camera preview (auto-on) to view the intercom camera."""
        if not _pymediasoup_available():
            return None
        if not self.notification_listener or not self.notification_listener.fcm_token:
            _LOGGER.error("Cannot start camera: no FCM token available")
            return None

        result = await self.api.auto_on(
            self.pairing.device_id,
            self.notification_listener.fcm_token,
        )

        if result:
            self._camera_active = True

            _LOGGER.info(
                "Camera preview started: %s (%s)",
                result.reason,
                result.description,
            )

            # Cancel previous camera timeout if still pending
            if self._camera_timeout_unsub:
                self._camera_timeout_unsub()

            @callback
            def _deactivate_camera(_now: Any) -> None:
                """Deactivate camera after timeout."""
                self._camera_active = False
                self.async_set_updated_data(self.data)
                self._camera_timeout_unsub = None

            self._camera_timeout_unsub = async_call_later(
                self.hass, CAMERA_TIMEOUT_SECONDS, _deactivate_camera
            )
            self.async_set_updated_data(self.data)

        return result

    async def change_video_source(self) -> DivertResponse | None:
        """Request a video source change on the intercom."""
        if not self.notification_listener or not self.notification_listener.fcm_token:
            return None

        return await self.api.change_video_source(
            self.pairing.device_id,
            self.notification_listener.fcm_token,
        )

    async def set_dnd(self, enabled: bool) -> None:
        """Set Do Not Disturb."""
        if not self.notification_listener or not self.notification_listener.fcm_token:
            return
        await self.api.set_dnd(
            self.pairing.device_id,
            self.notification_listener.fcm_token,
            enabled=enabled,
        )
        self._dnd_enabled = enabled

    async def press_f1(self) -> None:
        """Press F1 auxiliary button."""
        await self.api.press_f1(self.pairing.device_id)

    async def call_guard(self) -> None:
        """Call the building guard."""
        await self.api.call_guard(self.pairing.device_id)

    async def set_photo_caller(self, enabled: bool) -> None:
        """Enable or disable photo caller."""
        await self.api.set_photo_caller(self.pairing.device_id, enabled=enabled)
        if self.device_info:
            self.device_info = replace(self.device_info, photocaller=enabled)

    async def _start_stream(
        self,
        room_id: str,
        signaling_url: str,
        fermax_token: str = "",
        record: bool = True,
    ) -> None:
        """Start a video stream session for the given room."""
        if not _pymediasoup_available():
            return

        # Serialise start/stop so overlapping notifications (e.g. Autoon then
        # Call) can't race and orphan a half-started session (audit #11).
        async with self._stream_lock:
            await self._stop_stream_locked()

            if not self.notification_listener:
                return
            fcm_token = self.notification_listener.fcm_token
            if not fcm_token:
                return
            try:
                oauth_token = fermax_token or await self.api.get_access_token()
            except Exception:
                _LOGGER.warning("Cannot start stream: failed to get access token", exc_info=True)
                return

            # Resolve recordings directory via HA media_dirs for portability (M-5)
            media_root = self.hass.config.media_dirs.get("local", "/media")
            recordings_dir = str(Path(media_root) / RECORDINGS_DIR)

            session = FermaxStreamSession(
                signaling_url=signaling_url,
                oauth_token=oauth_token,
                fcm_token=fcm_token,
                room_id=room_id,
                on_end=None,
                recordings_dir=recordings_dir,
                record=record,
            )

            @callback
            def _on_stream_end() -> None:
                # Identity guard: ignore if a newer session has superseded us,
                # otherwise the old grabber's finally-callback could clobber the
                # new session's state (audit #26).
                if self._stream_session is not session:
                    return
                if session.latest_frame:
                    self._last_photo = session.latest_frame
                    self.hass.async_create_task(self._save_last_photo())
                self._stream_session = None
                self._camera_active = False
                if self._stream_stop_unsub:
                    self._stream_stop_unsub()
                    self._stream_stop_unsub = None
                # Fully tear down the session's relays / socket.io / tasks when
                # the track ends on its own (stop() is idempotent) (audit #45).
                self.hass.async_create_task(session.stop())
                self.async_set_updated_data(self.data)

            session._on_end = _on_stream_end
            self._stream_session = session

            success = await session.start()

            # If a concurrent call superseded us while we were starting, drop
            # our now-orphaned session instead of leaking it.
            if self._stream_session is not session:
                await session.stop()
                return

            if success:
                self._camera_active = True
                _LOGGER.info("Video stream started for room %s", room_id)
                async_dispatcher_send(
                    self.hass, SIGNAL_CAMERA_ON.format(self.pairing.device_id)
                )
                self._arm_auto_stop()
            else:
                _LOGGER.warning("Failed to start video stream for room %s", room_id)
                self._stream_session = None

    def _arm_auto_stop(self) -> None:
        """(Re)arm the unattended-stream auto-stop timer."""
        if self._stream_stop_unsub:
            self._stream_stop_unsub()
            self._stream_stop_unsub = None

        @callback
        def _auto_stop_stream(_now: Any) -> None:
            self._stream_stop_unsub = None
            # Don't cut off a call someone is actively watching via the card;
            # only auto-stop unattended previews (audit G3).
            if self._webrtc_peers:
                _LOGGER.debug("Auto-stop deferred — %d active viewer(s)", len(self._webrtc_peers))
                self._arm_auto_stop()
                return
            _LOGGER.info("Stream auto-stop after %ds (no active viewers)", self._stream_duration)
            self.hass.async_create_task(self.stop_stream())

        self._stream_stop_unsub = async_call_later(
            self.hass, self._stream_duration, _auto_stop_stream
        )

    async def _auto_respond(self) -> None:
        """Send auto-response audio after stream starts."""
        audio_file = self._auto_response_file
        if not audio_file:
            return

        # Confirm the configured file exists and is a regular file before handing
        # it to av.open(), so a stale/garbage path fails cleanly (audit #38).
        def _is_valid_file() -> bool:
            try:
                return Path(audio_file).is_file()
            except (OSError, ValueError):
                return False

        if not await asyncio.to_thread(_is_valid_file):
            _LOGGER.error("Auto-response file is missing or invalid: %s", audio_file)
            return

        # Wait for stream to be ready
        for _ in range(20):
            if self._stream_session and self._stream_session.is_active:
                break
            await asyncio.sleep(0.5)
        if self._stream_session and self._stream_session.is_active:
            await asyncio.sleep(1)  # Extra delay for audio transport
            await self._stream_session.send_audio(audio_file)
            _LOGGER.info("Auto-response sent: %s", audio_file)

    def register_webrtc_peer(self, session_id: str, pc: Any) -> None:
        """Register a WebRTC RTCPeerConnection for lifecycle management."""
        self._webrtc_peers[session_id] = pc

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            state = pc.connectionState
            _LOGGER.debug("WebRTC peer %s connection state: %s", session_id, state)
            if state in ("failed", "closed", "disconnected"):
                self._webrtc_peers.pop(session_id, None)

    def close_webrtc_peer(self, session_id: str) -> None:
        """Close a specific WebRTC peer connection."""
        pc = self._webrtc_peers.pop(session_id, None)
        if pc:
            self.hass.async_create_task(_close_pc(pc))

    async def _close_all_webrtc_peers(self) -> None:
        """Close all active WebRTC peer connections."""
        peers = dict(self._webrtc_peers)
        self._webrtc_peers.clear()
        for sid, pc in peers.items():
            _LOGGER.debug("Closing WebRTC peer %s on stream stop", sid)
            with contextlib.suppress(Exception):
                await pc.close()

    async def stop_stream(self) -> None:
        """Stop the current video stream session and all WebRTC peer connections."""
        async with self._stream_lock:
            await self._stop_stream_locked()

    async def _stop_stream_locked(self) -> None:
        """Stop the stream; caller must already hold self._stream_lock."""
        if self._stream_stop_unsub:
            self._stream_stop_unsub()
            self._stream_stop_unsub = None
        await self._close_all_webrtc_peers()
        if self._stream_session:
            if self._stream_session.latest_frame:
                self._last_photo = self._stream_session.latest_frame
                self.hass.async_create_task(self._save_last_photo())
            await self._stream_session.stop()
            self._stream_session = None
            self._camera_active = False


async def _close_pc(pc: Any) -> None:
    """Safely close an RTCPeerConnection."""
    with contextlib.suppress(Exception):
        await pc.close()
