# Overwatch: Multi-Agent Incident Investigator

A team of AI agents that watches a running microservice system, notices when something is wrong, investigates the cause across several data sources, and reports an evidence-backed root cause to Slack and Jira.

- Event: HackOS 2026 (build window closes **27 Oct 2026, 23:45 UTC**; submit at hackathon.genai.works/dashboard)
- Primary track: **02, The Agent That Can Explain Why**
- Also competing for: **NVIDIA Nemotron award** (Nemotron powers the Investigator)

---

## 1. The problem

When a production system slows down or fails, the on-call engineer has to open five tools (metrics, logs, deploy history, tickets, chat), guess a cause, and compare against what happened last time. This is slow, stressful and repetitive. Alerts say *that* something is wrong, rarely *why*.

**Target user:** an on-call engineer or SRE in a small-to-mid team running microservices.
**Success measure:** time from "problem starts" to "correct root cause with evidence is in front of a human".

## 2. The solution

Four cooperating agents:

| Agent | Job | Model (via Nightshift) |
|---|---|---|
| **Watcher** | Polls metrics through Zetaris, flags anomalies (slow DB, latency, error spike). Cheap and frequent. | Small Nemotron |
| **Investigator** | Forms hypotheses, runs queries to confirm or reject each, builds the evidence trail. | Larger Nemotron |
| **Historian** | Recalls similar past incidents and the fix that worked. | H-MEM engine |
| **Responder** | Drafts the Slack message and Jira ticket. A human clicks approve before anything is sent. | Any Nightshift model |

Flow:

```
Target system (3 services + Postgres)
        │ metrics, logs, deploys, tickets
        ▼
     Zetaris  (one SQL layer over all sources)
        ▼
 Watcher ──alert──► Investigator ◄──► Historian (H-MEM)
                        │ root cause + evidence trail
                        ▼
                    Responder ──(human approves)──► Slack + Jira
```

All model calls go through **Meterless Nightshift**. Nemotron is connected inside Nightshift.

## 3. Sponsor technology: where and how

| Technology | Role | What to show / prove |
|---|---|---|
| **Zetaris** | The only way agents read data. One query layer over metrics, logs, deploy history and the ticket table. | A cross-source query in the demo, e.g. "which deploy happened just before latency rose?" Screenshot of Zetaris running with 3+ sources. |
| **Meterless H-MEM (primary engine)** | Incident memory used by the Historian. Open source, runs inside our own code, **no API key**. | The with/without memory evaluation table (section 8) and the JSONL traces the engine writes (keep them in `eval/traces/`). |
| **Meterless Markovian (optional second engine)** | Keeps the Investigator's long multi-step investigation bounded (fixed framing, goal, current input, capped carryover instead of the full history). Also open source, no key. | One long investigation where the trace shows the carryover staying capped. |
| **Meterless Nightshift** | Primary AI provider for every agent. Runs **locally** and exposes an **OpenAI-compatible endpoint plus an access token** while it is running. No hosted Meterless API or credits to sign up for. | One real request passing through and returning, with the token hidden. |
| **NVIDIA Nemotron** | Powers the Investigator's reasoning. NVIDIA is connected as a provider inside Nightshift, alongside our direct NVIDIA connection. | Name the model in the demo and README. |
| *(Stretch)* World Model engine | Predict blast radius ("checkout fails in ~10 min if DB stays slow"). | Only if the core is finished by Day 4. |

**Two separate requirements (confirmed by the organisers):**
- **Build:** at least one engine integrated into a working feature. H-MEM (and Markovian if added) covers this. The JSONL traces are good evidence.
- **Power:** Nightshift added as a primary AI provider, like OpenAI or Anthropic. Separate from the engines, so we need both. The ready-made Cursor prompt is under "Power" at https://meterless.ai/engines.html and does the provider integration.

**Practical consequences:**
- Nightshift must be **running on the machine** whenever the agents run, including during demo recording. Put "start Nightshift" in the README.
- `overwatch/llm.py` is a plain OpenAI-compatible client pointed at the Nightshift local URL with the access token from `.env`. Meterless's examples call this token `LLM_API_KEY`; it is generated on our machine and is **not** a hosted Meterless key. Use that name in `.env.example`, plus the local base URL.
- **Model keys:** Nightshift can use keys we already have (e.g. OpenRouter) or a free NVIDIA key from https://build.nvidia.com. Get the NVIDIA key today and add it as a provider inside Nightshift; the participant guide also allows direct NVIDIA calls as a backup.
- Engines are libraries, so `memory.py` imports them directly and needs no key.

> Still to confirm: Zetaris install steps and its connector types for each source (and whether it can reach a local Postgres), the Nightshift local URL and how to pick a Nemotron model through it, and the H-MEM API. Check the "Getting started" lessons and meterless.ai/engines.html on Day 1 and update this section. Do not design around guesses.

