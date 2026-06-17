# API → CSV → Power BI, orchestrated (Prefect demo, with Airflow comparison)

> **This whole folder is a throwaway learning example.** It does not touch the
> Django app, the database, or any existing code. Delete `prefect_airflow_demo/`
> whenever you're done and nothing else breaks.

It answers one practical question:

> *"I have an API. I want its data in Power BI as a CSV, refreshed automatically.
> Where does an orchestrator (Prefect / Airflow) fit, and what's the simplest
> architecture to understand it?"*

---

## 1. The simplest architecture that works

```
   ┌──────────────┐      ┌───────────────────────────┐      ┌──────────────┐      ┌────────────┐
   │   HTTP API   │ ───► │  Orchestrator (Prefect)    │ ───► │  CSV file    │ ───► │  Power BI  │
   │ brand-margins│      │  fetch → transform → write │      │ latest.csv   │      │  (refresh) │
   └──────────────┘      └───────────────────────────┘      └──────────────┘      └────────────┘
                              ▲  runs on a schedule              folder a.k.a.        scheduled
                              │  (every 30 min), retries,        "powerbi_inbox"      refresh /
                              │  logs, alerts on failure                              gateway
```

Three moving parts:

1. **The pipeline code** — three small functions: *fetch the API → reshape the
   JSON into rows → write a CSV*. This is plain Python; it would work even
   without an orchestrator.
2. **The orchestrator** — Prefect (or Airflow). It does **not** do the work; it
   *runs the work reliably*: on a schedule, with retries, logging, a UI showing
   every run, and alerts when something fails. That's the entire reason to add
   it instead of a bare cron job.
3. **Power BI** — points at the CSV file/folder and refreshes on its own
   schedule. It never talks to the API directly; it only reads the CSV the
   orchestrator keeps fresh.

The orchestrator writes two files each run:
- `brand_margins_<timestamp>.csv` — an audit trail / history.
- `latest.csv` — a **stable filename** Power BI can bind to once and forget.

---

## 2. Why a CSV in the middle (and not API → Power BI directly)?

Power BI *can* call a web API directly, but a CSV "drop folder" is the simplest,
most robust pattern for learning and for messy/auth'd APIs:

- **Decoupling** — if the API is slow or down, Power BI still has the last good
  CSV. The orchestrator's retries hide transient API failures.
- **One place for the messy bits** — auth headers, pagination, JSON flattening,
  type cleanup all live in the pipeline, not in fragile Power Query M.
- **Auditability** — timestamped CSVs give you history "for free".
- **Power BI on-prem gateway** reads files/SharePoint trivially; it does not need
  network access to your internal API.

When you outgrow CSV you swap `write_csv` for `write_to_database` (e.g. load a
Postgres/SQL table) and point Power BI at that. The orchestration doesn't change.

---

## 3. Run the Prefect version

```bash
cd prefect_airflow_demo
python -m venv .venv && .venv\Scripts\activate     # Windows (use source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt

# (a) One-off run, no server. Writes powerbi_inbox/latest.csv and prints its path.
python prefect_flow.py

# (b) Scheduled, with the UI:
prefect server start            # terminal 1 -> open http://127.0.0.1:4200
python prefect_flow.py --serve  # terminal 2 -> runs every 30 min, visible in the UI
```

Then in **Power BI Desktop**: *Get Data → Text/CSV* → pick
`prefect_airflow_demo/powerbi_inbox/latest.csv` → Load. Publish, and set a
**Scheduled refresh** (via an on-prem data gateway if the file lives on a server).

---

## 4. The same pipeline in Airflow — what changes

`airflow_dag.py` is the identical fetch→transform→write logic wrapped for
Airflow. **Only the orchestration layer differs**, not the business logic. Drop
the file into your Airflow `dags/` folder and it appears in the UI.

| Concept | **Prefect** (`prefect_flow.py`) | **Airflow** (`airflow_dag.py`) |
|---|---|---|
| Define work | `@task` on a normal function | `@task` inside an `@dag` (or an Operator) |
| Define pipeline | `@flow` function; call tasks like normal Python | `@dag` function; build a DAG graph |
| Pass data between steps | Return values (just Python objects) | **XCom** (auto for `@task`; small values only) |
| Order of steps | Implicit from call order | Explicit: pass outputs, or `a >> b >> c` |
| Schedule | `interval=timedelta(minutes=30)` or a cron deployment | `schedule="*/30 * * * *"` on the DAG |
| Retries | `@task(retries=3, retry_delay_seconds=10)` | `default_args={"retries":3, "retry_delay":...}` |
| Backfill of missed runs | Off by default | `catchup` (here set `False`) — defaults to backfilling! |
| What you must run | Just Python for a one-off; `prefect server` for UI/schedules | A **metadata DB + scheduler + webserver** must be running |
| Mental model | "Decorated Python functions" — feels like normal code | "A DAG of tasks the scheduler executes" — more framework |
| Setup weight | `pip install prefect`, run a script | Install with a constraints file, init DB, start services |
| Dynamic logic (loops/conditionals at runtime) | Native Python — trivial | Possible (dynamic task mapping) but more ceremony |

### Running the Airflow version (heavier)

Airflow shouldn't be `pip install`-ed bare; use the official constraints file:

```bash
export AIRFLOW_VERSION=2.9.3
export PYTHON_VERSION=3.11
pip install "apache-airflow==${AIRFLOW_VERSION}" \
  --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

airflow standalone           # creates the DB + starts scheduler + webserver (http://localhost:8080)
# copy airflow_dag.py into ~/airflow/dags/  then enable the DAG in the UI
```

(For a real deployment you'd run scheduler/webserver as services, often via the
official Docker Compose. That operational overhead is the main cost of Airflow.)

---

## 5. How to choose (the short version)

- **Prefect** — best when you want the lightest path from "Python script" to
  "scheduled, retried, observable pipeline". Great for this CSV-for-Power-BI
  task. Minimal infra; the flow *is* the script.
- **Airflow** — the industry standard when you have **many** interdependent
  pipelines, a team, strong scheduling/backfill needs, and you're OK running and
  maintaining the scheduler + DB + webserver. Heavier, but battle-tested and
  ubiquitous in enterprises.

For "get one API into Power BI as CSV", **Prefect (or even a plain cron + the
script)** is plenty. Reach for Airflow when the number and complexity of
pipelines — not this single one — justify the platform.

---

## 6. Mapping this onto *this* project (optional)

The demo deliberately hits the project's public `brand-margins` API so the output
is real. If you later wanted to productionize a feed (e.g. daily sales for Power
BI), the same three-step shape applies — you'd just swap `fetch_api_data` for a
Django management command or an ORM query. Today that role is filled by
`so/management/commands/check_salesorder_margins.py` running under plain cron; an
orchestrator would add retries, history, and a UI on top of exactly that.

---

## 7. Files in this folder

| File | What it is |
|------|------------|
| `prefect_flow.py` | The Prefect pipeline (run this). |
| `airflow_dag.py` | The same pipeline as an Airflow DAG (for comparison). |
| `requirements.txt` | Demo dependencies (install in a throwaway venv). |
| `powerbi_inbox/` | Created on first run; holds `latest.csv` + dated CSVs. |
| `README.md` | This file. |

**To remove everything:** delete the `prefect_airflow_demo/` folder.
