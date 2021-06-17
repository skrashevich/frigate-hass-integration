"""Frigate HTTP views."""
from __future__ import annotations

from ipaddress import ip_address
import logging
from typing import Any
import urllib.parse

import aiohttp
from aiohttp import hdrs, web
from aiohttp.web_exceptions import HTTPBadGateway
from multidict import CIMultiDict

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import HTTP_NOT_FOUND

_LOGGER: logging.Logger = logging.getLogger(__package__)


class ProxyView(HomeAssistantView):
    """Hass.io view to handle base part."""

    requires_auth = True

    def __init__(self, host: str, websession: aiohttp.ClientSession):
        """Initialize the frigate clips proxy view."""
        self._host = host
        self._websession = websession

    def _create_url(self, **kwargs) -> str | None:
        """Create a URL."""
        raise NotImplementedError  # pragma: no cover

    async def get(
        self,
        request: web.Request,
        **kwargs,
    ) -> web.Response | web.StreamResponse | web.WebSocketResponse:
        """Route data to service."""
        try:
            return await self._handle_request(request, **kwargs)

        except aiohttp.ClientError as err:
            _LOGGER.debug("Reverse proxy error for %s: %s", request.rel_url, err)

        raise HTTPBadGateway() from None

    async def _handle_request(
        self, request: web.Request, **kwargs: Any
    ) -> web.Response | web.StreamResponse:
        """Handle route for request."""
        url = self._create_url(**kwargs)
        if not url:
            return web.Response(status=HTTP_NOT_FOUND)

        data = await request.read()
        source_header = _init_header(request)

        async with self._websession.request(
            request.method,
            url,
            headers=source_header,
            params=request.query,
            allow_redirects=False,
            data=data,
        ) as result:
            headers = _response_header(result)

            # Stream response
            response = web.StreamResponse(status=result.status, headers=headers)
            response.content_type = result.content_type

            try:
                await response.prepare(request)
                async for data in result.content.iter_chunked(4096):
                    await response.write(data)

            except (aiohttp.ClientError, aiohttp.ClientPayloadError) as err:
                _LOGGER.debug("Stream error for %s: %s", request.rel_url, err)

            return response


class ClipsProxyView(ProxyView):
    """A proxy for clips."""

    url = "/api/frigate/clips/{path:.*}"
    name = "api:frigate:clips"

    def _create_url(self, path: str) -> str:
        """Create URL."""
        return urllib.parse.urljoin(self._host, f"/clips/{path}")


class RecordingsProxyView(ProxyView):
    """A proxy for recordings."""

    url = "/api/frigate/recordings/{path:.*}"
    name = "api:frigate:recordings"

    def _create_url(self, path: str) -> str:
        """Create URL."""
        return urllib.parse.urljoin(self._host, f"/recordings/{path}")


class NotificationsProxyView(ProxyView):
    """A proxy for notifications."""

    url = "/api/frigate/notifications/{event_id}/{path:.*}"
    name = "api:frigate:notification"
    requires_auth = False

    def _create_url(self, event_id: str, path: str) -> str | None:
        """Create URL to service."""
        if path == "thumbnail.jpg":
            return urllib.parse.urljoin(
                self._host, f"/api/events/{event_id}/thumbnail.jpg"
            )
        if path == "snapshot.jpg":
            return urllib.parse.urljoin(
                self._host, f"/api/events/{event_id}/snapshot.jpg"
            )

        camera = path.split("/")[0]
        if path.endswith("clip.mp4"):
            return urllib.parse.urljoin(self._host, f"/clips/{camera}-{event_id}.mp4")


def _init_header(request: web.Request) -> CIMultiDict | dict[str, str]:
    """Create initial header."""
    headers = {}

    # filter flags
    for name, value in request.headers.items():
        if name in (
            hdrs.CONTENT_LENGTH,
            hdrs.CONTENT_ENCODING,
            hdrs.SEC_WEBSOCKET_EXTENSIONS,
            hdrs.SEC_WEBSOCKET_PROTOCOL,
            hdrs.SEC_WEBSOCKET_VERSION,
            hdrs.SEC_WEBSOCKET_KEY,
        ):
            continue
        headers[name] = value

    # Set X-Forwarded-For
    forward_for = request.headers.get(hdrs.X_FORWARDED_FOR)
    connected_ip = ip_address(request.transport.get_extra_info("peername")[0])
    if forward_for:
        forward_for = f"{forward_for}, {connected_ip!s}"
    else:
        forward_for = f"{connected_ip!s}"
    headers[hdrs.X_FORWARDED_FOR] = forward_for

    # Set X-Forwarded-Host
    forward_host = request.headers.get(hdrs.X_FORWARDED_HOST)
    if not forward_host:
        forward_host = request.host
    headers[hdrs.X_FORWARDED_HOST] = forward_host

    # Set X-Forwarded-Proto
    forward_proto = request.headers.get(hdrs.X_FORWARDED_PROTO)
    if not forward_proto:
        forward_proto = request.url.scheme
    headers[hdrs.X_FORWARDED_PROTO] = forward_proto

    return headers


def _response_header(response: aiohttp.ClientResponse) -> dict[str, str]:
    """Create response header."""
    headers = {}

    for name, value in response.headers.items():
        if name in (
            hdrs.TRANSFER_ENCODING,
            # Removing Content-Length header for streaming responses
            #   prevents seeking from working for mp4 files
            # hdrs.CONTENT_LENGTH,
            hdrs.CONTENT_TYPE,
            hdrs.CONTENT_ENCODING,
        ):
            continue
        headers[name] = value

    return headers
