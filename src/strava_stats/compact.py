from __future__ import annotations
import os, time, shutil, glob
from pathlib import Path
import duckdb

BASE = Path("data")
BRONZE_GLOB = BASE / "activities" / "*.parquet"
SILVER_DIR = BASE / "silver" / "strava_activities"
WAREHOUSE_DB = BASE / "warehouse" / "strava.duckdb"

def _safe_rmtree(p: Path | None):
    if not p:
        return
    try:
        shutil.rmtree(p)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[warn] Failed to remove dir {p}: {e}")

def _sweep_orphan_staging_dirs(current_staging: Path | None = None):
    """
    Remove any lingering staging dirs that match the pattern
    {SILVER_DIR.name}__staging_* except the one we're actively using.
    """
    prefix = f"{SILVER_DIR.name}__staging_"
    for d in SILVER_DIR.parent.glob(prefix + "*"):
        if d.is_dir() and d != current_staging:
            _safe_rmtree(d)

def _columns_in_parquet(con: duckdb.DuckDBPyConnection, glob_pattern: str) -> set[str]:
    # SELECT can use a bound parameter – fine in DuckDB
    cols = con.execute(
        "SELECT * FROM read_parquet(?, filename=true) LIMIT 0", [glob_pattern]
    ).fetchdf().columns
    return set(map(str, cols))

def _sql_quote(path: str) -> str:
    # escape single quotes for SQL string literal
    return path.replace("'", "''")

def main():
    shards = glob.glob(str(BRONZE_GLOB))
    if not shards:
        print(f"No shards found at {BRONZE_GLOB}")
        # Clean up any old orphans even if we didn't run compaction
        _sweep_orphan_staging_dirs(None)
        return

    # Ensure directories exist (idempotent)
    SILVER_DIR.parent.mkdir(parents=True, exist_ok=True)
    WAREHOUSE_DB.parent.mkdir(parents=True, exist_ok=True)

    con = None
    staging: Path | None = None
    backup: Path | None = None

    try:
        con = duckdb.connect(str(WAREHOUSE_DB))
        con.execute(f"PRAGMA threads={os.cpu_count() or 4}")
        con.execute("PRAGMA enable_object_cache")

        # ---------- Load all shards (filename captured for tie-breaks) ----------
        bronze_glob = _sql_quote(str(BRONZE_GLOB))
        con.execute(f"""
            CREATE OR REPLACE TEMP VIEW _all AS
            SELECT *, filename AS source_file
            FROM read_parquet('{bronze_glob}', filename=true)
        """)

        # ---------- Introspect schema to choose keys & ordering ----------
        cols = _columns_in_parquet(con, str(BRONZE_GLOB))
        has = cols.__contains__

        # Dedupe key
        if has("athlete_id") and has("id"):
            dedupe_key = "athlete_id, id"
            partition_athlete = True
        elif has("id"):
            dedupe_key = "id"
            partition_athlete = False
        else:
            dedupe_key = "source_file"  # last resort
            partition_athlete = False
            print("[WARN] No 'id' column found; deduping by source_file only.")

        # “Best/latest” ordering
        order_parts: list[str] = []
        if has("updated_at"):
            order_parts.append(
                "COALESCE(try_cast(updated_at AS TIMESTAMP), TIMESTAMP '1970-01-01') DESC"
            )
        if has("resource_state"):
            order_parts.append("try_cast(resource_state AS INTEGER) DESC")
        if has("ingestion_ts"):
            order_parts.append(
                "COALESCE(try_cast(ingestion_ts AS TIMESTAMP), TIMESTAMP '1970-01-01') DESC"
            )
        for c in ("summary_polyline", "map_summary_polyline"):
            if has(c):
                order_parts.append(f"({c} IS NOT NULL) DESC")
                break
        order_parts.append("source_file DESC")  # final tie-break
        order_by = ",\n".join(order_parts)

        con.execute(f"""
            CREATE OR REPLACE TEMP VIEW _dedup AS
            WITH ranked AS (
              SELECT
                *,
                ROW_NUMBER() OVER (
                  PARTITION BY {dedupe_key}
                  ORDER BY {order_by}
                ) AS rn
              FROM _all
            )
            SELECT * EXCLUDE rn FROM ranked WHERE rn = 1
        """)

        # ---------- Write silver (Parquet) via staging + atomic swap ----------
        staging = SILVER_DIR.parent / f"{SILVER_DIR.name}__staging_{int(time.time())}"
        staging.mkdir(parents=True, exist_ok=True)

        if partition_athlete:
            # Directory output with PARTITION_BY is valid
            con.execute(f"""
                COPY (SELECT * FROM _dedup)
                TO '{staging.as_posix()}'
                (FORMAT PARQUET, PARTITION_BY (athlete_id), OVERWRITE_OR_IGNORE TRUE);
            """)
        else:
            # Without partitions, COPY needs a FILE path (not a directory)
            outfile = staging / "part-00000.parquet"
            con.execute(f"""
                COPY (SELECT * FROM _dedup)
                TO '{_sql_quote(outfile.as_posix())}'
                (FORMAT PARQUET, OVERWRITE_OR_IGNORE TRUE);
            """)

        # Atomic swap with rollback safety
        if SILVER_DIR.exists():
            backup = SILVER_DIR.parent / f"{SILVER_DIR.name}__backup_{int(time.time())}"
            SILVER_DIR.rename(backup)
            try:
                staging.rename(SILVER_DIR)
            except Exception as e:
                # Roll back: try to restore previous silver
                try:
                    if not SILVER_DIR.exists() and backup.exists():
                        backup.rename(SILVER_DIR)
                finally:
                    raise e
        else:
            staging.rename(SILVER_DIR)

        # ---------- Materialize into DuckDB for BI tools / stable querying ----------
        silver_glob = _sql_quote(str(SILVER_DIR / "**" / "*.parquet"))

        # Build ORDER BY for materialized table to enhance zone-map pruning
        order_clause = []
        if has("athlete_id"):
            order_clause.append("athlete_id")
        if has("start_date"):
            order_clause.append("try_cast(start_date AS TIMESTAMP)")
        elif has("start_date_local"):
            order_clause.append("try_cast(start_date_local AS TIMESTAMP)")
        if not order_clause:
            if has("id"):
                order_clause.append("id")
            else:
                order_clause.append("source_file")
        order_sql = ", ".join(order_clause)

        con.execute("""
            CREATE SCHEMA IF NOT EXISTS gold;
        """)
        # Note: DDL cannot use parameters; inline the glob literal
        con.execute(f"""
            CREATE OR REPLACE TABLE gold.strava_activities AS
            SELECT *
            FROM read_parquet('{silver_glob}', hive_partitioning=1)
            ORDER BY {order_sql};
        """)

        # Public stable name for consumers (no file paths involved)
        con.execute("""
            CREATE OR REPLACE VIEW strava_activities AS
            SELECT * FROM gold.strava_activities;
        """)

        # Optional stats to help the planner
        con.execute("ANALYZE gold.strava_activities;")

        print("Compaction complete. View 'strava_activities' is ready (backed by gold.strava_activities).")

    finally:
        # Close DB connection first
        try:
            if con is not None:
                con.close()
        except Exception as e:
            print(f"[warn] Failed to close DuckDB connection: {e}")

        # Always attempt to remove the current staging and any backups
        _safe_rmtree(staging)
        _safe_rmtree(backup)

        # Sweep any orphan staging dirs from previous failed runs
        _sweep_orphan_staging_dirs(current_staging=None)

if __name__ == "__main__":
    main()
