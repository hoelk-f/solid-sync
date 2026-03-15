from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web

CONFIG_PATH = Path("/data/solid-sync.json")
HA_API_BASE = "http://supervisor/core/api"
HA_WS_URL = "ws://supervisor/core/websocket"
INGRESS_PORT = 8099
ALLOWED_REMOTE_ADDRESSES = {"127.0.0.1", "::1", "172.30.32.2"}
SUCCESS_STATUSES = {200, 201, 202, 204, 205}

LOGGER = logging.getLogger("solid_sync")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SyncProfile:
    id: str
    name: str
    sensor_entity_id: str
    oidc_url: str
    pod_url: str
    client_token: str
    client_secret: str
    resource_path: str
    enabled: bool = True
    last_sync_at: str | None = None
    last_error: str | None = None


class SolidOIDCClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        oidc_url: str,
        pod_url: str,
        client_token: str,
        client_secret: str,
    ) -> None:
        self._session = session
        self._oidc_url = oidc_url.rstrip("/")
        self._pod_url = pod_url.rstrip("/")
        self._client_token = client_token
        self._client_secret = client_secret
        self._token_endpoint: str | None = None

    async def _get_token_endpoint(self) -> str:
        if self._token_endpoint:
            return self._token_endpoint

        async with self._session.get(
            f"{self._oidc_url}/.well-known/openid-configuration"
        ) as response:
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(
                    f"OIDC discovery failed with status {response.status}: {body}"
                )

            config = await response.json()
            token_endpoint = config.get("token_endpoint")
            if not token_endpoint:
                raise RuntimeError("OIDC discovery did not return a token_endpoint")

            self._token_endpoint = token_endpoint
            return token_endpoint

    async def _authenticate(self) -> str:
        token_endpoint = await self._get_token_endpoint()
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_token,
            "client_secret": self._client_secret,
            "scope": "webid",
        }

        async with self._session.post(token_endpoint, data=payload) as response:
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(
                    f"OIDC authentication failed with status {response.status}: {body}"
                )

            data = await response.json()
            access_token = data.get("access_token")
            if not access_token:
                raise RuntimeError("OIDC authentication did not return an access token")

            return access_token

    async def put_json(self, resource_path: str, payload: dict[str, Any]) -> None:
        access_token = await self._authenticate()
        target = resource_path.lstrip("/")
        url = self._pod_url if not target else f"{self._pod_url}/{target}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        async with self._session.put(url, json=payload, headers=headers) as response:
            if response.status not in SUCCESS_STATUSES:
                body = await response.text()
                raise RuntimeError(
                    f"Solid PUT failed with status {response.status}: {body}"
                )


