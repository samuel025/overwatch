# Overwatch

**A team of AI agents that watches your running system, works out *why* something broke, and reports it to Slack and Jira with the evidence.**

Monitoring tools tell you *that* something is wrong. Overwatch investigates: it correlates metrics, logs, deploy history and past incidents, runs its own read-only queries to test hypotheses, and hands an on-call engineer a root cause they can verify, with a human approval step before anything is sent.

Overwatch is standalone and system-agnostic. You point it at your data with one config file; it does not need to run inside your system.

Built for HackOS 2026, Track 02: *The Agent That Can Explain Why*.

---

## How it works

```
 your system's data (databases, metrics, logs, deploys, tickets)
                 │
          Zetaris (one SQL layer over all sources)   ── or direct Postgres / Prometheus sources
                 │  read-only queries
   ┌─────────────┼───────────────────────────────────────────┐
   │ Watcher ──anomaly──► Historian ──similar incidents──►   │
   │ (checks, no model)   (H-MEM memory)                     │
   │                                  Investigator ◄──► queries
   │                                  (Nemotron via Nightshift)
   │                                        │ root cause + evidence trail
   │                                  human approves (UI / CLI)
   │                                        ▼
   │                                   Responder ──► Slack, Jira, webhook, console
   │                                        └──► teaches H-MEM (store incident, reward/penalize memories)
   └─────────────────────────────────────────────────────────┘
```

| Agent | What it does | Powered by |
|---|---|---|
| **Watcher** | Evaluates your checks (query + threshold) every interval. Deterministic and cheap. | your data sources |
| **Historian** | Recalls past incidents with similar symptoms. | **Meterless H-MEM** engine |
| **Investigator** | Hypothesis → read-only query → evidence loop, ending in a cited root cause, confidence and fix. | **Meterless Nightshift** → **NVIDIA Nemotron** |
| **Responder** | After human approval, sends the report and updates memory. | Slack / Jira / webhook |

## Sponsor technology

- **Zetaris**: the data layer. Overwatch's agents read through Zetaris so one query can span application data, ops metrics, deploy history and tickets that live in different systems. Configure it as a `type: zetaris` source.
- **Meterless H-MEM** (engine): incident memory in `memory-service/`, a TypeScript service the Python agents call over HTTP. Confirmed incidents are stored in the long-term tier with provenance; human approval/dismissal feeds back as `helpful` / `not_helpful`, which changes memory confidence. All mutations are recorded in the trust ledger (`ledger.jsonl`). **Official conformance suite: 13/13 pass**, see [`eval/hmem-conformance.md`](eval/hmem-conformance.md).
- **Meterless Nightshift** (provider): the app's primary AI provider. Every model call goes through Nightshift's local OpenAI-compatible endpoint (`overwatch/llm.py`), with the `meterless-app` / `meterless-scope` headers. Every call is logged to `traces/llm_calls.jsonl`.
- **NVIDIA Nemotron**: connected as a provider inside Nightshift; powers the Investigator's reasoning.

## The console

`overwatch run` serves a live console at http://127.0.0.1:8080:

- **Signals**: every check as a live chart with its alert threshold.
- **Agent pipeline**: Watcher → Historian → Investigator → Responder, lighting up as each one works.
- **Live investigation**: each thought, each query as it runs, its result rows, H-MEM recalls with relevance, then the diagnosis with confidence, recommended fixes and Approve / Dismiss.
- **Blast radius**: a live service map with the origin, affected and at-risk services and the failing call paths; when it started, how fast it was caught, how long it has lasted; and its business cost from your impact queries. Computed from data, not by the model (the model only names the origin service).
- **Incidents, Memory, Model calls**: incident history, H-MEM's stored memories and trust ledger, and every model call through Nightshift (model, latency, tokens).
- **Report a problem**: type a symptom and watch Overwatch investigate it.

Everything streams from `/api/events` (server-sent events), so the page is live without polling.

### Incident lifecycle

