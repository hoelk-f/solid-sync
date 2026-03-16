from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web

CONFIG_PATH = Path("/data/solid-sync.json")
CONFIG_SCHEMA_VERSION = 4
HA_API_BASE = "http://supervisor/core/api"
HA_WS_URL = "ws://supervisor/core/websocket"
INGRESS_PORT = 8099
ALLOWED_REMOTE_ADDRESSES = {"127.0.0.1", "::1", "172.30.32.2"}
SUCCESS_STATUSES = {200, 201, 202, 204, 205}

LOGGER = logging.getLogger("solid_sync")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def add_day(value: str) -> str:
    return (parse_timestamp(value) + timedelta(days=1)).isoformat()


def normalize_measurement_key(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_")
    return normalized


def default_measurement_key(entity_id: str) -> str:
    object_id = entity_id.split(".", 1)[-1]
    return normalize_measurement_key(object_id) or "value"


@dataclass
class SolidSettings:
    oidc_url: str = ""
    pod_url: str = ""
    client_token: str = ""
    client_secret: str = ""

    def is_complete(self) -> bool:
        return all(
            [
                self.oidc_url.strip(),
                self.pod_url.strip(),
                self.client_token.strip(),
                self.client_secret.strip(),
            ]
        )


@dataclass
class SyncMeasurement:
    key: str
    entity_id: str


@dataclass
class SyncProfile:
    id: str
    name: str
    resource_path: str
    measurements: list[SyncMeasurement] = field(default_factory=list)
    pending_entries: list[dict[str, Any]] = field(default_factory=list)
    next_flush_at: str | None = None
    last_sync_at: str | None = None
    last_error: str | None = None
    last_resource_path: str | None = None


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

    async def get_access_token(self) -> str:
        return await self._authenticate()

    def _build_resource_url(self, resource_path: str) -> str:
        target = self._normalize_resource_path(resource_path)
        return self._pod_url if not target else f"{self._pod_url}/{target}"

    def _build_container_url(self, container_path: str = "") -> str:
        target = container_path.strip().strip("/")
        base = f"{self._pod_url}/"
        return base if not target else f"{base}{target}/"

    def _normalize_resource_path(self, resource_path: str) -> str:
        target = resource_path.strip().strip("/")
        if not target:
            raise RuntimeError("Resource path is empty")
        return target

    async def ensure_parent_containers(
        self,
        resource_path: str,
        access_token: str | None = None,
    ) -> None:
        target = self._normalize_resource_path(resource_path)
        segments = target.split("/")
        if len(segments) < 2:
            return

        token = access_token or await self._authenticate()
        current_path = ""
        for segment in segments[:-1]:
            current_path = f"{current_path}/{segment}" if current_path else segment
            await self._ensure_container(current_path, token)

    async def _ensure_container(self, container_path: str, access_token: str) -> None:
        container_url = self._build_container_url(container_path)
        if await self._container_exists(container_url, access_token):
            return

        parent_parts = container_path.split("/")[:-1]
        parent_url = self._build_container_url("/".join(parent_parts))
        slug = container_path.split("/")[-1]
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "text/turtle",
            "Link": '<http://www.w3.org/ns/ldp#BasicContainer>; rel="type"',
            "Slug": slug,
        }
        body = (
            "@prefix ldp: <http://www.w3.org/ns/ldp#>.\n"
            "<> a ldp:Container, ldp:BasicContainer .\n"
        )

        async with self._session.post(parent_url, data=body, headers=headers) as response:
            if response.status in SUCCESS_STATUSES:
                return
            if response.status == 409 and await self._container_exists(
                container_url, access_token
            ):
                return

            body_text = await response.text()
            raise RuntimeError(
                f"Solid container creation failed for {container_path}: "
                f"{response.status} {body_text}"
            )

    async def _container_exists(self, container_url: str, access_token: str) -> bool:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/turtle, application/ld+json, application/json;q=0.9, */*;q=0.1",
        }

        async with self._session.head(
            container_url,
            headers=headers,
            allow_redirects=True,
        ) as response:
            if response.status in SUCCESS_STATUSES or response.status == 200:
                return True
            if response.status == 404:
                return False
            if response.status not in {400, 405, 501}:
                body_text = await response.text()
                raise RuntimeError(
                    f"Solid container probe failed for {container_url}: "
                    f"{response.status} {body_text}"
                )

        async with self._session.get(
            container_url,
            headers=headers,
            allow_redirects=True,
        ) as response:
            if response.status in SUCCESS_STATUSES or response.status == 200:
                return True
            if response.status == 404:
                return False

            body_text = await response.text()
            raise RuntimeError(
                f"Solid container probe failed for {container_url}: "
                f"{response.status} {body_text}"
            )

    async def get_json(
        self,
        resource_path: str,
        access_token: str | None = None,
    ) -> Any | None:
        token = access_token or await self._authenticate()
        url = self._build_resource_url(resource_path)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        async with self._session.get(url, headers=headers) as response:
            if response.status == 404:
                return None
            if response.status == 204:
                return None
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(
                    f"Solid GET failed with status {response.status}: {body}"
                )

            body = await response.text()
            if not body.strip():
                return None

        try:
            return json.loads(body)
        except json.JSONDecodeError as err:
            raise RuntimeError(
                "Solid resource is not valid JSON and cannot be appended"
            ) from err

    async def put_json(
        self,
        resource_path: str,
        payload: dict[str, Any],
        access_token: str | None = None,
    ) -> None:
        token = access_token or await self._authenticate()
        url = self._build_resource_url(resource_path)
        headers = {
            "Authorization": f"Bearer {token}",
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
        self._settings = SolidSettings()
        self._profiles: dict[str, SyncProfile] = {}
        self._client: SolidOIDCClient | None = None
        self._config_lock = asyncio.Lock()
        self._profile_locks: dict[str, asyncio.Lock] = {}
        self._shutdown_event = asyncio.Event()
        self._listener_task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._listener_connected = False
        self._listener_last_error: str | None = None
        self._supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")

    async def start(self) -> None:
        self._configure_logging()
        if not self._supervisor_token:
            raise RuntimeError("SUPERVISOR_TOKEN is not available")

        timeout = aiohttp.ClientTimeout(total=60)
        self._session = aiohttp.ClientSession(timeout=timeout)
        await self._load_config()
        self._listener_task = asyncio.create_task(self._run_listener(), name="ha-listener")
        self._flush_task = asyncio.create_task(self._run_flush_loop(), name="daily-flush")
        LOGGER.info("Solid Sync service started with %s profile(s)", len(self._profiles))

    async def stop(self) -> None:
        self._shutdown_event.set()

        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
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
            return [self._serialize_profile(profile) for profile in self._sorted_profiles()]

    async def list_entities(self) -> list[dict[str, Any]]:
        states = await self._fetch_states()
        entities = [
            {
                "entity_id": state["entity_id"],
                "name": state.get("attributes", {}).get("friendly_name")
                or state["entity_id"],
                "state": state.get("state", ""),
                "domain": state.get("entity_id", "").split(".", 1)[0],
            }
            for state in states
            if state.get("entity_id")
        ]
        entities.sort(key=lambda item: (item["name"].lower(), item["entity_id"].lower()))
        return entities

    async def get_bootstrap(self) -> dict[str, Any]:
        async with self._config_lock:
            settings = asdict(self._settings)
            profile_count = len(self._profiles)

        return {
            "settings": settings,
            "profiles": await self.list_profiles(),
            "entities": await self.list_entities(),
            "status": {
                "listener_connected": self._listener_connected,
                "listener_last_error": self._listener_last_error,
                "profile_count": profile_count,
                "settings_complete": self._settings.is_complete(),
            },
        }

    async def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self._build_settings(payload)
        async with self._config_lock:
            self._settings = settings
            self._client = None
            await self._save_config()
            return asdict(self._settings)

    async def create_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self._build_profile(payload)
        async with self._config_lock:
            self._profiles[profile.id] = profile
            self._profile_locks.setdefault(profile.id, asyncio.Lock())
            await self._save_config()
        LOGGER.info("Created profile %s with %s measurements", profile.name, len(profile.measurements))
        return self._serialize_profile(profile)

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
            self._profile_locks.setdefault(profile.id, asyncio.Lock())
            await self._save_config()
        LOGGER.info("Updated profile %s", profile.name)
        return self._serialize_profile(profile)

    async def delete_profile(self, profile_id: str) -> None:
        async with self._config_lock:
            if profile_id not in self._profiles:
                raise web.HTTPNotFound(text="Profile not found")
            removed = self._profiles.pop(profile_id)
            self._profile_locks.pop(profile_id, None)
            await self._save_config()
        LOGGER.info("Deleted profile %s", removed.name)

    async def test_profile(self, profile_id: str) -> dict[str, Any]:
        await self._queue_profile_snapshot(profile_id, suppress_errors=False)
        await self._flush_profile(profile_id, suppress_errors=False)
        async with self._config_lock:
            profile = self._profiles.get(profile_id)
            if not profile:
                raise web.HTTPNotFound(text="Profile not found")
            return self._serialize_profile(profile)

    async def _load_config(self) -> None:
        async with self._config_lock:
            if not CONFIG_PATH.exists():
                self._settings = SolidSettings()
                self._profiles = {}
                self._client = None
                self._profile_locks = {}
                return

            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            settings_data = raw.get("settings") or self._migrate_settings(raw)
            self._settings = SolidSettings(
                oidc_url=str(settings_data.get("oidc_url", "")).strip(),
                pod_url=str(settings_data.get("pod_url", "")).strip(),
                client_token=str(settings_data.get("client_token", "")).strip(),
                client_secret=str(settings_data.get("client_secret", "")).strip(),
            )
            self._profiles = {}
            self._client = None
            self._profile_locks = {}

            for item in raw.get("profiles", []):
                profile = self._profile_from_dict(item)
                self._profiles[profile.id] = profile
                self._profile_locks[profile.id] = self._profile_locks.get(
                    profile.id, asyncio.Lock()
                )

    def _migrate_settings(self, raw: dict[str, Any]) -> dict[str, str]:
        profiles = raw.get("profiles", [])
        if not profiles:
            return {}

        first = profiles[0]
        return {
            "oidc_url": first.get("oidc_url", ""),
            "pod_url": first.get("pod_url", ""),
            "client_token": first.get("client_token", ""),
            "client_secret": first.get("client_secret", ""),
        }

    def _profile_from_dict(self, item: dict[str, Any]) -> SyncProfile:
        measurements_raw = item.get("measurements")
        if measurements_raw is None and item.get("sensor_entity_id"):
            measurements_raw = [
                {
                    "key": default_measurement_key(str(item.get("sensor_entity_id", ""))),
                    "entity_id": str(item.get("sensor_entity_id", "")),
                }
            ]

        measurements = [
            SyncMeasurement(
                key=normalize_measurement_key(str(measurement.get("key", "")).strip())
                or default_measurement_key(str(measurement.get("entity_id", ""))),
                entity_id=str(measurement.get("entity_id", "")).strip(),
            )
            for measurement in (measurements_raw or [])
            if str(measurement.get("entity_id", "")).strip()
        ]
        pending_entries_raw = item.get("pending_entries")
        pending_entries = (
            self._normalize_entries(pending_entries_raw)
            if isinstance(pending_entries_raw, list)
            else []
        )
        next_flush_at = item.get("next_flush_at")
        if pending_entries and not next_flush_at:
            next_flush_at = add_day(pending_entries[0]["captured_at"])

        return SyncProfile(
            id=str(item.get("id", uuid.uuid4())),
            name=str(item.get("name", "")).strip(),
            resource_path=str(item.get("resource_path", "")).strip(),
            measurements=measurements,
            pending_entries=pending_entries,
            next_flush_at=next_flush_at,
            last_sync_at=item.get("last_sync_at"),
            last_error=item.get("last_error"),
            last_resource_path=item.get("last_resource_path"),
        )

    async def _save_config(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "settings": asdict(self._settings),
            "profiles": [asdict(profile) for profile in self._sorted_profiles()],
            "saved_at": utcnow(),
        }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _sorted_profiles(self) -> list[SyncProfile]:
        return sorted(self._profiles.values(), key=lambda profile: profile.name.lower())

    def _serialize_profile(self, profile: SyncProfile) -> dict[str, Any]:
        data = asdict(profile)
        data.pop("pending_entries", None)
        data["pending_entry_count"] = len(profile.pending_entries)
        return data

    def _build_settings(self, payload: dict[str, Any]) -> SolidSettings:
        settings = SolidSettings(
            oidc_url=str(payload.get("oidc_url", "")).strip(),
            pod_url=str(payload.get("pod_url", "")).strip(),
            client_token=str(payload.get("client_token", "")).strip(),
            client_secret=str(payload.get("client_secret", "")).strip(),
        )

        missing = [
            field_name
            for field_name, value in {
                "oidc_url": settings.oidc_url,
                "pod_url": settings.pod_url,
                "client_token": settings.client_token,
                "client_secret": settings.client_secret,
            }.items()
            if not value
        ]
        if missing:
            raise web.HTTPBadRequest(
                text=f"Missing required settings: {', '.join(sorted(missing))}"
            )

        return settings

    def _build_profile(
        self,
        payload: dict[str, Any],
        profile_id: str | None = None,
        existing: SyncProfile | None = None,
    ) -> SyncProfile:
        name = str(payload.get("name", "")).strip()
        resource_path = str(payload.get("resource_path", "")).strip()
        measurements_payload = payload.get("measurements")

        if not isinstance(measurements_payload, list):
            raise web.HTTPBadRequest(text="measurements must be a list")

        measurements: list[SyncMeasurement] = []
        seen_keys: set[str] = set()
        for item in measurements_payload:
            key = normalize_measurement_key(str(item.get("key", "")).strip())
            entity_id = str(item.get("entity_id", "")).strip()
            if not key or not entity_id:
                raise web.HTTPBadRequest(
                    text="Each measurement requires a key and an entity_id"
                )
            if key in seen_keys:
                raise web.HTTPBadRequest(
                    text=f"Duplicate measurement key: {key}"
                )
            seen_keys.add(key)
            measurements.append(SyncMeasurement(key=key, entity_id=entity_id))

        missing = [
            field_name
            for field_name, value in {
                "name": name,
                "resource_path": resource_path,
            }.items()
            if not value
        ]
        if missing:
            raise web.HTTPBadRequest(
                text=f"Missing required fields: {', '.join(sorted(missing))}"
            )
        if not measurements:
            raise web.HTTPBadRequest(text="At least one measurement is required")

        return SyncProfile(
            id=profile_id or str(uuid.uuid4()),
            name=name,
            resource_path=resource_path,
            measurements=measurements,
            pending_entries=list(existing.pending_entries) if existing else [],
            next_flush_at=existing.next_flush_at if existing else None,
            last_sync_at=existing.last_sync_at if existing else None,
            last_error=existing.last_error if existing else None,
            last_resource_path=existing.last_resource_path if existing else None,
        )

    def _get_client(self) -> SolidOIDCClient:
        if not self._settings.is_complete():
            raise RuntimeError("Solid settings are incomplete")

        if self._client is None:
            self._client = SolidOIDCClient(
                session=self.session,
                oidc_url=self._settings.oidc_url,
                pod_url=self._settings.pod_url,
                client_token=self._settings.client_token,
                client_secret=self._settings.client_secret,
            )

        return self._client

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

    def _get_profile_lock(self, profile_id: str) -> asyncio.Lock:
        return self._profile_locks.setdefault(profile_id, asyncio.Lock())

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

    async def _run_flush_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                await self._flush_due_profiles()
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as err:
                LOGGER.warning("Flush loop error: %s", err)
                await asyncio.sleep(60)

    async def _authenticate_websocket(
        self, websocket: aiohttp.ClientWebSocketResponse
    ) -> None:
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
                if any(
                    measurement.entity_id == entity_id
                    for measurement in profile.measurements
                )
            ]

        for profile_id in matching_profile_ids:
            await self._queue_profile_snapshot(profile_id, suppress_errors=True)

    async def _queue_profile_snapshot(self, profile_id: str, suppress_errors: bool) -> None:
        async with self._get_profile_lock(profile_id):
            async with self._config_lock:
                profile = self._profiles.get(profile_id)

            if not profile:
                return

            try:
                snapshot = await self._build_snapshot(profile)
            except Exception as err:
                async with self._config_lock:
                    current = self._profiles.get(profile_id)
                    if current:
                        current.last_error = str(err)
                        await self._save_config()
                LOGGER.error("Profile %s sync failed: %s", profile.name, err)
                if not suppress_errors:
                    raise
                return

            async with self._config_lock:
                current = self._profiles.get(profile_id)
                if current:
                    current.pending_entries.append(snapshot)
                    if not current.next_flush_at:
                        current.next_flush_at = add_day(snapshot["captured_at"])
                    current.last_error = None
                    await self._save_config()

            LOGGER.info(
                "Queued snapshot for profile %s (%s pending)",
                profile.name,
                len(current.pending_entries) if current else 0,
            )

    async def _flush_due_profiles(self) -> None:
        now = datetime.now(timezone.utc)
        async with self._config_lock:
            due_profile_ids = [
                profile.id
                for profile in self._profiles.values()
                if profile.pending_entries
                and profile.next_flush_at
                and parse_timestamp(profile.next_flush_at) <= now
            ]

        for profile_id in due_profile_ids:
            await self._flush_profile(profile_id, suppress_errors=True)

    async def _flush_profile(self, profile_id: str, suppress_errors: bool) -> None:
        async with self._get_profile_lock(profile_id):
            async with self._config_lock:
                profile = self._profiles.get(profile_id)

            if not profile or not profile.pending_entries:
                return

            pending_entries = list(profile.pending_entries)
            flushed_at = utcnow()

            try:
                client = self._get_client()
                access_token = await client.get_access_token()
                await client.ensure_parent_containers(
                    profile.resource_path,
                    access_token=access_token,
                )
                document = await self._build_appended_document(
                    client,
                    profile,
                    pending_entries,
                    updated_at=flushed_at,
                    access_token=access_token,
                )
                target_path = profile.resource_path
                await client.put_json(
                    target_path,
                    document,
                    access_token=access_token,
                )
            except Exception as err:
                async with self._config_lock:
                    current = self._profiles.get(profile_id)
                    if current:
                        current.last_error = str(err)
                        await self._save_config()
                LOGGER.error("Profile %s flush failed: %s", profile.name, err)
                if not suppress_errors:
                    raise
                return

            async with self._config_lock:
                current = self._profiles.get(profile_id)
                if current:
                    current.pending_entries = []
                    current.next_flush_at = None
                    current.last_sync_at = flushed_at
                    current.last_error = None
                    current.last_resource_path = target_path
                    await self._save_config()

            LOGGER.info(
                "Flushed %s queued snapshot(s) for profile %s to %s",
                len(pending_entries),
                profile.name,
                target_path,
            )

    async def _build_snapshot(self, profile: SyncProfile) -> dict[str, Any]:
        states = await asyncio.gather(
            *[self._fetch_state(measurement.entity_id) for measurement in profile.measurements]
        )

        measurements_payload: dict[str, Any] = {}
        missing_entities: list[str] = []

        for measurement, state in zip(profile.measurements, states):
            if state is None:
                missing_entities.append(measurement.entity_id)
                continue

            measurements_payload[measurement.key] = {
                "entity_id": measurement.entity_id,
                "state": state.get("state"),
                "attributes": state.get("attributes", {}),
                "last_changed": state.get("last_changed"),
                "last_updated": state.get("last_updated"),
            }

        if missing_entities:
            raise RuntimeError(
                "Missing current states for: " + ", ".join(sorted(missing_entities))
            )

        return {
            "captured_at": utcnow(),
            "measurements": measurements_payload,
        }

    async def _build_appended_document(
        self,
        client: SolidOIDCClient,
        profile: SyncProfile,
        snapshots: list[dict[str, Any]],
        updated_at: str,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        existing = await client.get_json(
            profile.resource_path,
            access_token=access_token,
        )
        entries = self._extract_existing_entries(existing)
        entries.extend(snapshots)
        return {
            "profile": profile.name,
            "resource_path": profile.resource_path,
            "updated_at": updated_at,
            "entries": entries,
        }

    def _extract_existing_entries(self, existing: Any) -> list[dict[str, Any]]:
        if existing is None:
            return []

        if isinstance(existing, dict):
            raw_entries = existing.get("entries")
            if isinstance(raw_entries, list):
                entries = self._normalize_entries(raw_entries)
                if not entries and raw_entries:
                    raise RuntimeError(
                        "Existing Solid resource contains unsupported entry data"
                    )
                return entries

            legacy_entry = self._normalize_snapshot_entry(existing)
            if legacy_entry:
                return [legacy_entry]

            raise RuntimeError(
                "Existing Solid resource must be a snapshot object or a document with entries"
            )

        if isinstance(existing, list):
            entries = self._normalize_entries(existing)
            if not entries and existing:
                raise RuntimeError(
                    "Existing Solid resource contains unsupported entry data"
                )
            return entries

        raise RuntimeError("Existing Solid resource must be a JSON object or array")

    def _normalize_entries(self, entries: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in entries:
            entry = self._normalize_snapshot_entry(item)
            if entry:
                normalized.append(entry)
        return normalized

    def _normalize_snapshot_entry(self, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None

        captured_at = str(item.get("captured_at", "")).strip()
        measurements = item.get("measurements")
        if not captured_at or not isinstance(measurements, dict):
            return None

        return {
            "captured_at": captured_at,
            "measurements": measurements,
        }


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


async def handle_update_settings(request: web.Request) -> web.Response:
    service: SolidSyncService = request.app["service"]
    payload = await request.json()
    settings = await service.update_settings(payload)
    return json_response(settings)


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
    app.router.add_put("/api/settings", handle_update_settings)
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