class SolidSyncService:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._profiles: dict[str, SyncProfile] = {}
        self._clients: dict[str, SolidOIDCClient] = {}
        self._config_lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        self._listener_task: asyncio.Task[None] | None = None
        self._listener_connected = False
        self._listener_last_error: str | None = None
        self._supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")

    async def start(self) -> None:
        self._configure_logging()
        if not self._supervisor_token:
            raise RuntimeError("SUPERVISOR_TOKEN is not available")

        timeout = aiohttp.ClientTimeout(total=60)
        self._session = aiohttp.ClientSession(timeout=timeout)
        await self._load_profiles()
        self._listener_task = asyncio.create_task(self._run_listener(), name="ha-listener")
        LOGGER.info("Solid Sync service started with %s profile(s)", len(self._profiles))

    async def stop(self) -> None:
        self._shutdown_event.set()

        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        if self._session:
            await self._session.close()

        LOGGER.info("Solid Sync service stopped")

    def _configure_logging(self) -> None:
        level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
        levels = {
            "TRACE": logging.DEBUG,
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "NOTICE": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "FATAL": logging.CRITICAL,
        }
        logging.basicConfig(
            level=levels.get(level_name, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("HTTP session is not initialized")
        return self._session

    async def list_profiles(self) -> list[dict[str, Any]]:
        async with self._config_lock:
            return [asdict(profile) for profile in self._sorted_profiles()]

    async def list_sensors(self) -> list[dict[str, Any]]:
        states = await self._fetch_states()
        sensors = [
            {
                "entity_id": state["entity_id"],
                "name": state.get("attributes", {}).get("friendly_name")
                or state["entity_id"],
                "state": state.get("state", ""),
            }
            for state in states
            if state.get("entity_id", "").startswith("sensor.")
        ]
        sensors.sort(key=lambda item: item["name"].lower())
        return sensors

    async def get_bootstrap(self) -> dict[str, Any]:
        return {
            "profiles": await self.list_profiles(),
            "sensors": await self.list_sensors(),
            "status": {
                "listener_connected": self._listener_connected,
                "listener_last_error": self._listener_last_error,
                "profile_count": len(self._profiles),
            },
        }

    async def create_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self._build_profile(payload)
        async with self._config_lock:
            self._profiles[profile.id] = profile
            self._clients[profile.id] = self._build_oidc_client(profile)
            await self._save_profiles()
        LOGGER.info("Created profile %s for %s", profile.name, profile.sensor_entity_id)
        return asdict(profile)

    async def update_profile(
        self, profile_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._config_lock:
            existing = self._profiles.get(profile_id)
            if not existing:
                raise web.HTTPNotFound(text="Profile not found")

        profile = self._build_profile(payload, profile_id=profile_id, existing=existing)
        async with self._config_lock:
            self._profiles[profile.id] = profile
            self._clients[profile.id] = self._build_oidc_client(profile)
            await self._save_profiles()
        LOGGER.info("Updated profile %s", profile.name)
        return asdict(profile)

    async def delete_profile(self, profile_id: str) -> None:
        async with self._config_lock:
            if profile_id not in self._profiles:
                raise web.HTTPNotFound(text="Profile not found")
            removed = self._profiles.pop(profile_id)
            self._clients.pop(profile_id, None)
            await self._save_profiles()
        LOGGER.info("Deleted profile %s", removed.name)

    async def test_profile(self, profile_id: str) -> dict[str, Any]:
        async with self._config_lock:
            profile = self._profiles.get(profile_id)
            if not profile:
                raise web.HTTPNotFound(text="Profile not found")

        state = await self._fetch_state(profile.sensor_entity_id)
        if not state or state.get("state") in {"unknown", "unavailable"}:
            raise web.HTTPBadRequest(text="Selected sensor has no usable current state")

        await self._sync_profile(profile.id, state, suppress_errors=False)
        async with self._config_lock:
            updated = self._profiles[profile.id]
            return asdict(updated)

    async def _load_profiles(self) -> None:
        async with self._config_lock:
            if not CONFIG_PATH.exists():
                self._profiles = {}
                self._clients = {}
                return

            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            profiles = raw.get("profiles", [])
            self._profiles = {}
            self._clients = {}

            for item in profiles:
                profile = SyncProfile(**item)
                self._profiles[profile.id] = profile
                self._clients[profile.id] = self._build_oidc_client(profile)

    async def _save_profiles(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "profiles": [asdict(profile) for profile in self._sorted_profiles()],
            "saved_at": utcnow(),
        }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _sorted_profiles(self) -> list[SyncProfile]:
        return sorted(self._profiles.values(), key=lambda profile: profile.name.lower())

    def _build_profile(
        self,
        payload: dict[str, Any],
        profile_id: str | None = None,
        existing: SyncProfile | None = None,
    ) -> SyncProfile:
        name = str(payload.get("name", "")).strip()
        sensor_entity_id = str(payload.get("sensor_entity_id", "")).strip()
        oidc_url = str(payload.get("oidc_url", "")).strip()
        pod_url = str(payload.get("pod_url", "")).strip()
        client_token = str(payload.get("client_token", "")).strip()
        client_secret = str(payload.get("client_secret", "")).strip()
        resource_path = str(payload.get("resource_path", "")).strip() or sensor_entity_id
        enabled = bool(payload.get("enabled", True))

        missing = [
            field_name
            for field_name, value in {
                "name": name,
                "sensor_entity_id": sensor_entity_id,
                "oidc_url": oidc_url,
                "pod_url": pod_url,
                "client_token": client_token,
                "client_secret": client_secret,
            }.items()
            if not value
        ]
        if missing:
            raise web.HTTPBadRequest(
                text=f"Missing required fields: {', '.join(sorted(missing))}"
            )

        if not sensor_entity_id.startswith("sensor."):
            raise web.HTTPBadRequest(text="Only sensor entities are supported right now")

        return SyncProfile(
            id=profile_id or str(uuid.uuid4()),
            name=name,
            sensor_entity_id=sensor_entity_id,
            oidc_url=oidc_url,
            pod_url=pod_url,
            client_token=client_token,
            client_secret=client_secret,
            resource_path=resource_path,
            enabled=enabled,
            last_sync_at=existing.last_sync_at if existing else None,
            last_error=existing.last_error if existing else None,
        )

    def _build_oidc_client(self, profile: SyncProfile) -> SolidOIDCClient:
        return SolidOIDCClient(
            session=self.session,
            oidc_url=profile.oidc_url,
            pod_url=profile.pod_url,
            client_token=profile.client_token,
            client_secret=profile.client_secret,
        )

    async def _fetch_states(self) -> list[dict[str, Any]]:
        async with self.session.get(
            f"{HA_API_BASE}/states",
            headers=self._ha_headers(),
        ) as response:
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(
                    f"Failed to fetch Home Assistant states: {response.status} {body}"
                )
            data = await response.json()
            return list(data)

    async def _fetch_state(self, entity_id: str) -> dict[str, Any] | None:
        async with self.session.get(
            f"{HA_API_BASE}/states/{entity_id}",
            headers=self._ha_headers(),
        ) as response:
            if response.status == 404:
                return None
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(
                    f"Failed to fetch state for {entity_id}: {response.status} {body}"
                )
            return await response.json()

    def _ha_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._supervisor_token}"}

    async def _run_listener(self) -> None:
        backoff_seconds = 5
        while not self._shutdown_event.is_set():
            try:
                LOGGER.info("Connecting to Home Assistant websocket")
                async with self.session.ws_connect(HA_WS_URL, heartbeat=30) as websocket:
                    await self._authenticate_websocket(websocket)
                    await websocket.send_json(
                        {
                            "id": 1,
                            "type": "subscribe_events",
                            "event_type": "state_changed",
                        }
                    )

                    subscribe_result = await websocket.receive_json()
                    if not subscribe_result.get("success", False):
                        raise RuntimeError(
                            f"Subscription failed: {json.dumps(subscribe_result)}"
                        )

                    self._listener_connected = True
                    self._listener_last_error = None
                    LOGGER.info("Subscribed to Home Assistant state_changed events")

                    while not self._shutdown_event.is_set():
                        try:
                            message = await websocket.receive(timeout=60)
                        except asyncio.TimeoutError:
                            continue
                        if message.type == WSMsgType.TEXT:
                            await self._handle_websocket_payload(json.loads(message.data))
                            continue
                        if message.type == WSMsgType.ERROR:
                            raise RuntimeError(str(websocket.exception()))
                        if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}:
                            raise RuntimeError("Home Assistant websocket closed")
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._listener_connected = False
                self._listener_last_error = str(err)
                LOGGER.warning("Listener error: %s", err)
                await asyncio.sleep(backoff_seconds)

    async def _authenticate_websocket(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        auth_required = await websocket.receive_json()
        if auth_required.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected websocket greeting: {auth_required}")

        await websocket.send_json(
            {"type": "auth", "access_token": self._supervisor_token}
        )
        auth_response = await websocket.receive_json()
        if auth_response.get("type") != "auth_ok":
            raise RuntimeError(f"Websocket authentication failed: {auth_response}")

    async def _handle_websocket_payload(self, payload: dict[str, Any]) -> None:
        if payload.get("type") != "event":
            return

        event = payload.get("event", {})
        data = event.get("data", {})
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")

        if not entity_id or not isinstance(new_state, dict):
            return
        if new_state.get("state") in {"unknown", "unavailable"}:
            return

        async with self._config_lock:
            matching_profile_ids = [
                profile.id
                for profile in self._profiles.values()
                if profile.enabled and profile.sensor_entity_id == entity_id
            ]

        if not matching_profile_ids:
            return

        for profile_id in matching_profile_ids:
            await self._sync_profile(profile_id, new_state, suppress_errors=True)

    async def _sync_profile(
        self,
        profile_id: str,
        state: dict[str, Any],
        suppress_errors: bool,
    ) -> None:
        async with self._config_lock:
            profile = self._profiles.get(profile_id)
            client = self._clients.get(profile_id)

        if not profile or not client or not profile.enabled:
            return

        payload = {
            "state": state.get("state"),
            "attributes": state.get("attributes", {}),
        }

        try:
            await client.put_json(profile.resource_path, payload)
        except Exception as err:
            async with self._config_lock:
                current = self._profiles.get(profile_id)
                if current:
                    current.last_error = str(err)
                    await self._save_profiles()
            LOGGER.error("Profile %s sync failed: %s", profile.name, err)
            if not suppress_errors:
                raise
            return

        async with self._config_lock:
            current = self._profiles.get(profile_id)
            if current:
                current.last_sync_at = utcnow()
                current.last_error = None
                await self._save_profiles()

        LOGGER.info("Synced %s to %s", profile.sensor_entity_id, profile.resource_path)


@web.middleware
async def ingress_only_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    allow_direct = os.environ.get("ALLOW_DIRECT", "").lower() in {"1", "true", "yes"}
    if allow_direct:
        return await handler(request)

    if request.remote not in ALLOWED_REMOTE_ADDRESSES:
        raise web.HTTPForbidden(text="Ingress access only")

    return await handler(request)


@web.middleware
async def json_error_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException as err:
        return json_response({"message": err.text or err.reason}, status=err.status)
    except Exception as err:
        LOGGER.exception("Unhandled request failure")
        return json_response({"message": str(err)}, status=500)


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data),
        status=status,
        content_type="application/json",
    )


