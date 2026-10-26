"""LAN-only HTTP boundary; never serves administrative credentials."""

import asyncio
import hmac
import json
import math
from urllib.parse import urlsplit

from aiohttp import web
from hoodini.api.schemas import as_public_dict
from hoodini.devices.service import ForgetError
from hoodini.protocol.profiles import ValidationError

_BODY_LIMIT = 65536


def _invalid():
    raise web.HTTPUnprocessableEntity(text="Invalid request.")


async def _body(request, fields):
    if request.content_length is not None and request.content_length > _BODY_LIMIT:
        _invalid()
    parts = []
    total = 0
    while not request.content.at_eof():
        chunk = await request.content.read(_BODY_LIMIT + 1 - total)
        total += len(chunk)
        if total > _BODY_LIMIT:
            _invalid()
        parts.append(chunk)
    try:
        body = json.loads(b"".join(parts))
        valid = type(body) is dict and not (set(body) - fields)
    except (UnicodeError, ValueError, OverflowError, RecursionError):
        valid = False
    if not valid:
        _invalid()
    return body


def _authorized(request, token):
    try:
        header = request.headers.get("Authorization", "")
        candidate = header.removeprefix("Bearer ")
        valid = header.startswith("Bearer ") and hmac.compare_digest(
            candidate.encode("utf-8"), token.encode("utf-8")
        )
    except (UnicodeError, ValueError, TypeError):
        valid = False
    if not valid:
        raise web.HTTPUnauthorized(text="Unauthorized.")


def _origin_ok(request):
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # nonbrowser client
    try:
        parsed = urlsplit(origin)
        host = request.headers["Host"]
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.netloc
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and parsed.netloc.lower() == host.lower()
            and parsed.scheme == request.scheme
        )
    except (ValueError, KeyError, UnicodeError):
        return False


def create_lan_app(devices, token):
    app = web.Application(client_max_size=_BODY_LIMIT)
    operations = set()

    def finished(task):
        operations.discard(task)
        if not task.cancelled():
            task.exception()  # consume a failed operation after its HTTP caller disconnects

    def owned(coroutine):
        task = asyncio.create_task(coroutine)
        operations.add(task)
        task.add_done_callback(finished)
        return asyncio.shield(task)

    def present(cuid):
        view = devices.get(cuid)
        if view is None:
            raise web.HTTPNotFound(text="Unknown device.")
        return view

    async def listing(request):
        return web.json_response([as_public_dict(view) for view in devices.list()])

    async def device(request):
        return web.json_response(as_public_dict(present(request.match_info["cuid"])))

    async def command(request):
        if not _origin_ok(request):
            raise web.HTTPForbidden(text="Invalid origin.")
        _authorized(request, token)
        cuid = request.match_info["cuid"]
        present(cuid)
        body = await _body(request, {"caps"})
        caps = body.get("caps")
        if type(caps) is not dict or not caps:
            _invalid()
        converted = {}
        for key, value in caps.items():
            if (
                type(key) is not str
                or not key.isascii()
                or not key.isdecimal()
                or len(key) > 3
                or str(int(key)) != key
                or not 0 <= int(key) <= 255
                or type(value) is not int
                or not 0 <= value <= 65535
            ):
                _invalid()
            converted[int(key)] = value
        try:
            result = await owned(devices.set_caps(cuid, converted))
            invalid_caps = False
        except ValidationError:
            invalid_caps = True
        if invalid_caps:
            _invalid()
        if result.status == "offline":
            raise web.HTTPConflict(text="Device offline.")
        if result.status == "timeout":
            raise web.HTTPGatewayTimeout(text="Command timed out.")
        return web.json_response(
            {
                "status": result.status,
                "seq": result.seq,
                "ack_result": result.ack_result,
            }
        )

    async def patch(request):
        if not _origin_ok(request):
            raise web.HTTPForbidden(text="Invalid origin.")
        _authorized(request, token)
        cuid = request.match_info["cuid"]
        present(cuid)
        body = await _body(request, {"info", "profile"})
        if not body:
            _invalid()
        info = body.get("info")
        if "info" in body and (type(info) is not dict or not _json_safe(info)):
            _invalid()
        if "profile" in body and body["profile"] not in (
            None,
            "GENERIC",
            "HIDDEN_ADVANCE",
        ):
            _invalid()
        kwargs = {}
        if "info" in body:
            kwargs["info"] = info
        if "profile" in body:
            kwargs["profile"] = body["profile"]
        error = None
        try:
            updated = await owned(devices.update(cuid, **kwargs))
        except (ValidationError, ValueError):
            error = "invalid"
        except KeyError:
            error = "missing"
        if error == "invalid":
            _invalid()
        if error == "missing":
            raise web.HTTPNotFound(text="Unknown device.")
        return web.json_response(as_public_dict(updated))

    async def delete(request):
        if not _origin_ok(request):
            raise web.HTTPForbidden(text="Invalid origin.")
        _authorized(request, token)
        cuid = request.match_info["cuid"]
        present(cuid)
        error = None
        try:
            await owned(devices.forget(cuid))
        except KeyError:
            error = "missing"
        except ForgetError:
            error = "forget"
        if error == "missing":
            raise web.HTTPNotFound(text="Unknown device.")
        if error == "forget":
            raise web.HTTPInternalServerError(text="Cannot forget device.")
        return web.Response(status=204)

    async def cleanup(app):
        # Own operations even after a disconnected/cancelled HTTP caller; forget's
        # trust-store lease must finish before Runtime may stop the service.
        while operations:
            await asyncio.gather(*tuple(operations), return_exceptions=True)

    app.on_shutdown.append(cleanup)
    prefix = "/api/v1"
    app.router.add_get(prefix + "/devices", listing)
    app.router.add_get(prefix + "/devices/{cuid}", device)
    app.router.add_post(prefix + "/devices/{cuid}/commands", command)
    app.router.add_patch(prefix + "/devices/{cuid}", patch)
    app.router.add_delete(prefix + "/devices/{cuid}", delete)
    return app


def _json_safe(value):
    # Limit private metadata depth before DeviceState's recursive validation/copy.
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 128:
            return False
        if item is None or type(item) in (str, bool, int):
            continue
        if type(item) is float:
            if not math.isfinite(item):
                return False
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is dict:
            if any(type(key) is not str for key in item):
                return False
            pending.extend((child, depth + 1) for child in item.values())
        else:
            return False
    return True
