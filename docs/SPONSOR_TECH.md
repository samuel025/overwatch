# Sponsor technology write-up

Overwatch is an AI on-call investigator (HackOS 2026, Track 02: *The Agent That Can Explain Why*). This note says what each sponsor technology does in it, what was tested, and what was not.

## Zetaris: the data layer

**Role.** Zetaris is Overwatch's own data layer. When a user connects a database, Overwatch registers it as a Zetaris datasource, and the Investigator queries only Zetaris (`<datasource>.<table>`, Spark SQL). One query can therefore join tables from different systems.

**What goes through it.**
- Every connected database, including its live sessions and locks (`<db>.live_sessions`, `<db>.live_locks`): Zetaris datasource views that run inside the database, so nothing is added to the user's schema.
- Prometheus, through a `promql` command that becomes a Zetaris REST table plus a flattening view.
- Loki, through `logql`, the same way.
- Docker telemetry, mirrored into Overwatch's own Postgres.
- Overwatch's own incident and change history.

**What does not.** Kubernetes and distributed traces are read live from their own APIs. That is a choice, not a limit we hit: the trace analysis and the span-attribute allowlist are in Overwatch's own code, and routing raw traces through Zetaris would bypass both. The Watcher also keeps a direct path for its checks, so monitoring does not depend on a heavier layer.

**Unified Semantic Layer object.** Besides the `usl` views, Overwatch compiles a Zetaris USL (`lightning.metastore.overwatch.incident_response`: `service`, `incident`, `change_event`, with primary and foreign keys and descriptions) and activates each table over its own history. It appears under Data Product > Unified Semantic Layer in the Zetaris UI.

**Semantic layer and data quality.** Overwatch builds a Unified Semantic Layer in Zetaris at startup: a SchemaStore container `usl` with `check_health`, `container_status`, `quality_results`, `incident_history`, `change_impact` (a release against what its service's checks did in the next 15 minutes) and, with Prometheus connected, `target_status`, which joins Prometheus targets (read through `promql`) with Docker state from Overwatch's own history. Discovery also generates data quality rules from column names (never-negative amounts and counts, an end time before the creation time) that run through Zetaris every 5 minutes as `dq_*` checks. Run live on 2026-10-29: all five views answered, and the rules read 0 breaking rows over 1.5 million.

**A Zetaris fault we found and worked around.** On 2.4.3 a `WHERE` that compares two columns returns every row: on the check-samples table, `WHERE value < failing` and `WHERE value >= failing` both returned 8,229, the whole table, while Postgres, a moment later, returned 81 and 7,657 of 8,243. The same comparison inside `sum(CASE ...)` is correct. Overwatch's rules use the `CASE` form and the Investigator is told not to write the `WHERE` form.

**Safety.** Agents can only run `SELECT`, `WITH`, `SHOW`, `DESCRIBE` and the `promql`/`logql` commands. Zetaris queries have wall-clock deadlines. Only Overwatch's setup and startup run DDL.

**Evidence.** Run live against a local Zetaris install. [`eval/loki-through-zetaris.md`](../eval/loki-through-zetaris.md) records an investigation in which `logql` results and Overwatch's history were joined and diagnosed correctly. Code: `overwatch/datalayer.py`, `overwatch/sources/zetaris.py`.

## Meterless H-MEM: the engine

**Role.** Incident memory. Approved incidents are stored with provenance and confidence. When something breaks, the Historian recalls similar past incidents from the symptoms alone, and the Investigator receives them as hints. Approving or dismissing a diagnosis becomes `helpful` or `not_helpful` feedback that changes the memory's confidence, and every change is written to a trust ledger.

**Test results.**
- The official H-MEM conformance suite: **13/13** ([`eval/hmem-conformance.md`](../eval/hmem-conformance.md)).
- The recall, use, feedback and store loop, run live: past incidents were recalled, one was used in the diagnosis, a new memory was stored, and the helpful one was rewarded ([`eval/hmem-recall-test.md`](../eval/hmem-recall-test.md)).

**Limit.** The recall threshold (0.25) was tuned by hand on the demo's incident history. We have not measured how recall quality changes with a large or noisy history.

## Meterless Runtime / Nightshift: the provider

**Role.** Every model call goes through one OpenAI-compatible client (`overwatch/llm.py`) built to Meterless Runtime's documented contract: the `meterless-app` and `meterless-scope` headers, the bearer token read from `proxy-token`, `meterless/*` model ids, URL normalization, and a 180 second timeout.

**What was tested, honestly.**
- The client is tested against a stand-in that implements the documented contract (`tests/test_settings.py`).
- **It has not been run against the real Meterless Runtime or Nightshift.** The current Nightshift build targets NVIDIA DGX Spark (ARM64) and has no Ubuntu x86_64 installer.
- Meterless's reviewers said that integrating Runtime as a provider and testing with NVIDIA's hosted API satisfies the requirement. All real investigations used NVIDIA's hosted API directly.
- Switching to Nightshift is a two-line change in `.env` ([`REFERENCE.md`](REFERENCE.md)).

## NVIDIA Nemotron: the model

**Role.**
- `nvidia/nemotron-3-ultra-550b-a55b` drives the Investigator's reasoning: it forms a hypothesis, runs one read-only query, reads the result, and repeats until it can conclude with evidence.
- `nvidia/nemotron-3.5-lightning-30b-a3b` gives the fast first guess within seconds and acts as a second reader that lists claims the data does not support.

**Results.** Real diagnoses with Nemotron: a dropped index (`slow_query`) correct three times, up to 98% confidence; `service_down`, `lock_contention`, `bad_deploy` and `config_timeout` correct; and a live URL-shortener application (killed and paused Postgres, killed Redis, connection exhaustion, slow queries) diagnosed correctly. These are spot checks during development, not a benchmark over many runs. We do not claim a diagnosis accuracy percentage.

## How the model's output is kept honest

The model's conclusion is checked mechanically against the data it saw: every cited quote and technical term must appear in the results, unverified quotes cap confidence at 60%, a suspect the timing argues against caps it at 50%, and cited log lines from more than 10 minutes before the problem began cap it at 60%. Nothing is sent to Slack or Jira until a person clicks Approve.

## Tools used to build it

Built with Claude Code. Pre-existing code and licenses are declared in section 10 of the [README](../README.md).