async def handle_index(request: web.Request) -> web.FileResponse:
    web_root = Path(__file__).parent / "web"
    return web.FileResponse(web_root / "index.html")


async def handle_bootstrap(request: web.Request) -> web.Response:
    service: SolidSyncService = request.app["service"]
    return json_response(await service.get_bootstrap())


async def handle_create_profile(request: web.Request) -> web.Response:
    service: SolidSyncService = request.app["service"]
    payload = await request.json()
    profile = await service.create_profile(payload)
    return json_response(profile, status=201)


async def handle_update_profile(request: web.Request) -> web.Response:
    service: SolidSyncService = request.app["service"]
    payload = await request.json()
    profile = await service.update_profile(request.match_info["profile_id"], payload)
    return json_response(profile)


async def handle_delete_profile(request: web.Request) -> web.Response:
    service: SolidSyncService = request.app["service"]
    await service.delete_profile(request.match_info["profile_id"])
    return json_response({"ok": True})


async def handle_test_profile(request: web.Request) -> web.Response:
    service: SolidSyncService = request.app["service"]
    profile = await service.test_profile(request.match_info["profile_id"])
    return json_response(profile)


async def handle_health(_: web.Request) -> web.Response:
    return json_response({"ok": True, "time": utcnow()})


def create_app(service: SolidSyncService) -> web.Application:
    web_root = Path(__file__).parent / "web"
    app = web.Application(middlewares=[json_error_middleware, ingress_only_middleware])
    app["service"] = service
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/bootstrap", handle_bootstrap)
    app.router.add_post("/api/profiles", handle_create_profile)
    app.router.add_put("/api/profiles/{profile_id}", handle_update_profile)
    app.router.add_delete("/api/profiles/{profile_id}", handle_delete_profile)
    app.router.add_post("/api/profiles/{profile_id}/test", handle_test_profile)
    app.router.add_get("/health", handle_health)
    app.router.add_static("/static", web_root, show_index=False)
    return app


async def main() -> None:
    service = SolidSyncService()
    await service.start()
    app = create_app(service)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=INGRESS_PORT)
    await site.start()
    LOGGER.info("Ingress UI listening on port %s", INGRESS_PORT)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
