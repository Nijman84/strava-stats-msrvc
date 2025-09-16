# dags/strava_stats_msrvc.example.py
from __future__ import annotations
from datetime import timedelta
from airflow import DAG
from airflow.utils.dates import days_ago
from airflow.operators.bash import BashOperator

# Configure this Airflow Variable in the UI or env:
# key: strava_repo_dir, value: /path/to/strava-stats-msrvc
try:
    from airflow.models import Variable
    REPO = Variable.get("strava_repo_dir")
except Exception:
    # Fallback for local illustrative runs
    REPO = "/opt/airflow/strava-stats-msrvc"

default_args = {
    "owner": "data-eng",
    "retries": 1,
    "retry_delay": timedelta(minutes=10),
}

with DAG(
    dag_id="strava_stats_msrvc_flow",
    default_args=default_args,
    description="Run strava-stats-msrvc daily flow with logging wrapper",
    schedule_interval="0 5 * * *",  # 05:00 daily
    start_date=days_ago(1),
    catchup=False,
    max_active_runs=1,
    tags=["strava", "duckdb", "pipeline"],
) as dag:

    run_flow = BashOperator(
        task_id="run_flow",
        bash_command=f"cd {REPO} && ./scripts/flow.sh",
        env={
            # Populate any secrets/ENV your Makefile expects here if needed
            # "STRAVA_CLIENT_ID": "{{ var.value.strava_client_id }}",
            # "STRAVA_CLIENT_SECRET": "{{ var.value.strava_client_secret }}",
        },
    )

    # If you later split the Makefile into steps (pull/compact/warehouse),
    # you can chain multiple BashOperators here. For now, the single flow is enough.
    run_flow
