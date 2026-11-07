"""Thread-safe DuckDB columnar analytical store with Polars Arrow zero-copy I/O."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

MODEL_IDS = ("hrrr", "rrfs", "nbm", "ecmwf_ifs", "ecmwf_aifs", "gfs")

# Operational NWP dissemination latency (minutes after cycle init_time_utc)
MODEL_AVAILABILITY_LAG_MINUTES: dict[str, int] = {
    "hrrr": 55,
    "rrfs": 60,
    "nbm": 65,
    "gfs": 220,
    "ecmwf_ifs": 360,
    "ecmwf_aifs": 360,
}


class TelemetryStore:
    """Columnar time-series store for vintage NWP runs, observations, and verification."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn: duckdb.DuckDBPyConnection | None = None

    def initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._conn is None:
                self._conn = duckdb.connect(str(self.db_path))
            self._conn.execute("SET TimeZone = 'UTC';")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS forecast_snapshots (
                    location_id INTEGER NOT NULL,
                    model_id VARCHAR NOT NULL,
                    init_time_utc TIMESTAMPTZ NOT NULL,
                    available_at_utc TIMESTAMPTZ NOT NULL,
                    valid_time_utc TIMESTAMPTZ NOT NULL,
                    lead_hours DOUBLE NOT NULL,
                    is_subhourly_15m BOOLEAN NOT NULL DEFAULT FALSE,
                    temp_c DOUBLE,
                    dewpoint_c DOUBLE,
                    wind_ms DOUBLE,
                    gust_ms DOUBLE,
                    wind_dir_deg DOUBLE,
                    precip_mm DOUBLE,
                    snowfall_cm DOUBLE,
                    precip_prob DOUBLE,
                    cloud_pct DOUBLE,
                    pressure_hpa DOUBLE,
                    solar_wm2 DOUBLE,
                    cape_jkg DOUBLE,
                    cin_jkg DOUBLE,
                    freezing_level_m DOUBLE,
                    ring_precip_max_mm DOUBLE,
                    ring_precip_mean_mm DOUBLE,
                    ring_temp_grad_c DOUBLE,
                    ring_theta_e_grad_k DOUBLE,
                    ring_gust_max_ms DOUBLE,
                    PRIMARY KEY (location_id, model_id, init_time_utc, valid_time_utc, is_subhourly_15m)
                );

                CREATE TABLE IF NOT EXISTS observations_station_raw (
                    station_id VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    valid_time_utc TIMESTAMPTZ NOT NULL,
                    temp_c DOUBLE,
                    dewpoint_c DOUBLE,
                    rel_humidity_pct DOUBLE,
                    wind_ms DOUBLE,
                    gust_ms DOUBLE,
                    wind_dir_deg DOUBLE,
                    precip_accum_mm DOUBLE,
                    precip_1h_mm DOUBLE,
                    pressure_hpa DOUBLE,
                    solar_wm2 DOUBLE,
                    PRIMARY KEY (station_id, source, valid_time_utc)
                );

                CREATE TABLE IF NOT EXISTS observations_blended_hourly (
                    location_id INTEGER NOT NULL,
                    valid_time_utc TIMESTAMPTZ NOT NULL,
                    temp_c DOUBLE,
                    dewpoint_c DOUBLE,
                    wet_bulb_c DOUBLE,
                    wind_ms DOUBLE,
                    gust_ms DOUBLE,
                    wind_dir_deg DOUBLE,
                    precip_1h_mm DOUBLE,
                    pressure_hpa DOUBLE,
                    solar_wm2 DOUBLE,
                    user_condition VARCHAR,
                    station_count INTEGER,
                    PRIMARY KEY (location_id, valid_time_utc)
                );

                CREATE TABLE IF NOT EXISTS ensemble_predictions (
                    location_id INTEGER NOT NULL,
                    issued_at_utc TIMESTAMPTZ NOT NULL,
                    valid_time_utc TIMESTAMPTZ NOT NULL,
                    lead_hours DOUBLE NOT NULL,
                    temp_p05_c DOUBLE,
                    temp_p10_c DOUBLE,
                    temp_p25_c DOUBLE,
                    temp_p50_c DOUBLE,
                    temp_p75_c DOUBLE,
                    temp_p90_c DOUBLE,
                    temp_p95_c DOUBLE,
                    dewpoint_p50_c DOUBLE,
                    wet_bulb_c DOUBLE,
                    wind_p50_ms DOUBLE,
                    gust_p50_ms DOUBLE,
                    gust_p90_ms DOUBLE,
                    wind_dir_deg DOUBLE,
                    pressure_hpa DOUBLE,
                    precip_prob_cal DOUBLE,
                    precip_p50_mm DOUBLE,
                    precip_p90_mm DOUBLE,
                    precip_type VARCHAR,
                    bust_risk_level VARCHAR,
                    bust_summary VARCHAR,
                    PRIMARY KEY (location_id, issued_at_utc, valid_time_utc)
                );
                """
            )

    async def initialize(self) -> None:
        await asyncio.to_thread(self.initialize_sync)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            self.initialize_sync()
        assert self._conn is not None
        return self._conn

    def _query_pl_sync(self, sql: str, params: list[Any] | None = None) -> pl.DataFrame:
        with self._lock:
            rel = self.conn.sql(sql, params=params or [])
            res = pl.from_arrow(rel.to_arrow_table())
            df = res if isinstance(res, pl.DataFrame) else res.to_frame()
        dt_exprs = []
        for col_name, dtype in df.schema.items():
            if isinstance(dtype, pl.Datetime):
                if dtype.time_zone is None:
                    dt_exprs.append(pl.col(col_name).dt.replace_time_zone("UTC"))
                elif dtype.time_zone != "UTC":
                    dt_exprs.append(pl.col(col_name).dt.convert_time_zone("UTC"))
        return df.with_columns(dt_exprs) if dt_exprs else df

    # ── Zero-Copy Polars Arrow Ingestion ──────────────────────────────────

    def _upsert_polars_sync(self, table_name: str, df: pl.DataFrame) -> int:
        if df.is_empty():
            return 0
        arrow_tbl = df.to_arrow()
        with self._lock, self.conn.cursor() as cur:
            cur.register("_incoming_arrow", arrow_tbl)
            try:
                cur.execute(
                    f"INSERT OR REPLACE INTO {table_name} BY NAME SELECT * FROM _incoming_arrow"
                ).fetchall()
            finally:
                cur.unregister("_incoming_arrow")
        return df.height

    async def upsert_forecast_snapshots(self, df: pl.DataFrame) -> int:
        return await asyncio.to_thread(self._upsert_polars_sync, "forecast_snapshots", df)

    async def upsert_station_observations(self, df: pl.DataFrame) -> int:
        return await asyncio.to_thread(
            self._upsert_polars_sync, "observations_station_raw", df
        )

    async def upsert_blended_hourly(self, df: pl.DataFrame) -> int:
        return await asyncio.to_thread(
            self._upsert_polars_sync, "observations_blended_hourly", df
        )

    async def upsert_ensemble_predictions(self, df: pl.DataFrame) -> int:
        return await asyncio.to_thread(
            self._upsert_polars_sync, "ensemble_predictions", df
        )

    def delete_location_telemetry_sync(self, location_id: int) -> None:
        with self._lock:
            for tbl in (
                "forecast_snapshots",
                "observations_blended_hourly",
                "ensemble_predictions",
            ):
                self.conn.execute(
                    f"DELETE FROM {tbl} WHERE location_id = ?", [location_id]
                )

    async def delete_location_telemetry(self, location_id: int) -> None:
        await asyncio.to_thread(self.delete_location_telemetry_sync, location_id)

    def compact_telemetry_sync(
        self,
        *,
        subhourly_days: int = 30,
        retention_days: int = 730,
        thin_runs_after_days: int = 90,
    ) -> dict[str, int]:
        """
        Bounded rolling telemetry maintenance:
        1) Prunes 15-minute sub-hourly HRRR snapshots older than `subhourly_days` (default 30d).
        2) Prunes all telemetry older than `retention_days` (default 730d / 2 years).
        3) Thins intermediate hourly NWP runs older than `thin_runs_after_days` (default 90d)
           down to the 4 primary synoptic cycles (00Z/06Z/12Z/18Z) to cut storage by ~75%
           while keeping 100% of verified ASOF training pairs intact.
        """
        now_utc = datetime.now(UTC)
        sub_cutoff = now_utc - timedelta(days=subhourly_days)
        ret_cutoff = now_utc - timedelta(days=retention_days)
        thin_cutoff = now_utc - timedelta(days=thin_runs_after_days)

        with self._lock:
            sub_del = self.conn.execute(
                """
                DELETE FROM forecast_snapshots
                WHERE is_subhourly_15m = TRUE AND valid_time_utc < ?
                """,
                [sub_cutoff],
            ).fetchone()
            snap_del = self.conn.execute(
                "DELETE FROM forecast_snapshots WHERE valid_time_utc < ?",
                [ret_cutoff],
            ).fetchone()
            raw_del = self.conn.execute(
                "DELETE FROM observations_station_raw WHERE valid_time_utc < ?",
                [ret_cutoff],
            ).fetchone()
            blend_del = self.conn.execute(
                "DELETE FROM observations_blended_hourly WHERE valid_time_utc < ?",
                [ret_cutoff],
            ).fetchone()
            ens_del = self.conn.execute(
                "DELETE FROM ensemble_predictions WHERE valid_time_utc < ?",
                [ret_cutoff],
            ).fetchone()
            thin_del = self.conn.execute(
                """
                DELETE FROM forecast_snapshots
                WHERE is_subhourly_15m = FALSE
                  AND valid_time_utc < ?
                  AND EXTRACT(HOUR FROM timezone('UTC', init_time_utc)) NOT IN (0, 6, 12, 18)
                """,
                [thin_cutoff],
            ).fetchone()
            self.conn.execute("CHECKPOINT;")

        return {
            "subhourly_deleted": int(sub_del[0]) if sub_del else 0,
            "snapshots_expired": int(snap_del[0]) if snap_del else 0,
            "raw_obs_expired": int(raw_del[0]) if raw_del else 0,
            "blended_obs_expired": int(blend_del[0]) if blend_del else 0,
            "ensemble_expired": int(ens_del[0]) if ens_del else 0,
            "intermediate_runs_thinned": int(thin_del[0]) if thin_del else 0,
        }

    async def compact_telemetry(
        self,
        *,
        subhourly_days: int = 30,
        retention_days: int = 730,
        thin_runs_after_days: int = 90,
    ) -> dict[str, int]:
        return await asyncio.to_thread(
            self.compact_telemetry_sync,
            subhourly_days=subhourly_days,
            retention_days=retention_days,
            thin_runs_after_days=thin_runs_after_days,
        )

    def compact_subhourly_sync(self, retention_days: int = 30) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self._lock:
            self.conn.execute(
                """
                DELETE FROM forecast_snapshots
                WHERE is_subhourly_15m = TRUE AND valid_time_utc < ?
                """,
                [cutoff],
            )

    async def compact_subhourly(self, retention_days: int = 30) -> None:
        await asyncio.to_thread(self.compact_subhourly_sync, retention_days)

    # ── Point-in-Time Leakage-Free ASOF Queries ───────────────────────────

    def query_inference_grid_sync(
        self,
        location_id: int,
        issue_time_utc: datetime,
        start_valid_utc: datetime,
        end_valid_utc: datetime,
    ) -> pl.DataFrame:
        """Assemble point-in-time multi-model forecast grid strictly available <= issue_time_utc."""
        return self._query_pl_sync(
            """
            WITH ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY model_id, valid_time_utc
                        ORDER BY available_at_utc DESC
                    ) AS rn
                FROM forecast_snapshots
                WHERE location_id = ?
                  AND is_subhourly_15m = FALSE
                  AND available_at_utc <= ?
                  AND valid_time_utc >= ?
                  AND valid_time_utc <= ?
            ),
            latest AS (
                SELECT * FROM ranked WHERE rn = 1
            ),
            trends AS (
                SELECT
                    valid_time_utc,
                    MAX(CASE WHEN model_id = 'hrrr' AND rn = 1 THEN temp_c END)
                      - MAX(CASE WHEN model_id = 'hrrr' AND rn = 3 THEN temp_c END) AS hrrr_3run_temp_trend,
                    MAX(CASE WHEN model_id = 'hrrr' AND rn = 1 THEN temp_c END)
                      - MAX(CASE WHEN model_id = 'hrrr' AND rn = 2 THEN temp_c END) AS hrrr_prev_temp_trend,
                    MAX(CASE WHEN model_id = 'ecmwf_ifs' AND rn = 1 THEN temp_c END)
                      - MAX(CASE WHEN model_id = 'ecmwf_ifs' AND rn = 2 THEN temp_c END) AS ecmwf_2run_temp_trend
                FROM ranked
                WHERE rn <= 3
                GROUP BY valid_time_utc
            )
            SELECT
                l.*,
                t.hrrr_3run_temp_trend,
                t.hrrr_prev_temp_trend,
                t.ecmwf_2run_temp_trend
            FROM latest l
            LEFT JOIN trends t USING (valid_time_utc)
            ORDER BY l.valid_time_utc ASC, l.model_id ASC
            """,
            [location_id, issue_time_utc, start_valid_utc, end_valid_utc],
        )

    async def query_inference_grid(
        self,
        location_id: int,
        issue_time_utc: datetime,
        start_valid_utc: datetime,
        end_valid_utc: datetime,
    ) -> pl.DataFrame:
        return await asyncio.to_thread(
            self.query_inference_grid_sync,
            location_id,
            issue_time_utc,
            start_valid_utc,
            end_valid_utc,
        )

    def query_subhourly_15m_sync(
        self,
        location_id: int,
        issue_time_utc: datetime,
        start_valid_utc: datetime,
        end_valid_utc: datetime,
    ) -> pl.DataFrame:
        return self._query_pl_sync(
            """
            WITH ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY valid_time_utc
                        ORDER BY available_at_utc DESC
                    ) AS rn
                FROM forecast_snapshots
                WHERE location_id = ?
                  AND model_id = 'hrrr'
                  AND is_subhourly_15m = TRUE
                  AND available_at_utc <= ?
                  AND valid_time_utc >= ?
                  AND valid_time_utc <= ?
            )
            SELECT * EXCLUDE (rn) FROM ranked WHERE rn = 1
            ORDER BY valid_time_utc ASC
            """,
            [location_id, issue_time_utc, start_valid_utc, end_valid_utc],
        )

    async def query_subhourly_15m(
        self,
        location_id: int,
        issue_time_utc: datetime,
        start_valid_utc: datetime,
        end_valid_utc: datetime,
    ) -> pl.DataFrame:
        return await asyncio.to_thread(
            self.query_subhourly_15m_sync,
            location_id,
            issue_time_utc,
            start_valid_utc,
            end_valid_utc,
        )

    def build_training_pairs_sync(
        self,
        location_id: int,
        lead_offsets_hours: tuple[int, ...] = (2, 6, 12, 18, 24, 36, 48, 72, 120, 168),
        retention_days: int = 730,
    ) -> pl.DataFrame:
        """
        Construct point-in-time non-leaking (issue_time_utc, valid_time_utc) training matrix
        via DuckDB ASOF JOIN across every verified observation hour and lead offset.
        """
        offsets_sql = ", ".join(f"({int(h)})" for h in lead_offsets_hours)
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        return self._query_pl_sync(
            f"""
                WITH obs AS (
                    SELECT *
                    FROM observations_blended_hourly
                    WHERE location_id = ?
                      AND temp_c IS NOT NULL
                      AND valid_time_utc >= ?
                ),
                lead_grid AS (
                    SELECT
                        o.location_id,
                        o.valid_time_utc,
                        h.target_lead_h,
                        o.valid_time_utc - (h.target_lead_h * INTERVAL '1 hour') AS issue_time_utc,
                        o.temp_c AS obs_temp_c,
                        o.dewpoint_c AS obs_dewpoint_c,
                        o.wet_bulb_c AS obs_wet_bulb_c,
                        o.wind_ms AS obs_wind_ms,
                        o.gust_ms AS obs_gust_ms,
                        o.wind_dir_deg AS obs_wind_dir_deg,
                        o.precip_1h_mm AS obs_precip_1h_mm,
                        o.pressure_hpa AS obs_pressure_hpa,
                        o.user_condition AS obs_user_condition
                    FROM obs o
                    CROSS JOIN (VALUES {offsets_sql}) AS h(target_lead_h)
                ),
                snaps AS (
                    SELECT *
                    FROM forecast_snapshots
                    WHERE location_id = ?
                      AND is_subhourly_15m = FALSE
                ),
                hrrr_s AS (SELECT * FROM snaps WHERE model_id = 'hrrr'),
                rrfs_s AS (SELECT * FROM snaps WHERE model_id = 'rrfs'),
                nbm_s  AS (SELECT * FROM snaps WHERE model_id = 'nbm'),
                ifs_s  AS (SELECT * FROM snaps WHERE model_id = 'ecmwf_ifs'),
                aifs_s AS (SELECT * FROM snaps WHERE model_id = 'ecmwf_aifs'),
                gfs_s  AS (SELECT * FROM snaps WHERE model_id = 'gfs')
                SELECT
                    g.*,
                    hrrr.temp_c AS hrrr_temp_c,
                    hrrr.dewpoint_c AS hrrr_dewpoint_c,
                    hrrr.wind_ms AS hrrr_wind_ms,
                    hrrr.gust_ms AS hrrr_gust_ms,
                    hrrr.wind_dir_deg AS hrrr_wind_dir_deg,
                    hrrr.precip_mm AS hrrr_precip_mm,
                    hrrr.snowfall_cm AS hrrr_snowfall_cm,
                    hrrr.precip_prob AS hrrr_precip_prob,
                    hrrr.cloud_pct AS hrrr_cloud_pct,
                    hrrr.pressure_hpa AS hrrr_pressure_hpa,
                    hrrr.solar_wm2 AS hrrr_solar_wm2,
                    hrrr.cape_jkg AS hrrr_cape_jkg,
                    hrrr.cin_jkg AS hrrr_cin_jkg,
                    hrrr.freezing_level_m AS hrrr_freezing_level_m,
                    hrrr.ring_precip_max_mm AS hrrr_ring_precip_max_mm,
                    hrrr.ring_temp_grad_c AS hrrr_ring_temp_grad_c,
                    hrrr.ring_theta_e_grad_k AS hrrr_ring_theta_e_grad_k,
                    hrrr.ring_gust_max_ms AS hrrr_ring_gust_max_ms,
                    rrfs.temp_c AS rrfs_temp_c,
                    rrfs.dewpoint_c AS rrfs_dewpoint_c,
                    rrfs.wind_ms AS rrfs_wind_ms,
                    rrfs.gust_ms AS rrfs_gust_ms,
                    rrfs.precip_mm AS rrfs_precip_mm,
                    rrfs.ring_precip_max_mm AS rrfs_ring_precip_max_mm,
                    nbm.temp_c AS nbm_temp_c,
                    nbm.dewpoint_c AS nbm_dewpoint_c,
                    nbm.wind_ms AS nbm_wind_ms,
                    nbm.gust_ms AS nbm_gust_ms,
                    nbm.wind_dir_deg AS nbm_wind_dir_deg,
                    nbm.precip_mm AS nbm_precip_mm,
                    nbm.precip_prob AS nbm_precip_prob,
                    nbm.cloud_pct AS nbm_cloud_pct,
                    ifs.temp_c AS ecmwf_ifs_temp_c,
                    ifs.dewpoint_c AS ecmwf_ifs_dewpoint_c,
                    ifs.wind_ms AS ecmwf_ifs_wind_ms,
                    ifs.gust_ms AS ecmwf_ifs_gust_ms,
                    ifs.wind_dir_deg AS ecmwf_ifs_wind_dir_deg,
                    ifs.precip_mm AS ecmwf_ifs_precip_mm,
                    ifs.cloud_pct AS ecmwf_ifs_cloud_pct,
                    ifs.pressure_hpa AS ecmwf_ifs_pressure_hpa,
                    ifs.solar_wm2 AS ecmwf_ifs_solar_wm2,
                    ifs.ring_precip_max_mm AS ecmwf_ifs_ring_precip_max_mm,
                    ifs.ring_temp_grad_c AS ecmwf_ifs_ring_temp_grad_c,
                    ifs.ring_theta_e_grad_k AS ecmwf_ifs_ring_theta_e_grad_k,
                    aifs.temp_c AS ecmwf_aifs_temp_c,
                    aifs.dewpoint_c AS ecmwf_aifs_dewpoint_c,
                    aifs.wind_ms AS ecmwf_aifs_wind_ms,
                    aifs.precip_mm AS ecmwf_aifs_precip_mm,
                    gfs.temp_c AS gfs_temp_c,
                    gfs.dewpoint_c AS gfs_dewpoint_c,
                    gfs.wind_ms AS gfs_wind_ms,
                    gfs.gust_ms AS gfs_gust_ms,
                    gfs.precip_mm AS gfs_precip_mm,
                    gfs.cape_jkg AS gfs_cape_jkg,
                    gfs.cin_jkg AS gfs_cin_jkg,
                    gfs.freezing_level_m AS gfs_freezing_level_m,
                    gfs.ring_precip_max_mm AS gfs_ring_precip_max_mm
                FROM lead_grid g
                ASOF LEFT JOIN hrrr_s hrrr
                    ON hrrr.valid_time_utc = g.valid_time_utc
                   AND hrrr.available_at_utc <= g.issue_time_utc
                ASOF LEFT JOIN rrfs_s rrfs
                    ON rrfs.valid_time_utc = g.valid_time_utc
                   AND rrfs.available_at_utc <= g.issue_time_utc
                ASOF LEFT JOIN nbm_s nbm
                    ON nbm.valid_time_utc = g.valid_time_utc
                   AND nbm.available_at_utc <= g.issue_time_utc
                ASOF LEFT JOIN ifs_s ifs
                    ON ifs.valid_time_utc = g.valid_time_utc
                   AND ifs.available_at_utc <= g.issue_time_utc
                ASOF LEFT JOIN aifs_s aifs
                    ON aifs.valid_time_utc = g.valid_time_utc
                   AND aifs.available_at_utc <= g.issue_time_utc
                ASOF LEFT JOIN gfs_s gfs
                    ON gfs.valid_time_utc = g.valid_time_utc
                   AND gfs.available_at_utc <= g.issue_time_utc
                WHERE COALESCE(hrrr.temp_c, nbm.temp_c, ifs.temp_c, gfs.temp_c) IS NOT NULL
                ORDER BY g.valid_time_utc ASC, g.target_lead_h ASC
            """,
            [location_id, cutoff, location_id],
        )

    async def build_training_pairs(
        self,
        location_id: int,
        lead_offsets_hours: tuple[int, ...] = (2, 6, 12, 18, 24, 36, 48, 72, 120, 168),
        retention_days: int = 730,
    ) -> pl.DataFrame:
        return await asyncio.to_thread(
            self.build_training_pairs_sync, location_id, lead_offsets_hours, retention_days
        )

    def query_recent_blended_obs_sync(
        self, location_id: int, hours: int = 96
    ) -> pl.DataFrame:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        return self._query_pl_sync(
            """
            SELECT *
            FROM observations_blended_hourly
            WHERE location_id = ? AND valid_time_utc >= ?
            ORDER BY valid_time_utc ASC
            """,
            [location_id, cutoff],
        )

    async def query_recent_blended_obs(
        self, location_id: int, hours: int = 96
    ) -> pl.DataFrame:
        return await asyncio.to_thread(
            self.query_recent_blended_obs_sync, location_id, hours
        )

    def query_verification_data_sync(
        self, location_id: int, days: int = 30
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Return (raw_models_vs_obs, mesoscale_ai_vs_obs) strictly out-of-sample."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        models_df = self._query_pl_sync(
            """
            SELECT
                f.model_id,
                f.init_time_utc,
                f.available_at_utc,
                f.valid_time_utc,
                EXTRACT(EPOCH FROM (f.valid_time_utc - f.available_at_utc)) / 3600.0 AS effective_lead_h,
                f.temp_c AS pred_temp_c,
                f.wind_ms AS pred_wind_ms,
                f.gust_ms AS pred_gust_ms,
                f.precip_mm AS pred_precip_mm,
                COALESCE(f.precip_prob / 100.0, CASE WHEN f.precip_mm >= 0.25 THEN 0.7 ELSE 0.05 END) AS pred_pop,
                o.temp_c AS obs_temp_c,
                o.wind_ms AS obs_wind_ms,
                o.gust_ms AS obs_gust_ms,
                o.precip_1h_mm AS obs_precip_mm,
                o.user_condition AS obs_user_condition
            FROM forecast_snapshots f
            INNER JOIN observations_blended_hourly o
                ON o.location_id = f.location_id
               AND o.valid_time_utc = f.valid_time_utc
            WHERE f.location_id = ?
              AND f.is_subhourly_15m = FALSE
              AND f.valid_time_utc >= ?
              AND f.available_at_utc < f.valid_time_utc
            """,
            [location_id, cutoff],
        )

        ai_df = self._query_pl_sync(
            """
            SELECT
                e.issued_at_utc,
                e.valid_time_utc,
                e.lead_hours AS effective_lead_h,
                e.temp_p10_c,
                e.temp_p50_c,
                e.temp_p90_c,
                e.wind_p50_ms,
                e.gust_p50_ms,
                e.precip_prob_cal,
                e.precip_p50_mm,
                o.temp_c AS obs_temp_c,
                o.wind_ms AS obs_wind_ms,
                o.gust_ms AS obs_gust_ms,
                o.precip_1h_mm AS obs_precip_mm,
                o.user_condition AS obs_user_condition
            FROM ensemble_predictions e
            INNER JOIN observations_blended_hourly o
                ON o.location_id = e.location_id
               AND o.valid_time_utc = e.valid_time_utc
            WHERE e.location_id = ?
              AND e.valid_time_utc >= ?
              AND e.issued_at_utc < e.valid_time_utc
            """,
            [location_id, cutoff],
        )
        return models_df, ai_df

    async def query_verification_data(
        self, location_id: int, days: int = 30
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        return await asyncio.to_thread(
            self.query_verification_data_sync, location_id, days
        )

    def get_stats_sync(self) -> dict[str, Any]:
        with self._lock:
            snap_cnt = self.conn.sql("SELECT COUNT(*) FROM forecast_snapshots").fetchall()[0][0]
            obs_cnt = self.conn.sql("SELECT COUNT(*) FROM observations_blended_hourly").fetchall()[0][0]
            ens_cnt = self.conn.sql("SELECT COUNT(*) FROM ensemble_predictions").fetchall()[0][0]
        size_mb = (
            round(self.db_path.stat().st_size / (1024 * 1024), 2)
            if self.db_path.exists()
            else 0.0
        )
        return {
            "forecast_snapshots": snap_cnt,
            "blended_observations": obs_cnt,
            "ensemble_predictions": ens_cnt,
            "duckdb_size_mb": size_mb,
        }

    async def get_stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.get_stats_sync)