## 4. Pluggable by design

Overwatch is a **standalone tool**, not tied to any particular system. Someone else installs it, writes one config file describing their system, and runs it. Nothing in `overwatch/` knows about our demo services.

**How a user plugs it in** (`overwatch.yaml`):
- **sources:** where to read data. Each is a SQL connection (SQLAlchemy URL) or a Prometheus URL. The recommended setup is one Zetaris source that already federates all their data; direct Postgres/Prometheus sources also work.
- **catalog:** short descriptions of the tables/metrics the Investigator may query, so the model knows what exists.
- **checks:** what the Watcher evaluates (a query + threshold), e.g. "p95 latency of orders > 500 ms".
- **notifiers:** console, Slack webhook, Jira, generic webhook.
- **llm:** Nightshift base URL, token (from env), model names per agent.
- **approval:** `manual` (human approves before anything is sent) or `auto`.

**Adapter interfaces** (each a small class, so new integrations are one file):
- `DataSource`: `query(sql_or_promql) -> rows`, read-only enforced (only `SELECT`/`WITH`, row limit, timeout)
- `Notifier`: `send(incident_report)`
- `Memory`: `remember(incident)`, `recall(symptoms) -> similar incidents` (H-MEM implementation + simple local fallback)

**Model calls** use a plain JSON action loop (the model answers with `{"action": "query", ...}` or `{"action": "conclude", ...}`), so it works with any OpenAI-compatible provider even without native tool calling.

**Ways to run it:** `overwatch run -c overwatch.yaml` (continuous), `overwatch check` (one pass), `overwatch investigate "orders is slow"` (ad hoc), plus a small web UI for the trace and approve button. Packaged as a pip-installable package and a Docker image.

## 5. Demo target system (only for the demo, in `examples/`)

A `docker-compose` setup:
- `gateway`, `orders`, `payments`: small services (any language; pick what the team knows best)
- `postgres`: the database
- Emits Prometheus-style metrics and structured logs to files or tables Zetaris can read
- A `deploys` table recording each fake deploy (service, version, time)
- A `tickets` table seeded with 10-15 past incidents (this is what H-MEM learns from)

**Fault injector** (`scripts/inject_fault.py <name>`):
1. `slow_query`: drop an index so one query becomes slow
2. `bad_deploy`: new version raises the error rate
3. `pool_exhaustion`: connection pool saturates
4. `downstream_timeout`: payments times out
5. *(stretch)* `memory_leak`

Scripted faults make the demo repeatable and give us the evaluation data.

**Language decision (22 Oct):** Python for the agents; H-MEM runs as a small TypeScript service (`memory-service/`) because the Meterless engines and their official conformance runner are TypeScript. Conformance: 13/13 passing (`eval/hmem-conformance.md`).

**Nightshift facts (from meterless.ai/engines.html):** default `http://127.0.0.1:8088/v1`, health `GET /healthz`, models `GET /v1/models`; token in `<stateDir>/proxy-token` sent as Bearer; send `meterless-app` and `meterless-scope: user` headers; model ids are routing modes like `meterless/text.deep` (pick Nemotron inside Nightshift, or pin a model id).

## 6. Repo layout

```
hackos/
  PLAN.md
  README.md                 # setup, usage, dependencies, sponsor-tech write-up
  pyproject.toml            # pip-installable package, `overwatch` CLI
  overwatch.example.yaml    # template a user copies for their own system
  .env.example / .gitignore
  overwatch/                # the product, system-agnostic
    config.py  models.py  llm.py  orchestrator.py  cli.py  server.py
    sources/                # base.py, sql.py (Zetaris/Postgres/any SQLAlchemy), prometheus.py
    agents/                 # watcher.py, investigator.py, historian.py, responder.py
    memory/                 # base.py, local.py (fallback), hmem.py (client for memory-service)
    notifiers/              # console.py, slack.py, jira.py, webhook.py
  memory-service/           # H-MEM engine (TypeScript) + HTTP server + conformance suite
  examples/demo-system/     # docker-compose services + Postgres + fault injector + its overwatch.yaml
  eval/                     # run_eval.py, results.md, traces/
  docs/                     # architecture diagram, screenshots, deck, demo script
```

## 7. Build plan (UTC)

**Today, 22 Oct. Registration closes today: register and create the team first.**
- [ ] Register, create team, select Track 02
- [ ] Run Nightshift locally, connect NVIDIA inside it (keep the direct NVIDIA connection too), send one real request through its OpenAI-compatible endpoint that returns from a Nemotron model
- [ ] Open https://meterless.ai/engines.html, grab the H-MEM / Markovian code and the "Add Meterless as a provider" Cursor prompt
- [ ] Install Zetaris (follow "Getting started with Zetaris")
- [ ] Decide team roles (section 10)