- **Watching never pauses.** Investigations run on a small worker pool (`watch.max_parallel_investigations`), so checks keep running and charts keep moving while the model thinks.
- **A cascade is one incident.** Alerts that fire within `watch.merge_window_seconds` of an open incident join it instead of opening five separate ones.
- **Incidents recover on their own** once every check behind them has stayed healthy for `watch.resolve_after_seconds`; a check that flaps back resets the clock. The blast radius then shows the time to recover. Incidents opened from a human report have no check to watch, so a human closes them.
- **Nothing is lost on restart.** Incidents are stored in SQLite (`state_path`, default `<trace_dir>/overwatch.db`); an investigation cut off by a restart is marked failed rather than left hanging. (Check history is in memory, so recovery timing restarts with the process.)
- **The console can be locked.** Set `OVERWATCH_TOKEN` (or `console.token`), open the console once with `?token=...`, and the browser keeps a cookie; the API also accepts `Authorization: Bearer`. `overwatch run --host 0.0.0.0` refuses to start without a token unless you pass `--insecure`. The UI is a React app in `web/`, built into the Python package:

```bash
cd web && npm install && npm run build   # outputs overwatch/ui/dist, served by `overwatch run`
npm run dev                              # hot reload on :5173, proxies /api to :8080
```

## Repository layout

```
overwatch/              Python package (the product, system-agnostic)
  agents/               watcher, historian, investigator, responder
  sources/              sql (Zetaris / Postgres / any SQLAlchemy DB), prometheus
  memory/               hmem client (default), local and none fallbacks
  notifiers/            console, slack, jira, webhook
  orchestrator.py       pipeline + incident state
  server.py, events.py  JSON API + live event stream
  ui/dist/              built console (from web/)
  cli.py                `overwatch` command
  templates/            starter overwatch.yaml
web/                    console source (React, Vite)
memory-service/         H-MEM engine (TypeScript) + HTTP server + vendored conformance suite
examples/demo-system/   demo shop to break on purpose (3 services, Postgres, fault injector)
eval/                   engine test results
tests/                  offline tests
```

## Requirements

