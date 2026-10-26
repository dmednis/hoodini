"""Peer-gated admin listener with an independent browser mutation boundary."""

import asyncio
import hmac
import secrets
from dataclasses import dataclass
from importlib.resources import files
from string import Template
from urllib.parse import urlsplit

from aiohttp import web
from hoodini.api.admin_ui import snapshot as render_snapshot
from hoodini.devices.service import ForgetError
from hoodini.protocol.profiles import ValidationError

_FORM_LIMIT = 8192
_TEMPLATE = files("hoodini.api").joinpath("templates/admin.html")
_STATIC = files("hoodini.api").joinpath("static")
_ASSETS = {
    "pico.min.css": ("vendor/pico.min.css", "text/css"),
    "admin.css": ("admin.css", "text/css"),
    "admin.js": ("admin.js", "text/javascript"),
}
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
)


@dataclass(frozen=True)
class IngressContext:
    """Trusted construction input, never inferred from HTTP proxy headers."""

    bind: str
    port: int
    prefix: str


def _authority(value):
    if (
        type(value) is not str
        or not value
        or value.endswith(":")
        or any(c in value for c in ",/\\@?# \t\r\n")
    ):
        return None
    try:
        url = urlsplit("http://" + value)
        if not url.hostname or (url.port is not None and not 1 <= url.port <= 65535):
            return None
        return (url.hostname.lower(), url.port)
    except ValueError:
        return None


def _origin(value):
    if type(value) is not str or not value or any(c in value for c in ",\\?# \t\r\n"):
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme not in ("http", "https")
            or not url.netloc
            or url.username is not None
            or url.password is not None
            or url.path
            or url.query
            or url.fragment
        ):
            return None
        authority = _authority(url.netloc)
        if authority is None:
            return None
        return (
            url.scheme,
            authority[0],
            authority[1] or (443 if url.scheme == "https" else 80),
        )
    except ValueError:
        return None


def _forbidden():
    raise web.HTTPForbidden(text="Forbidden.")


def validate_ingress_context(ingress_context: IngressContext) -> None:
    if (
        not isinstance(ingress_context, IngressContext)
        or type(ingress_context.port) is not int
        or not 1 <= ingress_context.port <= 65535
        or _authority(ingress_context.bind) is None
        or type(ingress_context.prefix) is not str
        or not ingress_context.prefix.startswith("/")
        or any(c in ingress_context.prefix for c in "?#\\\r\n")
        or "//" in ingress_context.prefix
    ):
        raise ValueError("Trusted ingress context required.")