**23 Oct (Day 2): Data foundation**
- [ ] `docker-compose` target system running and emitting metrics and logs
- [ ] Zetaris connected to at least 3 sources; one cross-source query works
- [ ] Fault injector with faults 1-2
- [ ] Opening ceremony, 09:00 UTC

**24 Oct (Day 3): Agents**
- [ ] Watcher detects an injected fault
- [ ] Investigator produces a root cause with a list of queries as evidence
- [ ] Historian backed by H-MEM; seed past incidents
- [ ] Write the first version of the eval (section 8)

**25 Oct (Day 4): Actions, UI, polish. Feature freeze at end of day**
- [ ] Responder: Slack webhook message + Jira ticket (mock fallback ready)
- [ ] UI with investigation trace and approve button
- [ ] Faults 3-4; run the full eval and save results
- [ ] Start README and architecture diagram

**26 Oct (Day 5): Packaging, bug fixes only**
- [ ] Record demo video (1-4 min), keys hidden
- [ ] Build deck
- [ ] Finish README; test it from a fresh clone

**27 Oct: Buffer and submission**
- [ ] Run the checklist (section 11)
- [ ] **Submit by 18:00 UTC**, well before the 23:45 deadline

## 8. Engine test result (required)

For each injected fault, run the pipeline **with H-MEM memory on** and **off**, 3 runs each. Record:
- Root cause correct? (yes/no, judged against the known injected fault)
- Time to root cause (seconds)
- Number of queries used

Save to `eval/results.md` as a table. Expected story: memory improves accuracy and speed on faults similar to seeded past incidents. If it does not, report that honestly and explain it.

**Built (22 Oct):** 14-fault catalog with ground truth (`examples/demo-system/scripts/faults.py`), 6 with a similar past ticket and 8 never seen before. `scripts/verify_faults.py` proves each fault shows in the data; `eval/run_eval.py` runs every fault with H-MEM on/off and grades automatically → `eval/results.md`. Plus the official H-MEM conformance scorecard (13/13) in `eval/hmem-conformance.md`. Needs Nightshift to run.

## 9. Demo script (about 3 min)

1. (0:00) One sentence on the problem. Healthy system on screen.
2. (0:20) Inject `slow_query`.
3. (0:35) Watcher flags the orders latency.
4. (0:50) Investigator trace: latency up → DB p95 up → one query → deploy 20 minutes earlier. Show the Zetaris cross-source query.
5. (1:40) Historian: "Similar to the incident on 3 Oct, fixed by adding an index." Show H-MEM.
6. (2:00) Responder drafts Slack and Jira; click approve; both appear.
7. (2:30) Show one request going through Nightshift (keys hidden), name Nemotron, show the eval table.
8. (2:50) Close: what is unique.

## 10. Suggested roles (adjust to team size)

- **A. Target system and fault injector**, docker-compose, load generator
- **B. Agents and Zetaris**, Watcher, Investigator, Nightshift client
- **C. Memory, integrations and eval**, H-MEM, Slack, Jira, eval script
- **D. UI, video and deck**

Solo or two people: cut the UI to a simple page, mock Jira, and skip faults 3-5.

## 11. Submission checklist

- [ ] Track 02 selected in the team workspace
- [ ] Demo video 1-4 min, full workflow, no keys visible
- [ ] Video and deck name Zetaris, H-MEM and Nightshift and show where each sits
- [ ] Engine eval result included
- [ ] Deck: problem and users, solution, architecture diagram, stack, sponsor-tech usage, screenshots, future work
- [ ] README works from a fresh clone
- [ ] `.env` ignored; grep the repo for keys
- [ ] Pre-existing code declared in the form
- [ ] Nemotron feature described (for the NVIDIA award)
- [ ] Submitted and confirmed on the dashboard

## 12. Risks

| Risk | Mitigation |
|---|---|
| Zetaris setup takes longer than expected | Start it today; ask in the hackathon channels early; keep sources simple (SQL tables/files) |
| Nightshift not running during a demo or test | Add a startup check in `llm.py` that fails loudly with "start Nightshift"; list it first in the README |
| Scope creep | No custom dashboards; freeze features end of Day 4; stretch items only after the core works |
| Jira/Slack setup eats time | Slack webhook + one Jira REST call; keep a mock endpoint as fallback |
| LLM gives vague or wrong root causes | Require the Investigator to cite query results; test against the known injected faults |
| Hallucinated actions | Human approval before sending anything |
| Leaked keys | `.env` only, `.gitignore`, review the video before upload |
| "Why not Datadog?" | Deck answer: it explains across sources and remembers past incidents, rather than only alerting |

## 13. Open decisions

- Team size and roles
- Service language for the target system
- Which stretch item to add if time allows (World Model or fault 5)