- Python 3.11+, Node.js 20+
- Meterless Nightshift running locally, with NVIDIA connected as a provider (free key at https://build.nvidia.com)
- Zetaris (recommended) or direct database access
- Docker, only for the demo system

## Quick start (your own system)

```bash
# 1. Install
python -m venv .venv && . .venv/bin/activate
pip install -e .
(cd memory-service && npm install)

# 2. Start the H-MEM memory service (keeps state in memory-service/data)
(cd memory-service && npm start) &

# 3. Configure
cp .env.example .env            # fill in LLM_API_KEY (Nightshift token), ZETARIS_URL, ...
overwatch init overwatch.yaml   # then edit: sources, catalog, checks, notifiers

# 4. Verify everything is reachable
overwatch -c overwatch.yaml doctor

# 5. Run
overwatch -c overwatch.yaml run            # watch continuously, UI at http://127.0.0.1:8080
overwatch -c overwatch.yaml check          # one pass from the terminal
overwatch -c overwatch.yaml investigate "checkout is slow since lunch"
```

### Configuring for your system

Everything system-specific lives in `overwatch.yaml` (see `overwatch/templates/overwatch.example.yaml`):

- **sources**: SQLAlchemy URLs (Zetaris, Postgres, MySQL, ...) or Prometheus URLs. Queries are restricted to a single `SELECT`/`WITH`, capped in rows and time, and run in a read-only transaction on Postgres. Use a read-only database user anyway.
- **catalog**: describe the tables/metrics the Investigator may query. This matters more than anything else for investigation quality.
- **checks**: a query returning one number. A check alerts on a fixed `threshold`, on `relative_factor` (e.g. 4× its own median over the last `baseline_minutes`, but only above `relative_min`), or both. Set `no_data: alert` to treat a check that returns nothing (a service that stopped reporting) as an incident.
- **topology**: which service calls which. Used to compute the blast radius: what is affected, what is at risk.
- **impact**: queries that turn an incident into business cost (failed checkouts, revenue at risk...). `{since}` is replaced with the incident start.
- **llm**: Nightshift URL, token (`api_key` or `token_file`), model or routing mode.
- **approval**: `manual` (default) or `auto`.
- **notifiers**: Slack incoming webhook, Jira Cloud (email + API token), generic webhook, console.

Adding an integration is one small class: a `DataSource` (`query`), a `Notifier` (`send`) or a `Memory` (`remember`/`recall`).

## Demo: break a shop and watch Overwatch explain it

```bash
cd examples/demo-system
docker compose up -d --build        # gateway, orders, payments, Postgres (shopdb + opsdb) on :55432, load generator
python scripts/seed_memory.py       # load past incident tickets into H-MEM
overwatch -c overwatch.yaml doctor
overwatch -c overwatch.yaml run     # open http://127.0.0.1:8080

python scripts/inject_fault.py list          # the fault catalog
python scripts/inject_fault.py show lock_contention
python scripts/inject_fault.py slow_query    # break something
python scripts/inject_fault.py reset
```

### Fault catalog

19 realistic failures, each with ground truth used for automatic grading (`scripts/faults.py`). They are designed so the obvious answer is often wrong:

| Fault | Category | What really happened | Why it's hard |
|---|---|---|---|
| `slow_query` | code | orders v1.8.0 migration dropped an index | the error shows up in the gateway, not orders |
| `bad_deploy` | code | payments v2.3.0 fails 35% of payments | |
| `pool_exhaustion` | config | config-77 shrank the payments DB pool | |
| `downstream_timeout` | dependency | payments got slow, no deploy | no change record to blame |
| `red_herring` | dependency | same, plus an innocent gateway deploy at the same moment | tempting wrong answer |
| `lock_contention` | database | an `inventory-sync` session holds a table lock, idle in transaction | only visible in `pg_stat_activity` / `pg_locks`, never seen before |
| `config_timeout` | config | config-81 set gateway→orders timeout to 4 ms | orders is healthy but gets blamed |
| `retry_storm` | compound | payments fails and gateway v3.2.0 retries multiply the load | two interacting causes |
| `n_plus_one` | code | orders v1.9.0 runs one query per order | no single slow query; evidence is call counts in `pg_stat_statements` |
| `report_job` | capacity | a batch report moved into business hours saturates DB CPU | cause is outside the services |
| `flash_sale` | capacity | real traffic surge (15 → ~65 rps) | right answer is "scale", not "roll back" |
| `memory_leak` | code | orders v1.9.1 leaks memory, OOM-killed every minute | intermittent; evidence is a pattern over time |
| `database_down` | infrastructure | Postgres is down | Overwatch's own data sources fail |
| `double_trouble` | compound | index dropped AND bad payments deploy | a single-cause answer is half right |
| `service_down` | infrastructure | the orders service stops entirely | it goes silent: shown on the map as "Not responding" |
| `bad_replica` | infrastructure | one of two payments instances fails everything | the service looks half-broken; only per-instance data shows it is one copy |
| `connection_leak` | code | payments v2.4.0 never returns DB connections; pools drain over minutes | gradual: a trend, not a step |
| `provider_rate_limit` | dependency | the external card processor answers 429 to 40% of charges | nothing changed on our side; the fix is outside the code |
| `cert_expired` | dependency | TLS to the card processor fails: certificate has expired | a classic outage, found in the logs |

The demo is built to be realistic, not easy:

- Change records read like real ones ("Release 2.3.0", "config sync from infra-repo@4be1f0"), never like the answer, and other teams' unrelated releases keep arriving in the background.
- Error messages are what the software would actually log, and a small background error rate (<1%) is always present.
- payments runs as two instances; a `collector` container records CPU, memory, restarts and OOM kills per container, like cAdvisor / the kubelet in a real cluster.
- A `chaos` container plays the outside world: the external card processor, other teams' changes, and background troublemakers (lock holder, report job, traffic surge).

```bash
python scripts/verify_faults.py     # proves each fault shows up in the data (no model needed)
python ../../eval/run_eval.py       # full benchmark: every fault, H-MEM on vs off -> eval/results.md
```

## Tests

```bash
pip install -e ".[dev]" && pytest                  # offline: no DB, model or memory service needed
cd memory-service && npm run conformance           # official H-MEM conformance suite
```

## Pre-existing code

- `memory-service/src/hmem/` and `memory-service/conformance/` are vendored from the Meterless H-MEM reference implementation (Apache-2.0), with Overwatch changes listed in [`memory-service/src/hmem/NOTICE.md`](memory-service/src/hmem/NOTICE.md).
- Everything else was written during the HackOS 2026 build window.
