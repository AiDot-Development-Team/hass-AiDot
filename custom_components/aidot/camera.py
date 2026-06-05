"""Support for Aidot cameras."""

from __future__ import annotations

import asyncio
import logging
import os
import zlib

import aiohttp
import voluptuous as vol
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from aidot.device_client import TALK_PCM_FRAME_BYTES, TALK_PCM_RATE

from .const import DEFAULT_SERVE_PORT_BASE
from .coordinator import AidotCameraUpdateCoordinator, AidotConfigEntry
from .entity import aidot_device_info

_LOGGER = logging.getLogger(__name__)

# Pull model: SDES cameras serve their decrypted stream over a local HTTP-listen
# socket and HA's stream integration / go2rtc PULL it the standard way (no go2rtc
# pre-registration, which a default go2rtc rejects). Each camera gets a stable
# local port so the URL is deterministic. Base is env-overridable.
def _serve_port(name: str) -> int:
    """Deterministic loopback HTTP-serve port for a camera (base..base+399)."""
    base = int(os.environ.get("AIDOT_SERVE_PORT_BASE", DEFAULT_SERVE_PORT_BASE))
    return base + (zlib.crc32(name.encode()) % 400)


SERVICE_TALK = "talk"
SERVICE_PTZ = "ptz"

# Directions accepted by the aidot.ptz service - the exact keys the library's
# async_ptz_move() understands (AVIOCTRLDEFs codes), so they pass straight
# through.  Mirrors onvif.ptz / reolink.ptz_move as an automation-and-card
# friendly entry point alongside the per-direction button entities.
PTZ_DIRECTIONS = [
    "up", "down", "left", "right",
    "left_up", "left_down", "right_up", "right_down",
    "zoom_in", "zoom_out", "stop",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Aidot camera entities."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_cameras() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered
        }
        new = [AidotCamera(c) for c in new_coords.values()]
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    _add_new_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_cameras))

    # Register the two-way-audio (push-to-talk / announce) service. Plays an audio
    # source through the camera speaker via the library's async_speak().
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_TALK,
        {
            vol.Required("media"): cv.string,
            vol.Optional("max_seconds", default=30): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=300)
            ),
        },
        "async_talk",
    )
    platform.async_register_entity_service(
        SERVICE_PTZ,
        {
            vol.Required("direction"): vol.In(PTZ_DIRECTIONS),
            vol.Optional("speed", default=4): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=8)
            ),
        },
        "async_ptz",
    )


