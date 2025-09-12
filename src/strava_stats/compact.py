# src/strava_stats/compact.py
from __future__ import annotations

import glob
from pathlib import Path
import duckdb

DATA = Path("data")
DB   = DATA / "warehouse" / "strava.duckdb"
PARQUET_GLOB = str((DATA / "activities" / "activities_*.parquet").resolve())

def main() -> None:
    if not glob.glob(PARQUET_GLOB):
        print("No Parquet shards found in data/activities; nothing to compact.")
        return

    con = duckdb.connect(str(DB))

    # Detect columns present across shards (union_by_name to stabilise schema)
    cols_df = con.execute(
        "SELECT * FROM read_parquet(?, union_by_name=true) LIMIT 0", [PARQUET_GLOB]
    ).fetchdf()
    has_ingestion_ts = "ingestion_ts" in cols_df.columns

    con.execute("CREATE SCHEMA IF NOT EXISTS gold;")

    ingestion_ts_expr = (
        "TRY_CAST(ingestion_ts AS TIMESTAMP) AS ingestion_ts"
        if has_ingestion_ts
        else "CAST(NULL AS TIMESTAMP) AS ingestion_ts"
    )

    create_gold_sql = f"""
    CREATE OR REPLACE TABLE gold.activities AS
    WITH raw AS (
      SELECT * FROM read_parquet('{PARQUET_GLOB}', union_by_name=true)
    ),
    typed AS (
      SELECT
        CAST(id AS BIGINT)                          AS id,
        name,
        sport_type,
        type,
        CAST(distance AS DOUBLE)                    AS distance,
        CAST(moving_time AS INTEGER)                AS moving_time,
        CAST(elapsed_time AS INTEGER)               AS elapsed_time,
        CAST(total_elevation_gain AS DOUBLE)        AS total_elevation_gain,
        TRY_CAST(start_date AS TIMESTAMP)           AS start_date,
        TRY_CAST(start_date_local AS TIMESTAMP)     AS start_date_local,
        timezone,
        utc_offset,
        achievement_count,
        kudos_count,
        average_speed,
        max_speed,
        average_heartrate,
        max_heartrate,
        suffer_score,
        commute,
        manual,
        visibility,
        gear_id,
        location_city,
        location_state,
        location_country,
        map_id,
        polyline,
        summary_polyline,
        {ingestion_ts_expr}
      FROM raw
    ),
    dedup AS (
      SELECT *,
             ROW_NUMBER() OVER (
               PARTITION BY id
               ORDER BY COALESCE(ingestion_ts, start_date) DESC NULLS LAST
             ) AS rn
      FROM typed
    )
    SELECT * EXCLUDE rn
    FROM dedup
    WHERE rn = 1;
    """

    con.execute(create_gold_sql)
    con.execute("CREATE OR REPLACE VIEW activities AS SELECT * FROM gold.activities;")
    con.execute("CHECKPOINT")
    con.close()
    print("Compaction complete. View 'activities' is ready (backed by gold.activities).")

if __name__ == "__main__":
    main()
