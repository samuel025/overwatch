import { useEffect, useState } from "react";
import clsx from "clsx";
import { toast } from "sonner";
import { setupApi, type External, type Plan, type SetupState } from "../api";
import { SystemMap } from "./SystemMap";

type Step = "connect" | "review" | "saving";

const AI_PRESETS = {
  nightshift: { label: "Meterless Nightshift", base_url: "http://127.0.0.1:8088/v1", model: "meterless/text.deep", triage_model: "meterless/text.fast" },
  nvidia: { label: "NVIDIA (direct)", base_url: "https://integrate.api.nvidia.com/v1", model: "nvidia/nemotron-3-ultra-550b-a55b", triage_model: "nvidia/nemotron-3.5-lightning-30b-a3b" },
};

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function Setup({ state, onCancel }: { state: SetupState; onCancel?: () => void }) {
  const [step, setStep] = useState<Step>("connect");
  const [useDocker, setUseDocker] = useState(state.docker_available);
  const [project, setProject] = useState(state.docker_projects[0] ?? "");
  const [serviceLabel, setServiceLabel] = useState("");
  const [prometheus, setPrometheus] = useState("");
  const [databases, setDatabases] = useState<{ name: string; url: string }[]>([{ name: "", url: "" }]);
  const [ai, setAi] = useState({ base_url: state.ai_base_url, api_key: "", model: state.ai_model, triage_model: "" });
  const [aiTest, setAiTest] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [yaml, setYaml] = useState("");

  useEffect(() => {
    if (!state.docker_projects.includes(project) && state.docker_projects[0]) setProject(state.docker_projects[0]);
  }, [state.docker_projects, project]);

  async function discover() {
    setBusy(true);
    try {
      const res = await setupApi.discover({
        docker: { enabled: useDocker, project, service_label: serviceLabel },
        prometheus_url: prometheus,
        databases: databases.filter((d) => d.name && d.url),
      });
      setPlan(res.plan);
      setYaml(res.yaml);
      if (res.plan.errors.length && !res.plan.checks.length) toast.error("Nothing could be discovered", { description: res.plan.errors.join("\n") });
      else setStep("review");
    } catch (e) {
      toast.error("Discovery failed", { description: String(e) });
    } finally {
      setBusy(false);
    }
  }

  async function testAi() {
    setAiTest(null);
    const res = await setupApi.testAi(ai);
    if (!res.ok) return setAiTest({ ok: false, text: res.error ?? "failed" });
    const has = !ai.model || res.models?.includes(ai.model);
    setAiTest({ ok: true, text: `Connected · ${res.models?.length ?? 0} models${ai.model && !has ? ` · "${ai.model}" not listed (it may still work)` : ""}` });
  }

  async function save() {
    if (!plan) return;
    setStep("saving");
    try {
      const res = await setupApi.apply({ plan, ai });
      toast.success("Overwatch is watching your system", { description: `Saved ${res.config}${res.backup ? ` (old config kept as ${res.backup})` : ""}` });
      window.setTimeout(() => window.location.assign("/"), 800);
    } catch (e) {
      toast.error("Could not start", { description: String(e) });
      setStep("review");
    }
  }

  const updatePlan = (fn: (p: Plan) => Plan) => setPlan((p) => (p ? fn(structuredClone(p)) : p));
  const todo = (v: string) => v.startsWith("TODO");
  const setExternal = (i: number, patch: Partial<External>) =>
    updatePlan((p) => { p.externals[i] = { ...p.externals[i], ...patch }; return p; });
  // The map preview includes accepted external services.
  const previewTopology = (() => {
    if (!plan) return {};
    const t: Record<string, string[]> = Object.fromEntries(Object.entries(plan.topology).map(([k, v]) => [k, [...v]]));
    for (const e of plan.externals ?? []) {
      if (!e.enabled || !e.name.trim()) continue;
      for (const c of e.callers) t[c] = [...new Set([...(t[c] ?? []), e.name.trim()])];
    }
    return t;
  })();

  return (
    <div className="setup">
      <header className="setup-head">
        <div>
          <h1>Connect your system</h1>
          <p className="muted">
            Tell Overwatch where things are. It discovers your services, how they connect, what to watch and what
            data it can read, then writes <code>{state.config_path}</code> for you.
          </p>
        </div>
        <ol className="steps">
          <li className={clsx(step === "connect" && "on")}>1 · Connect</li>
          <li className={clsx(step !== "connect" && "on")}>2 · Review</li>
          <li className={clsx(step === "saving" && "on")}>3 · Start watching</li>
        </ol>
      </header>

      {step === "connect" && (
        <div className="setup-grid">
          <section className="card">
            <h2 className="card-title">Where your services run</h2>
            <label className="toggle">
              <input type="checkbox" checked={useDocker} onChange={(e) => setUseDocker(e.target.checked)} disabled={!state.docker_available} />
              <span>
                <b>Docker / Docker Compose</b>
                <span className="field-hint">
                  {state.docker_available
                    ? `Detected on this machine · ${state.docker_projects.length} Compose project${state.docker_projects.length === 1 ? "" : "s"}`
                    : "Docker is not reachable from Overwatch"}
                </span>
              </span>
            </label>
            {useDocker && (
              <div className="field-row">
                <Field label="Compose project">
                  <select value={project} onChange={(e) => setProject(e.target.value)}>
                    {state.docker_projects.map((p) => <option key={p}>{p}</option>)}
                  </select>
                </Field>
                <Field label="Service label (optional)" hint="Container label that names the service; default is the Compose service">
                  <input value={serviceLabel} onChange={(e) => setServiceLabel(e.target.value)} placeholder="com.docker.compose.service" />
                </Field>
              </div>
            )}
            <p className="field-hint">Gives Overwatch: your services, who calls whom, liveness, health endpoints, logs and container events.</p>
          </section>

          <section className="card">
            <h2 className="card-title">Metrics and data</h2>
            <Field label="Prometheus (optional)" hint="Standard request metrics become latency and error checks automatically">
              <input value={prometheus} onChange={(e) => setPrometheus(e.target.value)} placeholder="http://prometheus:9090" />
            </Field>
            <div className="field-label">Databases or Zetaris (optional)</div>
            {databases.map((d, i) => (
              <div key={i} className="db-row">
                <input value={d.name} placeholder="name, e.g. zetaris" onChange={(e) => setDatabases(databases.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                <input value={d.url} placeholder="postgresql://readonly:password@host:5432/db" onChange={(e) => setDatabases(databases.map((x, j) => (j === i ? { ...x, url: e.target.value } : x)))} />
                {databases.length > 1 && <button className="btn" onClick={() => setDatabases(databases.filter((_, j) => j !== i))} aria-label="Remove">×</button>}
              </div>
            ))}
            <button className="btn btn-small-inline" onClick={() => setDatabases([...databases, { name: "", url: "" }])}>+ Add a database</button>
            <p className="field-hint">Use a read-only user. Tables, change logs, logs, metrics tables and database health views are recognised automatically.</p>
          </section>

          <section className="card">
            <h2 className="card-title">AI provider</h2>
            <div className="preset-row">
              {Object.entries(AI_PRESETS).map(([k, p]) => (
                <button key={k} className="btn" onClick={() => setAi({ ...ai, base_url: p.base_url, model: p.model, triage_model: p.triage_model })}>{p.label}</button>
              ))}
            </div>
            <div className="field-row">
              <Field label="Address"><input value={ai.base_url} onChange={(e) => setAi({ ...ai, base_url: e.target.value })} placeholder="http://127.0.0.1:8088/v1" /></Field>
              <Field label="Key or token" hint={state.ai_configured ? "Leave empty to keep the one already set" : "Nightshift: the proxy-token file"}>
                <input type="password" value={ai.api_key} onChange={(e) => setAi({ ...ai, api_key: e.target.value })} autoComplete="off" />
              </Field>
            </div>
            <div className="field-row">
              <Field label="Investigation model"><input value={ai.model} onChange={(e) => setAi({ ...ai, model: e.target.value })} placeholder="meterless/text.deep" /></Field>
              <Field label="Fast first-guess model"><input value={ai.triage_model} onChange={(e) => setAi({ ...ai, triage_model: e.target.value })} placeholder="meterless/text.fast" /></Field>
            </div>
            <div className="test-row">
              <button className="btn" onClick={testAi} disabled={!ai.base_url}>Test connection</button>
              {aiTest && <span className={aiTest.ok ? "ok small" : "err small"}>{aiTest.ok ? "✓ " : "✕ "}{aiTest.text}</span>}
            </div>
          </section>

          <div className="setup-actions">
            {onCancel && <button className="btn" onClick={onCancel}>Back to the console</button>}
            <button className="btn btn-primary" onClick={discover} disabled={busy || (!useDocker && !prometheus && !databases.some((d) => d.url))}>
              {busy ? "Discovering…" : "Discover my system"}
            </button>
          </div>
        </div>
      )}

      {step !== "connect" && plan && (
        <div className="setup-grid">
          {(plan.errors.length > 0 || plan.notes.length > 0) && (
            <section className="card">
              {plan.errors.map((e) => <p key={e} className="err small">✕ {e}</p>)}
              {plan.notes.map((n) => <p key={n} className="small muted">• {n}</p>)}
            </section>
          )}

          <section className="card">
            <div className="card-head">
              <h2 className="card-title">Your system: {Object.keys(plan.services).length} services</h2>
              <span className="small muted">Discovered from {[plan.docker.project && "Docker", ...Object.keys(plan.sources)].filter(Boolean).join(", ")}</span>
            </div>
            {Object.keys(previewTopology).length > 0 ? <SystemMap preview={previewTopology} /> : <p className="empty">No dependencies found; add a topology later if you like.</p>}
          </section>

          <section className="card">
            <h2 className="card-title">External services</h2>
            <p className="field-hint">
              Third-party APIs and managed services your system calls. Docker cannot see them, so Overwatch looks for
              their addresses in your services' settings and error logs (only the hostname is kept, never the setting's
              value). Add any it missed.
            </p>
            {(plan.externals ?? []).length === 0 && <p className="empty">None found.</p>}
            <ul className="external-list">
              {(plan.externals ?? []).map((e, i) => (
                <li key={i} className={clsx("external", !e.enabled && "external-off")}>
                  <div className="external-top">
                    <label className="toggle">
                      <input type="checkbox" checked={e.enabled} onChange={(ev) => setExternal(i, { enabled: ev.target.checked })} />
                      <input className="external-name" value={e.name} placeholder="api.example.com" onChange={(ev) => setExternal(i, { name: ev.target.value })} />
                    </label>
                    {e.suggested ? <span className="kind-chip">found automatically</span> : <span className="kind-chip">added by you</span>}
                  </div>
                  {e.evidence?.length > 0 && <p className="field-hint">{e.evidence.join(" · ")}</p>}
                  <div className="field-label">Called by</div>
                  <div className="caller-row">
                    {Object.keys(plan.services).filter((s) => s !== e.name).map((s) => (
                      <label key={s} className={clsx("caller", e.callers.includes(s) && "caller-on")}>
                        <input type="checkbox" checked={e.callers.includes(s)}
                          onChange={(ev) => setExternal(i, { callers: ev.target.checked ? [...e.callers, s] : e.callers.filter((c) => c !== s) })} />
                        {s}
                      </label>
                    ))}
                  </div>
                  <div className="field-row">
                    <Field label="Health or status URL (optional)" hint="Overwatch will call it to tell 'them' from 'us'">
                      <input value={e.health_url} placeholder="https://status.example.com/api/v2/status.json" onChange={(ev) => setExternal(i, { health_url: ev.target.value })} />
                    </Field>
                    <Field label="Text in your error logs when it fails (optional)"
                      hint={plan.log_source ? `Becomes a check on ${plan.log_source.type === "sql" ? plan.log_source.table : "container logs"}: more than 5 such errors in 2 minutes` : "No logs found to check"}>
                      <input value={e.log_text} placeholder="e.g. the hostname, or a word from the error" onChange={(ev) => setExternal(i, { log_text: ev.target.value })} />
                    </Field>
                  </div>
                </li>
              ))}
            </ul>
            <button className="btn btn-small-inline"
              onClick={() => updatePlan((p) => ({ ...p, externals: [...(p.externals ?? []), { name: "", callers: [], evidence: [], health_url: "", log_text: "", enabled: true, suggested: false }] }))}>
              + Add an external service
            </button>
          </section>

          <section className="card">
            <h2 className="card-title">Describe it in a sentence or two</h2>
            <p className="field-hint">This is what an engineer would tell a newcomer. It helps the investigation most. Lines marked TODO are worth filling in.</p>
            <Field label="The system">
              <textarea rows={2} className={clsx(todo(plan.system.description.split(". ").pop() ?? "") && "todo")} value={plan.system.description}
                onChange={(e) => updatePlan((p) => ({ ...p, system: { ...p.system, description: e.target.value } }))} />
            </Field>
            {Object.entries(plan.services).map(([name, facts]) => (
              <Field key={name} label={name} hint={facts.runs_on}>
                <input className={clsx(todo(facts.what ?? "") && "todo")} value={facts.what ?? ""}
                  onChange={(e) => updatePlan((p) => ({ ...p, services: { ...p.services, [name]: { ...p.services[name], what: e.target.value } } }))} />
              </Field>
            ))}
          </section>

          <section className="card">
            <h2 className="card-title">What Overwatch will watch: {plan.checks.filter((c) => c.enabled).length} checks</h2>
            <ul className="check-list">
              {plan.checks.map((c, i) => (
                <li key={c.name}>
                  <label className="toggle">
                    <input type="checkbox" checked={c.enabled} onChange={(e) => updatePlan((p) => { p.checks[i].enabled = e.target.checked; return p; })} />
                    <span>
                      <b>{c.name}</b> <span className="kind-chip">{c.kind ?? "metric"}</span>
                      <span className="field-hint">{c.why || c.description}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </section>

          <section className="card">
            <h2 className="card-title">Evidence gathered the moment something breaks</h2>
            <ul className="check-list">
              {plan.evidence.map((e, i) => (
                <li key={e.name}>
                  <label className="toggle">
                    <input type="checkbox" checked={e.enabled} onChange={(ev) => updatePlan((p) => { p.evidence[i].enabled = ev.target.checked; return p; })} />
                    <span><b>{e.label}</b><span className="field-hint">from {e.source}</span></span>
                  </label>
                </li>
              ))}
              <li className="field-hint">Plus, built in: changes reported to Overwatch{plan.docker.project ? ", container state and container events" : ""}.</li>
            </ul>
          </section>

          <section className="card">
            <details>
              <summary className="card-title">The configuration file it will write</summary>
              <pre className="sql yaml-preview">{yaml}</pre>
              <p className="field-hint">Secrets (database addresses with passwords, the AI key) go into .env next to it, not into this file.</p>
            </details>
          </section>

          <div className="setup-actions">
            <button className="btn" onClick={() => setStep("connect")} disabled={step === "saving"}>Back</button>
            <button className="btn btn-primary" onClick={save} disabled={step === "saving" || !plan.checks.some((c) => c.enabled)}>
              {step === "saving" ? "Starting…" : "Save and start watching"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
