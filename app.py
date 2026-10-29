#!/usr/bin/env python3
"""Experimental, single-store Shopify-embedded product migration server.

One deployment is a read-only source OR a separately authorized destination.
Every protected request requires a Shopify App Bridge ID token, validated with
this deployment's app secret and restricted to its one configured shop.
No Shopify access token is returned to the browser or persisted to disk.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
from aiohttp import ClientError, ClientSession, ClientTimeout, web

import migration

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
CONFIG_KEY = web.AppKey("settings", object)
SERVICE_KEY = web.AppKey("migration_service", object)
EXCHANGE_KEY = web.AppKey("token_exchange", object)
CACHE_KEY = web.AppKey("online_token_cache", dict)
TICKETS_KEY = web.AppKey("download_tickets", dict)
REQUEST_AUTH = web.RequestKey("verified_shopify", bool)
ROLE_SCOPES = {
    "source": frozenset({"read_products", "read_inventory"}),
    "destination": frozenset({"read_products", "write_products", "write_inventory", "write_files"}),
}


class RetryShopifySession(ValueError):
    """Shopify rejected a stale ID token; App Bridge should refresh it once."""


@dataclass(frozen=True)
class Settings:
    role: str
    api_key: str
    api_secret: str = field(repr=False)
    allowed_shop: str = ""
    data_dir: Path = ROOT / "data"
    # Only tests set this directly. It isn't controlled by an environment var.
    test_preview: bool = False

    def validate(self):
        if self.role not in ROLE_SCOPES:
            raise ValueError("IMPORTGIANT_ROLE must be source or destination.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", self.api_key):
            raise ValueError("Set SHOPIFY_API_KEY to this app's Client ID.")
        if len(self.api_secret) < 16:
            raise ValueError("Set SHOPIFY_API_SECRET to this app's Client secret.")
        if not migration.STORE_RE.fullmatch(self.allowed_shop):
            raise ValueError("Set SHOPIFY_ALLOWED_SHOP to the original shop.myshopify.com domain.")
        return self


def settings_from_env():
    return Settings(
        role=os.environ.get("IMPORTGIANT_ROLE", "").strip().lower(),
        api_key=os.environ.get("SHOPIFY_API_KEY", "").strip(),
        api_secret=os.environ.get("SHOPIFY_API_SECRET", ""),
        allowed_shop=os.environ.get("SHOPIFY_ALLOWED_SHOP", "").strip().lower(),
        data_dir=Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve(),
    ).validate()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def make_db_factory(path: Path):
    def database():
        conn = sqlite3.connect(path, factory=ClosingConnection, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn
    return database


def _safe_shop_url(value, *, allow_admin_path=False):
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError("Invalid Shopify session.")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        valid_path = parsed.path in (("", "/", "/admin", "/admin/") if allow_admin_path else ("", "/"))
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment or not valid_path
                or not migration.STORE_RE.fullmatch(host)):
            raise ValueError("Invalid Shopify session.")
        return host
    except (ValueError, TypeError):
        raise ValueError("Invalid Shopify session.") from None


def verify_id_token(raw, settings: Settings):
    if not isinstance(raw, str) or not raw or len(raw) > 8192:
        raise ValueError("A Shopify session token is required.")
    try:
        claims = jwt.decode(
            raw, settings.api_secret, algorithms=["HS256"], audience=settings.api_key,
            options={"require": ["exp", "nbf", "iat", "aud", "iss", "dest", "sub", "sid"]},
            leeway=5,
        )
        dest = _safe_shop_url(claims.get("dest"))
        issuer = _safe_shop_url(claims.get("iss"), allow_admin_path=True)
        if dest != issuer or dest != settings.allowed_shop:
            raise ValueError("This deployment is not configured for that Shopify store.")
        sub, sid = str(claims.get("sub") or ""), str(claims.get("sid") or "")
        if not sub or len(sub) > 128 or not sid or len(sid) > 256:
            raise ValueError("Invalid Shopify session.")
        return claims
    except (jwt.PyJWTError, ValueError, TypeError):
        raise ValueError("Shopify session is invalid, expired, or for a different store.") from None


def _scope_set(value):
    if not isinstance(value, str):
        return set()
    scopes = {x.strip() for x in value.split(",") if x.strip()}
    # Shopify considers write_X to include read_X, even when read_X isn't
    # separately listed in the issued token's scopes.
    scopes.update("read_" + s[6:] for s in tuple(scopes) if s.startswith("write_"))
    return scopes


def validate_exchange(data, claims, settings: Settings):
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise ValueError("Shopify did not return an online access token.")
    raw_app_scopes = data.get("scope")
    raw_user_scopes = data.get("associated_user_scope")
    if not isinstance(raw_app_scopes, str) or not isinstance(raw_user_scopes, str):
        raise ValueError("Shopify did not return the app and user scopes; access is blocked.")
    app_scopes, user_scopes = _scope_set(raw_app_scopes), _scope_set(raw_user_scopes)
    required = ROLE_SCOPES[settings.role]
    if not required.issubset(app_scopes) or not required.issubset(user_scopes):
        raise ValueError("The installed app or current user lacks required Shopify permissions.")
    if settings.role == "source":
        original_scopes = {x.strip() for x in raw_app_scopes.split(",") if x.strip()}
        if any(not s.startswith(("read_", "unauthenticated_read_")) for s in original_scopes):
            raise ValueError("The source app has non-read permissions; use a separate read-only app.")
    associated = data.get("associated_user")
    if not isinstance(associated, dict) or str(associated.get("id") or "") != str(claims["sub"]):
        raise ValueError("Shopify returned an unexpected user for this session.")
    expires = data.get("expires_in")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= 30:
        raise ValueError("Shopify returned an invalid online token lifetime.")
    return {"role": settings.role, "shop": settings.allowed_shop, "token": data["access_token"],
            "sub": str(claims["sub"]), "sid": str(claims["sid"]),
            "app_scopes": sorted(app_scopes), "user_scopes": sorted(user_scopes),
            "cache_until": time.monotonic() + min(20, float(expires) - 30)}


async def exchange_token(settings: Settings, id_token: str):
    url = f"https://{settings.allowed_shop}/admin/oauth/access_token"
    payload = {
        "client_id": settings.api_key,
        "client_secret": settings.api_secret,
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": id_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
        "requested_token_type": "urn:shopify:params:oauth:token-type:online-access-token",
    }
    try:
        async with ClientSession(timeout=ClientTimeout(total=25, connect=10)) as session:
            async with session.post(url, data=payload, allow_redirects=False) as response:
                if response.status == 400:
                    raise RetryShopifySession("Shopify needs a fresh session. Reopen the app if this persists.")
                if response.status != 200:
                    raise ValueError("Shopify couldn't exchange this session. Confirm the app is installed and reopen it from Shopify Admin.")
                raw = await response.content.read(65537)
                if len(raw) > 65536:
                    raise ValueError("Shopify returned an unexpectedly large token response.")
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise ValueError("Shopify returned an invalid token response.") from None
    except (ClientError, asyncio.TimeoutError):
        raise ValueError("Could not reach Shopify to authenticate this session.") from None


async def authenticate_request(request):
    settings = request.app[CONFIG_KEY]
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise web.HTTPUnauthorized(reason="Open this app inside Shopify Admin to authenticate.")
    raw = header[7:].strip()
    try:
        claims = verify_id_token(raw, settings)
    except ValueError as exc:
        raise web.HTTPUnauthorized(reason=str(exc)) from None
    key = (str(claims["sub"]), str(claims["sid"]))
    cache = request.app[CACHE_KEY]
    now = time.monotonic()
    # Sensitive final actions require a fresh scope check rather than a cached one.
    force = request.path in {"/api/migration/import", "/api/download-ticket"}
    cached = cache.get(key)
    if cached and cached["cache_until"] > now and not force:
        return cached
    try:
        data = await request.app[EXCHANGE_KEY](settings, raw)
        session = validate_exchange(data, claims, settings)
    except RetryShopifySession as exc:
        raise web.HTTPUnauthorized(reason=str(exc)) from None
    except ValueError as exc:
        raise web.HTTPForbidden(reason=str(exc)) from None
    if len(cache) >= 128:
        for old in list(cache):
            if cache[old]["cache_until"] <= now:
                cache.pop(old, None)
        if len(cache) >= 128:
            cache.clear()
    cache[key] = session
    return session


@web.middleware
async def secure_middleware(request, handler):
    settings = request.app[CONFIG_KEY]
    ctx = None
    try:
        if request.path.startswith("/api/"):
            # JSON endpoints never need multi-gigabyte request bodies. Keep the
            # large limit only for the separately streamed package upload.
            if request.path != "/api/migration/stage" or request.content_type == "application/json":
                request = request.clone(client_max_size=64 * 1024)
            session = await authenticate_request(request)
            request[REQUEST_AUTH] = True
            ctx = migration.CURRENT_SHOP.set(session)
        response = await handler(request)
    except web.HTTPException as exc:
        if request.path.startswith("/api/"):
            headers = {"X-Shopify-Retry-Invalid-Session-Request": "1"} if exc.status == 401 else {}
            response = web.json_response({"error": exc.reason or "Request denied."}, status=exc.status, headers=headers)
        else:
            raise
    finally:
        if ctx is not None:
            migration.CURRENT_SHOP.reset(ctx)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    if request.path == "/" or re.fullmatch(r"/apps/[A-Za-z0-9_-]+/?", request.path):
        ancestors = "*" if settings.test_preview else f"https://{settings.allowed_shop} https://admin.shopify.com"
        response.headers["Content-Security-Policy"] = f"frame-ancestors {ancestors}"
    return response


class EmbeddedMigration(migration.MigrationService):
    def require_auth(self, request):
        if not request.get(REQUEST_AUTH):
            raise web.HTTPUnauthorized(reason="Open this app inside Shopify Admin.")

    def unlocked(self, request):
        return bool(request.get(REQUEST_AUTH))

    def key_ready(self):
        return True


def _json_error(message, status=400):
    return web.json_response({"error": message}, status=status)


async def index(request):
    settings = request.app[CONFIG_KEY]
    template = (PUBLIC / "index.html").read_text(encoding="utf-8")
    document = template.replace("__SHOPIFY_API_KEY__", html.escape(settings.api_key, quote=True))
    return web.Response(text=document, content_type="text/html")


async def health(request):
    return web.json_response({"ok": True})


async def context(request):
    settings = request.app[CONFIG_KEY]
    return web.json_response({"role": settings.role, "shop": settings.allowed_shop,
                              "read_only": settings.role == "source",
                              "max_upload": migration.MAX_BUNDLE})


async def export_start(request):
    if request.app[CONFIG_KEY].role != "source":
        raise web.HTTPForbidden(reason="Exports are available only in the source app.")
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _json_error("Invalid export request.")
    if not isinstance(body, dict) or body.get("mode") != "shopify":
        return _json_error("Only a Shopify source export is supported.")
    return await request.app[SERVICE_KEY].start_export(request)


async def connection_test(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _json_error("Invalid connection request.")
    if not isinstance(body, dict) or body.get("role") != request.app[CONFIG_KEY].role:
        raise web.HTTPForbidden(reason="This deployment can access only its configured store role.")
    return await request.app[SERVICE_KEY].test_connection(request)


async def stage_package(request):
    role = request.app[CONFIG_KEY].role
    if role == "source" and request.content_type != "application/json":
        raise web.HTTPForbidden(reason="The source app can review only its own exported packages.")
    if role == "destination" and not request.content_type.startswith("multipart/"):
        return _json_error("Upload a product ZIP to review it.")
    return await request.app[SERVICE_KEY].stage(request)


async def import_start(request):
    if request.app[CONFIG_KEY].role != "destination":
        raise web.HTTPForbidden(reason="Imports are disabled in the source app.")
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _json_error("Invalid import request.")
    if not isinstance(body, dict) or body.get("force_draft") is not True:
        return _json_error("This experimental importer creates draft products only.")
    return await request.app[SERVICE_KEY].start_import(request)


async def create_download_ticket(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _json_error("Invalid download request.")
    if not isinstance(body, dict):
        return _json_error("Invalid download request.")
    opid = str(body.get("operation_id") or "")
    kind = body.get("kind")
    if not re.fullmatch(r"[0-9a-f]{32}", opid) or kind not in {"package", "report"}:
        return _json_error("Select an available download.")
    service = request.app[SERVICE_KEY]
    row = service.operation(opid)
    field = "filepath" if kind == "package" else "reportpath"
    if not row or row.get("mode") != "shopify" or not row.get(field):
        raise web.HTTPNotFound(reason="The requested file is not available.")
    if kind == "package" and (row.get("kind") != "export" or row.get("status") != "completed"):
        raise web.HTTPNotFound(reason="The package is not available.")
    if kind == "report" and row.get("kind") != "import":
        raise web.HTTPNotFound(reason="The report is not available.")
    path = Path(row[field]).resolve()
    if not path.is_relative_to(service.root.resolve()) or not path.is_file():
        raise web.HTTPNotFound(reason="The requested file is not available.")
    tickets = request.app[TICKETS_KEY]
    now = time.monotonic()
    for key in list(tickets):
        if tickets[key]["expires"] <= now:
            tickets.pop(key, None)
    if len(tickets) >= 128:
        raise web.HTTPTooManyRequests(reason="Too many pending downloads.")
    ticket = secrets.token_urlsafe(32)
    filename = f"importgiant-products-{opid[:8]}.zip" if kind == "package" else f"importgiant-report-{opid[:8]}.jsonl"
    tickets[ticket] = {"path": path, "filename": filename, "expires": now + 60}
    return web.json_response({"ticket": ticket, "expires_in": 60})


async def download_once(request):
    if request.content_length is not None and request.content_length > 4096:
        raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=request.content_length)
    if request.content_type != "application/x-www-form-urlencoded":
        raise web.HTTPBadRequest(reason="Invalid download request.")
    raw = bytearray()
    async for chunk in request.content.iter_chunked(4096):
        raw.extend(chunk)
        if len(raw) > 4096:
            raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(raw))
    try:
        body = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=4)
        values = body.get("ticket", [])
        ticket = values[0] if len(values) == 1 else ""
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(reason="Invalid download request.") from None
    if not isinstance(ticket, str) or len(ticket) > 128:
        raise web.HTTPNotFound(reason="This download link is invalid or expired.")
    entry = request.app[TICKETS_KEY].pop(ticket, None)
    if not entry or entry["expires"] <= time.monotonic() or not entry["path"].is_file():
        raise web.HTTPNotFound(reason="This download link is invalid or expired.")
    response = web.FileResponse(entry["path"])
    response.headers["Content-Disposition"] = f'attachment; filename="{entry["filename"]}"'
    response.headers["Cache-Control"] = "no-store"
    return response


def create_app(settings: Settings, *, exchange=exchange_token, client_factory=None):
    settings.validate()
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = make_db_factory(settings.data_dir / "workspace.sqlite3")
    # A persistent volume is bound to one role and shop. Accidentally pointing
    # a destination deployment at the source volume must fail closed.
    with db() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS app_binding (id INTEGER PRIMARY KEY CHECK (id=1), role TEXT NOT NULL, shop TEXT NOT NULL)")
        bound = connection.execute("SELECT role, shop FROM app_binding WHERE id=1").fetchone()
        if bound and (bound["role"] != settings.role or bound["shop"] != settings.allowed_shop):
            raise ValueError("This data directory belongs to another shop or role. Use a separate persistent volume.")
        if not bound:
            connection.execute("INSERT INTO app_binding (id, role, shop) VALUES (1, ?, ?)", (settings.role, settings.allowed_shop))
    app = web.Application(client_max_size=migration.MAX_BUNDLE + 1024 * 1024, middlewares=[secure_middleware])
    app[CONFIG_KEY] = settings
    app[EXCHANGE_KEY] = exchange
    app[CACHE_KEY] = {}
    app[TICKETS_KEY] = {}
    service = EmbeddedMigration(app, db, lambda _entity: [], settings.data_dir, client_factory or migration.ShopifyClient)
    app[SERVICE_KEY] = service
    app.router.add_get("/", index)
    # Some Shopify Admin launches include an /apps/<handle> entry path.
    # Serve the same shell without forwarding its query string elsewhere.
    app.router.add_get(r"/apps/{handle:[A-Za-z0-9_-]+}", index)
    app.router.add_get(r"/apps/{handle:[A-Za-z0-9_-]+}/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/context", context)
    app.router.add_get("/api/migration/operations", service.operations)
    app.router.add_post("/api/migration/test", connection_test)
    app.router.add_post("/api/migration/export", export_start)
    app.router.add_post("/api/migration/stage", stage_package)
    app.router.add_post("/api/migration/import", import_start)
    app.router.add_post("/api/download-ticket", create_download_ticket)
    app.router.add_post("/download", download_once)
    app.router.add_static("/static/", PUBLIC, show_index=False)
    app.on_cleanup.append(service.shutdown)
    return app


def main():
    # New files and directories should not be world-readable on the host.
    # This does not change permissions on an existing mounted volume.
    os.umask(0o077)
    settings = settings_from_env()
    # The transfer client checks this independent server-side read-only gate.
    os.environ["IMPORTGIANT_ROLE"] = settings.role
    if settings.role == "source":
        os.environ["PORTER_SHOPIFY_READ_ONLY"] = "1"
    else:
        os.environ.pop("PORTER_SHOPIFY_READ_ONLY", None)
    port = int(os.environ.get("PORT", "3000"))
    # No access log: one-time download tickets and request metadata must not be
    # recorded by this process. The deployment proxy must also avoid body logs.
    web.run_app(create_app(settings), host="0.0.0.0", port=port, access_log=None)


if __name__ == "__main__":
    main()
