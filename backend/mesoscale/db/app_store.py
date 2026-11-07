"""Async SQLite WAL store for OLTP application state, locations, check-ins, and push."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

DEFAULT_NOTIFICATION_PREFS: dict[str, Any] = {
    "enabled": True,
    "nws_warnings": True,
    "nws_watches": True,
    "hail_alerts": True,
    "hail_min_size_in": 0.50,
    "hail_lead_minutes": 45,
    "hail_bypass_quiet_hours": True,
    "model_temp_shift_f": 8.0,
    "frontal_bust_risk": True,
    "freezing_rain_risk": True,
    "quiet_hours_enabled": True,
    "quiet_start": "22:30",
    "quiet_end": "06:30",
}

DEFAULT_PWS_CONFIG: dict[str, Any] = {
    "enabled": False,
    "provider": "none",  # none | tempest | ambient | ecowitt
    "station_id": "",
    "api_key": "",
    "application_key": "",
    "elevation_m": None,
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AppStore:
    """Async SQLite WAL repository for application metadata (zero pre-seeded cities)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("PRAGMA foreign_keys=ON;")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS pinned_locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    address_label TEXT NOT NULL DEFAULT '',
                    admin1 TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT 'US',
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    elevation_m REAL NOT NULL DEFAULT 350.0,
                    timezone TEXT NOT NULL DEFAULT 'America/Chicago',
                    stations_json TEXT NOT NULL DEFAULT '[]',
                    alerts_enabled INTEGER NOT NULL DEFAULT 1,
                    backfill_status TEXT NOT NULL DEFAULT 'pending',
                    backfill_progress REAL NOT NULL DEFAULT 0.0,
                    backfill_message TEXT NOT NULL DEFAULT '',
                    model_status TEXT NOT NULL DEFAULT 'untrained',
                    created_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_registry (
                    location_id INTEGER PRIMARY KEY REFERENCES pinned_locations(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL DEFAULT 1,
                    trained_at_utc TEXT NOT NULL,
                    train_rows INTEGER NOT NULL DEFAULT 0,
                    artifact_path TEXT NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    aci_state_json TEXT NOT NULL DEFAULT '{}',
                    kalman_state_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS user_precip_checkins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    location_id INTEGER NOT NULL REFERENCES pinned_locations(id) ON DELETE CASCADE,
                    reported_at_utc TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    intensity_mm_hr REAL NOT NULL DEFAULT 0.0,
                    notes TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_checkins_loc_time
                    ON user_precip_checkins(location_id, reported_at_utc DESC);

                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    user_agent TEXT NOT NULL DEFAULT '',
                    created_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watermarks (
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_utc TEXT NOT NULL,
                    PRIMARY KEY (scope, key)
                );
                """
            )
            # Idempotent schema migrations for existing SQLite databases
            for alter_sql in (
                "ALTER TABLE pinned_locations ADD COLUMN address_label TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE pinned_locations ADD COLUMN alerts_enabled INTEGER NOT NULL DEFAULT 1",
            ):
                try:
                    await db.execute(alter_sql)
                except Exception:
                    pass
            await db.commit()

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    # ── Pinned Locations ──────────────────────────────────────────────────

    async def list_locations(self) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            async with conn.execute(
                "SELECT * FROM pinned_locations ORDER BY id ASC"
            ) as cur:
                rows = await cur.fetchall()
                return [self._decode_location(dict(r)) for r in rows]
        finally:
            await conn.close()

    async def get_location(self, location_id: int) -> dict[str, Any] | None:
        conn = await self._connect()
        try:
            async with conn.execute(
                "SELECT * FROM pinned_locations WHERE id = ?", (location_id,)
            ) as cur:
                row = await cur.fetchone()
                return self._decode_location(dict(row)) if row else None
        finally:
            await conn.close()

    async def create_location(
        self,
        *,
        name: str,
        admin1: str,
        country: str,
        lat: float,
        lon: float,
        elevation_m: float,
        timezone_name: str,
        stations: list[dict[str, Any]],
        address_label: str = "",
        alerts_enabled: bool = True,
    ) -> dict[str, Any]:
        conn = await self._connect()
        try:
            # Avoid duplicate pin within ~85 meters (0.0008 deg) so multiple addresses in the same city stay separate
            async with conn.execute("SELECT * FROM pinned_locations") as cur:
                existing = await cur.fetchall()
                for row in existing:
                    r = dict(row)
                    if abs(r["lat"] - lat) < 0.0008 and abs(r["lon"] - lon) < 0.0008:
                        if address_label and not r.get("address_label"):
                            await conn.execute(
                                "UPDATE pinned_locations SET name = ?, address_label = ? WHERE id = ?",
                                (name, address_label, r["id"]),
                            )
                            await conn.commit()
                            r["name"] = name
                            r["address_label"] = address_label
                        return self._decode_location(r)

            now = utc_now_iso()
            cur = await conn.execute(
                """
                INSERT INTO pinned_locations
                    (name, address_label, admin1, country, lat, lon, elevation_m, timezone,
                     stations_json, alerts_enabled, backfill_status, backfill_progress,
                     backfill_message, model_status, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0.0, 'Queued for Day-1 backfill', 'untrained', ?)
                """,
                (
                    name,
                    address_label,
                    admin1,
                    country,
                    lat,
                    lon,
                    elevation_m,
                    timezone_name,
                    json.dumps(stations),
                    1 if alerts_enabled else 0,
                    now,
                ),
            )
            loc_id = cur.lastrowid
            await conn.commit()
        finally:
            await conn.close()
        loc = await self.get_location(int(loc_id))  # type: ignore[arg-type]
        assert loc is not None
        return loc

    async def update_location_status(
        self,
        location_id: int,
        *,
        name: str | None = None,
        address_label: str | None = None,
        alerts_enabled: bool | None = None,
        backfill_status: str | None = None,
        backfill_progress: float | None = None,
        backfill_message: str | None = None,
        model_status: str | None = None,
        stations: list[dict[str, Any]] | None = None,
    ) -> None:
        fields: list[str] = []
        params: list[Any] = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if address_label is not None:
            fields.append("address_label = ?")
            params.append(address_label)
        if alerts_enabled is not None:
            fields.append("alerts_enabled = ?")
            params.append(1 if alerts_enabled else 0)
        if backfill_status is not None:
            fields.append("backfill_status = ?")
            params.append(backfill_status)
        if backfill_progress is not None:
            fields.append("backfill_progress = ?")
            params.append(float(backfill_progress))
        if backfill_message is not None:
            fields.append("backfill_message = ?")
            params.append(backfill_message)
        if model_status is not None:
            fields.append("model_status = ?")
            params.append(model_status)
        if stations is not None:
            fields.append("stations_json = ?")
            params.append(json.dumps(stations))
        if not fields:
            return
        params.append(location_id)
        conn = await self._connect()
        try:
            await conn.execute(
                f"UPDATE pinned_locations SET {', '.join(fields)} WHERE id = ?",
                tuple(params),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def delete_location(self, location_id: int) -> bool:
        conn = await self._connect()
        try:
            cur = await conn.execute(
                "DELETE FROM pinned_locations WHERE id = ?", (location_id,)
            )
            await conn.commit()
            return cur.rowcount > 0
        finally:
            await conn.close()

    @staticmethod
    def _decode_location(row: dict[str, Any]) -> dict[str, Any]:
        row["stations"] = json.loads(row.pop("stations_json", "[]"))
        row["address_label"] = str(row.get("address_label") or "")
        row["alerts_enabled"] = bool(row.get("alerts_enabled", 1))
        return row

    # ── Model Registry, Conformal ACI & Kalman State ──────────────────────

    async def get_model_record(self, location_id: int) -> dict[str, Any] | None:
        conn = await self._connect()
        try:
            async with conn.execute(
                "SELECT * FROM model_registry WHERE location_id = ?", (location_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                d = dict(row)
                d["metrics"] = json.loads(d.pop("metrics_json", "{}"))
                d["aci_state"] = json.loads(d.pop("aci_state_json", "{}"))
                d["kalman_state"] = json.loads(d.pop("kalman_state_json", "{}"))
                return d
        finally:
            await conn.close()

    async def upsert_model_record(
        self,
        location_id: int,
        *,
        version: int,
        train_rows: int,
        artifact_path: str,
        metrics: dict[str, Any],
        aci_state: dict[str, Any] | None = None,
        kalman_state: dict[str, Any] | None = None,
    ) -> None:
        existing = await self.get_model_record(location_id)
        aci = aci_state if aci_state is not None else (existing["aci_state"] if existing else {})
        kalman = (
            kalman_state
            if kalman_state is not None
            else (existing["kalman_state"] if existing else {})
        )
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO model_registry
                    (location_id, version, trained_at_utc, train_rows, artifact_path,
                     metrics_json, aci_state_json, kalman_state_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(location_id) DO UPDATE SET
                    version = excluded.version,
                    trained_at_utc = excluded.trained_at_utc,
                    train_rows = excluded.train_rows,
                    artifact_path = excluded.artifact_path,
                    metrics_json = excluded.metrics_json,
                    aci_state_json = excluded.aci_state_json,
                    kalman_state_json = excluded.kalman_state_json
                """,
                (
                    location_id,
                    version,
                    utc_now_iso(),
                    train_rows,
                    artifact_path,
                    json.dumps(metrics),
                    json.dumps(aci),
                    json.dumps(kalman),
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def update_online_states(
        self,
        location_id: int,
        *,
        aci_state: dict[str, Any] | None = None,
        kalman_state: dict[str, Any] | None = None,
    ) -> None:
        existing = await self.get_model_record(location_id)
        if existing is None:
            await self.upsert_model_record(
                location_id,
                version=0,
                train_rows=0,
                artifact_path="",
                metrics={},
                aci_state=aci_state or {},
                kalman_state=kalman_state or {},
            )
            return
        conn = await self._connect()
        try:
            if aci_state is not None:
                await conn.execute(
                    "UPDATE model_registry SET aci_state_json = ? WHERE location_id = ?",
                    (json.dumps(aci_state), location_id),
                )
            if kalman_state is not None:
                await conn.execute(
                    "UPDATE model_registry SET kalman_state_json = ? WHERE location_id = ?",
                    (json.dumps(kalman_state), location_id),
                )
            await conn.commit()
        finally:
            await conn.close()

    # ── 1-Tap Backyard Precipitation Check-Ins ────────────────────────────

    async def add_precip_checkin(
        self,
        location_id: int,
        condition: str,
        intensity_mm_hr: float,
        notes: str = "",
        reported_at_utc: str | None = None,
    ) -> dict[str, Any]:
        ts = reported_at_utc or utc_now_iso()
        conn = await self._connect()
        try:
            cur = await conn.execute(
                """
                INSERT INTO user_precip_checkins
                    (location_id, reported_at_utc, condition, intensity_mm_hr, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (location_id, ts, condition, intensity_mm_hr, notes),
            )
            cid = cur.lastrowid
            await conn.commit()
            return {
                "id": cid,
                "location_id": location_id,
                "reported_at_utc": ts,
                "condition": condition,
                "intensity_mm_hr": intensity_mm_hr,
                "notes": notes,
            }
        finally:
            await conn.close()

    async def list_precip_checkins(
        self, location_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            async with conn.execute(
                """
                SELECT * FROM user_precip_checkins
                WHERE location_id = ?
                ORDER BY reported_at_utc DESC
                LIMIT ?
                """,
                (location_id, limit),
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        finally:
            await conn.close()

    # ── Web Push Subscriptions & Settings ─────────────────────────────────

    async def save_push_subscription(
        self, endpoint: str, p256dh: str, auth: str, user_agent: str = ""
    ) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent, created_at_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(endpoint) DO UPDATE SET
                    p256dh = excluded.p256dh,
                    auth = excluded.auth,
                    user_agent = excluded.user_agent
                """,
                (endpoint, p256dh, auth, user_agent, utc_now_iso()),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def remove_push_subscription(self, endpoint: str) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
            )
            await conn.commit()
        finally:
            await conn.close()

    async def list_push_subscriptions(self) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            async with conn.execute("SELECT * FROM push_subscriptions") as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        finally:
            await conn.close()

    async def get_setting(self, key: str, default: dict[str, Any]) -> dict[str, Any]:
        conn = await self._connect()
        try:
            async with conn.execute(
                "SELECT value_json FROM app_settings WHERE key = ?", (key,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return dict(default)
                merged = dict(default)
                merged.update(json.loads(row["value_json"]))
                return merged
        finally:
            await conn.close()

    async def set_setting(self, key: str, value: dict[str, Any]) -> dict[str, Any]:
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO app_settings (key, value_json, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (key, json.dumps(value), utc_now_iso()),
            )
            await conn.commit()
            return value
        finally:
            await conn.close()

    async def get_notification_prefs(self) -> dict[str, Any]:
        return await self.get_setting("notification_prefs", DEFAULT_NOTIFICATION_PREFS)

    async def set_notification_prefs(self, prefs: dict[str, Any]) -> dict[str, Any]:
        merged = await self.get_notification_prefs()
        merged.update(prefs)
        return await self.set_setting("notification_prefs", merged)

    async def get_pws_config(self) -> dict[str, Any]:
        return await self.get_setting("pws_config", DEFAULT_PWS_CONFIG)

    async def set_pws_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        merged = await self.get_pws_config()
        merged.update(cfg)
        return await self.set_setting("pws_config", merged)

    async def get_watermark(self, scope: str, key: str) -> str | None:
        conn = await self._connect()
        try:
            async with conn.execute(
                "SELECT value_utc FROM watermarks WHERE scope = ? AND key = ?",
                (scope, key),
            ) as cur:
                row = await cur.fetchone()
                return str(row["value_utc"]) if row else None
        finally:
            await conn.close()

    async def set_watermark(self, scope: str, key: str, value_utc: str) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO watermarks (scope, key, value_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(scope, key) DO UPDATE SET value_utc = excluded.value_utc
                """,
                (scope, key, value_utc),
            )
            await conn.commit()
        finally:
            await conn.close()