def create_admin_app(
    devices, token, diagnostics, *, addon, ingress_context=None
) -> web.Application:
    if addon:
        validate_ingress_context(ingress_context)
    csrf = secrets.token_urlsafe(32)
    operations = set()
    template = Template(_TEMPLATE.read_text(encoding="utf-8"))

    @web.middleware
    async def boundary(request, handler):
        if addon:
            transport = request.transport
            peer = transport.get_extra_info("peername") if transport else None
            if not peer or peer[0] != "172.30.32.2":
                _forbidden()
        host = _authority(request.headers.get("Host"))
        if host is None or len(request.headers.getall("Host", [])) != 1:
            _forbidden()
        if addon:
            ingress_paths = request.headers.getall("X-Ingress-Path", [])
            if len(ingress_paths) > 1 or (
                ingress_paths and ingress_paths[0] != ingress_context.prefix
            ):
                _forbidden()
            forwarded_host = request.headers.getall("X-Forwarded-Host", [])
            forwarded_proto = request.headers.getall("X-Forwarded-Proto", [])
            if len(forwarded_host) != 1 or len(forwarded_proto) != 1:
                _forbidden()
            if forwarded_proto[0] not in ("http", "https"):
                _forbidden()
            browser_origin = _origin(forwarded_proto[0] + "://" + forwarded_host[0])
            if browser_origin is None:
                _forbidden()
        else:
            local = (
                request.transport.get_extra_info("sockname")
                if request.transport
                else None
            )
            allowed = {"localhost", "127.0.0.1", "::1"}
            if local:
                allowed.add(local[0].lower())
            if ingress_context is not None:
                allowed.add(ingress_context.bind.lower())
            if host[0] not in allowed:
                _forbidden()
            browser_origin = (request.scheme, host[0], host[1] or 80)
        if (
            request.method == "POST"
            and _origin(request.headers.get("Origin")) != browser_origin
        ):
            _forbidden()
        response = await handler(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Content-Security-Policy": _CSP,
                "X-Content-Type-Options": "nosniff",
                # Keep same-origin form Origin headers; no-referrer makes Chrome
                # serialize them as null on HTTP, defeating the mutation boundary.
                "Referrer-Policy": "same-origin",
            }
        )
        return response

    def own(coroutine):
        task = asyncio.create_task(coroutine)
        operations.add(task)

        def finished(done):
            operations.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return asyncio.shield(task)

    async def index(request):
        page = template.substitute(render_snapshot(devices, diagnostics, csrf))
        return web.Response(text=page, content_type="text/html")

    async def snapshot(request):
        return web.json_response(render_snapshot(devices, diagnostics, csrf))

    async def reveal_token(request):
        # Same peer/host boundary as the page; no CORS or token in initial HTML.
        return web.json_response({"token": token})

    async def asset(request):
        entry = _ASSETS.get(request.match_info["name"])
        if entry is None:
            raise web.HTTPNotFound()
        path, content_type = entry
        return web.Response(
            body=_STATIC.joinpath(path).read_bytes(), content_type=content_type
        )

    async def mutate(request):
        if request.content_length is not None and request.content_length > _FORM_LIMIT:
            raise web.HTTPUnprocessableEntity(text="Invalid form.")
        parts = []
        total = 0
        while not request.content.at_eof():
            chunk = await request.content.read(_FORM_LIMIT + 1 - total)
            total += len(chunk)
            if total > _FORM_LIMIT:
                raise web.HTTPUnprocessableEntity(text="Invalid form.")
            parts.append(chunk)
        data = b"".join(parts)
        try:
            from urllib.parse import parse_qsl

            if request.content_type != "application/x-www-form-urlencoded" or any(
                data[index : index + 1] == b"%"
                and (
                    index + 2 >= len(data)
                    or any(
                        c not in b"0123456789abcdefABCDEF"
                        for c in data[index + 1 : index + 3]
                    )
                )
                for index in range(len(data))
            ):
                raise ValueError
            fields = parse_qsl(
                data.decode("utf-8", "strict"),
                keep_blank_values=True,
                strict_parsing=True,
                encoding="utf-8",
                errors="strict",
            )
            values = dict(fields)
            required = {"csrf", "cuid"} | (
                {"profile"} if request.path == "/profile" else set()
            )
            if (
                len(fields) != len(required)
                or set(values) != required
                or not values["cuid"]
            ):
                raise ValueError
            valid = hmac.compare_digest(
                values["csrf"].encode("utf-8"), csrf.encode("ascii")
            )
        except (ValueError, UnicodeError, TypeError):
            raise web.HTTPUnprocessableEntity(text="Invalid form.") from None
        if not valid:
            _forbidden()
        cuid = values["cuid"]
        if devices.get(cuid) is None:
            raise web.HTTPNotFound(text="Unknown device.")
        try:
            if request.path == "/profile":
                profile = values["profile"] or None
                if profile not in (None, "GENERIC", "HIDDEN_ADVANCE"):
                    raise web.HTTPUnprocessableEntity(text="Invalid form.")
                await own(devices.update(cuid, profile=profile))
            else:
                await own(devices.forget(cuid))
        except (ValidationError, ValueError):
            raise web.HTTPUnprocessableEntity(text="Invalid form.") from None
        except KeyError:
            raise web.HTTPNotFound(text="Unknown device.") from None
        except ForgetError:
            raise web.HTTPInternalServerError(text="Cannot forget device.") from None
        raise web.HTTPSeeOther(location="./")

    async def cleanup(app):
        while operations:
            await asyncio.gather(*tuple(operations), return_exceptions=True)

    app = web.Application(middlewares=[boundary], client_max_size=_FORM_LIMIT)
    app.on_shutdown.append(cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/snapshot", snapshot)
    app.router.add_get("/token", reveal_token)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_post("/profile", mutate)
    app.router.add_post("/forget", mutate)
    return app