class AidotCamera(CoordinatorEntity[AidotCameraUpdateCoordinator], Camera):
    """Representation of an Aidot IP camera."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: AidotCameraUpdateCoordinator) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        info = coordinator.device_client.info
        self._attr_unique_id = info.dev_id
        self._attr_device_info = aidot_device_info(info)

        # Sanitised device ID safe to use as an RTSP stream name.
        self._rtsp_name = info.dev_id.replace("/", "_").replace(":", "_")
        # Cache for the last successfully fetched thumbnail bytes
        self._cached_image: bytes | None = None
        self._image_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Warm the thumbnail cache in the background so the camera card shows an
        # image on first load (before any stream / go2rtc) instead of a blank
        # tile. Non-blocking (a slow cloud fetch must not delay setup), and the
        # task is cancelled on removal so it can't write state on a dead entity.
        task = self.hass.async_create_task(self._prefetch_thumbnail())
        self.async_on_remove(task.cancel)

    async def _prefetch_thumbnail(self) -> None:
        try:
            url = await self.coordinator.device_client.async_get_latest_thumbnail()
        except Exception:  # noqa: BLE001
            return
        if not url:
            return
        # Expose the CDN URL immediately so the camera card (and picture-elements
        # poster) shows the most-recent event thumbnail while the live stream is
        # connecting, instead of a blank tile.
        self._attr_entity_picture = url
        self.async_write_ha_state()
        # Fetch the bytes so async_camera_image() / the HA image proxy can serve
        # them locally if the CDN URL later expires or becomes unreachable.
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    async with self._image_lock:
                        self._cached_image = await resp.read()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Thumbnail bytes fetch failed for %s: %s", self.unique_id, exc)

    async def stream_source(self) -> str | None:
        """Serve a pullable HTTP stream for all camera models (go2rtc pulls it).

        Both paths serve H.264 over a local HTTP-listen socket that HA's stream
        integration / go2rtc pull the standard way (no go2rtc pre-registration):

        - SDES (A001064/A001513): ffmpeg receives the decrypted SRTP directly.
          First call takes 25-70s while the SCTP handshake runs.
        - DTLS (A000088): aiortc does ICE/DTLS/decrypt and the library taps the
          encoded H.264 and -c copy's it to the same serve (no decode/re-encode).

        Subsequent viewers reuse the warm session.
        """
        dc = self.coordinator.device_client

        serve_url = f"http://127.0.0.1:{_serve_port(self._rtsp_name)}/{self._rtsp_name}.ts"
        if dc.stream_rtsp_url is None:
            try:
                await dc.start_keepalive(rtsp_push_url=serve_url)
                _LOGGER.info(
                    "Started HTTP stream serve for %s → %s", self._rtsp_name, serve_url
                )
            except Exception as exc:
                _LOGGER.warning(
                    "Failed to start stream serve for %s: %s", self._rtsp_name, exc
                )
                return None

        # Wait until the serve is actually bound before handing HA the URL, so it
        # connects on the first try instead of getting "Connection refused" and
        # waiting out its ~40s reconnect interval (big cold-start latency win).
        # A warm session returns instantly; SDES (no ready signal) returns at once.
        waiter = getattr(dc, "async_wait_serve_ready", None)
        if waiter is not None:
            try:
                ready = await waiter(timeout=22)
            except Exception:  # noqa: BLE001
                ready = False
            # DTLS connects are per-attempt probabilistic; if the serve never
            # bound (this attempt's WebRTC connect failed), return None so HA
            # retries cleanly rather than handing go2rtc a dead URL (which it
            # reports as "connection refused"). The keepalive keeps reconnecting
            # in the background, so the next view usually succeeds. SDES has no
            # ready signal (waiter returns False by design), so skip it there.
            if not ready and not getattr(dc, "is_sdes_camera", False):
                _LOGGER.debug(
                    "stream_source: %s serve not ready yet (DTLS connect "
                    "pending) - returning None for a clean retry",
                    self._rtsp_name,
                )
                return None

        return serve_url

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return the latest JPEG from the background stream, or a cloud thumbnail."""
        # Prefer the persistent stream buffer (updated by the library every ~1s).
        live = self.coordinator.device_client.latest_jpeg
        if live is not None:
            return live

        # Fallback: fetch the most recent cloud event thumbnail.
        async with self._image_lock:
            url = await self.coordinator.device_client.async_get_latest_thumbnail()
            if not url:
                return self._cached_image

            try:
                session = async_get_clientsession(self.hass)
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        self._cached_image = await resp.read()
                        return self._cached_image
            except Exception as exc:
                _LOGGER.debug("Thumbnail fetch failed for %s: %s", self.unique_id, exc)

            return self._cached_image

    # ------------------------------------------------------------------ #
    # Two-way audio (push-to-talk / announce)
    # ------------------------------------------------------------------ #
    async def async_talk(self, media: str, max_seconds: int = 30) -> None:
        """Play an audio source through the camera speaker (``aidot.talk`` service).

        ``media`` may be a Home Assistant media-source id, an http(s) URL, or a
        local file path. It is transcoded to 8 kHz mono PCM and streamed to the
        camera over the existing WebRTC audio channel (DTLS and SDES cameras alike).
        """
        source = await self._resolve_media(media)
        pcm = await self._decode_pcm_8k(source)
        if not pcm:
            _LOGGER.warning("aidot.talk: no audio decoded from %s", media)
            return

        n = TALK_PCM_FRAME_BYTES
        frames = (pcm[i:i + n].ljust(n, b"\x00") for i in range(0, len(pcm), n))

        def _provider() -> bytes | None:
            return next(frames, None)

        ok = await self.coordinator.device_client.async_speak(
            _provider, max_seconds=max_seconds
        )
        if not ok:
            _LOGGER.warning(
                "aidot.talk: could not establish a talk session for %s", self.unique_id
            )

    async def async_ptz(self, direction: str, speed: int = 4) -> None:
        """Move the camera (``aidot.ptz`` service).

        ``direction`` is one of :data:`PTZ_DIRECTIONS`; ``speed`` is 1-8.
        PTZ commands ride the active stream session, so the camera must be
        streaming (open the live view first). ``stop`` halts continuous motion.
        """
        ok = await self.coordinator.device_client.async_ptz_move(direction, speed)
        if not ok:
            _LOGGER.warning(
                "aidot.ptz: command %s not sent for %s "
                "(camera must be streaming - open the live view first)",
                direction, self.unique_id,
            )

    async def _resolve_media(self, media: str) -> str:
        """Resolve a HA media-source id to a playable URL; pass URLs/paths through."""
        from homeassistant.components import media_source

        if media_source.is_media_source_id(media):
            item = await media_source.async_resolve_media(
                self.hass, media, self.entity_id
            )
            from homeassistant.components.media_player import (
                async_process_play_media_url,
            )

            return async_process_play_media_url(self.hass, item.url)
        return media

    async def _decode_pcm_8k(self, source: str) -> bytes:
        """Transcode any audio source to raw s16le 8 kHz mono PCM via ffmpeg."""
        try:
            from homeassistant.components.ffmpeg import get_ffmpeg_manager

            binary = get_ffmpeg_manager(self.hass).binary
        except Exception:
            binary = "ffmpeg"
        proc = await asyncio.create_subprocess_exec(
            binary, "-nostdin", "-i", source,
            "-f", "s16le", "-ar", str(TALK_PCM_RATE), "-ac", "1", "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        pcm, _ = await proc.communicate()
        return pcm
