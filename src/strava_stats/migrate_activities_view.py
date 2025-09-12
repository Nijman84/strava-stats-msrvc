from __future__ import annotations
import duckdb
from pathlib import Path

DB = Path("data/warehouse/strava.duckdb")

def main() -> None:
    con = duckdb.connect(str(DB))
    # Create or refresh the compatibility alias:
    con.execute("CREATE OR REPLACE VIEW activities AS SELECT * FROM strava_activities")
    print("Created view: activities → strava_activities")
    con.close()

if __name__ == "__main__":
    main()
