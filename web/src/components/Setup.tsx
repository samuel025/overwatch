import { useEffect, useState } from "react";
import clsx from "clsx";
import { toast } from "sonner";
import { setupApi, type AiSettings, type Notifiers, type Plan, type SetupState, type WatchSettings } from "../api";
import { AiEditor, ExternalsEditor, Field, NotificationsEditor, Section, Switch, WatchEditor, DraftButton } from "./SettingsParts";
import { SystemMap } from "./SystemMap";

const STEPS = [
  { id: "connect", label: "Connect", hint: "Where things run" },
  { id: "system", label: "Review system", hint: "Services and external APIs" },
  { id: "watch", label: "Checks & alerts", hint: "What to watch, who to tell" },
] as const;
type Step = (typeof STEPS)[number]["id"];

const DEFAULT_WATCH: WatchSettings = {
  interval_seconds: 5, confirm_samples: 2, cooldown_seconds: 600, merge_window_seconds: 300,
  resolve_after_seconds: 60, max_parallel_investigations: 2,
};

/** First-run wizard (and "run discovery again"): connect → review what was found → choose checks and alerts. */
export function Setup({ state, onCancel }: { state: SetupState; onCancel?: () => void }) {
  const [step, setStep] = useState<Step>("connect");
  const [useDocker, setUseDocker] = useState(state.docker_available);
  const [project, setProject] = useState(state.docker_projects[0] ?? "");
  const [serviceLabel, setServiceLabel] = useState("");
  const [useK8s, setUseK8s] = useState(state.kubernetes_in_cluster);
  const [k8s, setK8s] = useState({ url: "", token: "", namespace: "", service_label: "", ca_file: "", skip_tls: false });
  const [loki, setLoki] = useState({ url: "", tenant: "", token: "" });
  const [traces, setTraces] = useState({ url: "", tenant: "", token: "" });
  const [prometheus, setPrometheus] = useState("");
  const [databases, setDatabases] = useState<{ name: string; url: string }[]>([{ name: "", url: "" }]);
  const [ai, setAi] = useState<AiSettings>({ base_url: state.ai_base_url, api_key: "", api_key_set: state.ai_configured, model: state.ai_model, triage_model: "", max_steps: 8 });
  const [notifiers, setNotifiers] = useState<Notifiers>({});
  const [watch, setWatch] = useState<Partial<WatchSettings>>(DEFAULT_WATCH);
  const [approval, setApproval] = useState<"manual" | "auto">("manual");
  const [busy, setBusy] = useState(false);
  const [drafted, setDrafted] = useState<Record<string, boolean>>({}); // fields an AI wrote and nobody has edited yet
  const edited = (key: string) => setDrafted((x) => (x[key] ? { ...x, [key]: false } : x));
  const [saving, setSaving] = useState(false);
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
        kubernetes: { enabled: useK8s, ...k8s },
        loki,
        traces,
      });
      setPlan(res.plan);
      setYaml(res.yaml);
      if (res.plan.errors.length && !res.plan.checks.length) toast.error("Nothing could be discovered", { description: res.plan.errors.join("\n") });
      else setStep("system");
    } catch (e) {
      toast.error("Discovery failed", { description: String(e) });
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!plan) return;
    setSaving(true);
    try {
      const res = await setupApi.apply({ plan: { ...plan, settings: { notifiers, watch, approval, ai: { max_steps: ai.max_steps } } }, ai });
      toast.success("Overwatch is watching your system", { description: `Saved ${res.config}${res.backup ? ` (old config kept as ${res.backup.split("/").pop()})` : ""}` });
      window.setTimeout(() => window.location.assign("/"), 800);
    } catch (e) {
      toast.error("Could not start", { description: String(e) });
      setSaving(false);
    }
  }

  const updatePlan = (fn: (p: Plan) => void) => setPlan((p) => { if (!p) return p; const n = structuredClone(p); fn(n); return n; });
  const todo = (v: string) => v.startsWith("TODO") || v.endsWith("TODO: one or two sentences on what the system does and how requests flow.");
  const previewTopology = (() => {
    if (!plan) return {};
    const t: Record<string, string[]> = Object.fromEntries(Object.entries(plan.topology).map(([k, v]) => [k, [...v]]));
    for (const e of plan.externals ?? []) {
      if (!e.enabled || !e.name.trim()) continue;
      for (const c of e.callers) t[c] = [...new Set([...(t[c] ?? []), e.name.trim()])];
    }
    return t;
  })();
  const stepIndex = STEPS.findIndex((s) => s.id === step);
  const canDiscover = useDocker || useK8s || !!loki.url || !!traces.url || !!prometheus || databases.some((d) => d.url);

  return (
    <div className="settings-shell setup">
      <div className="settings-top">
        <div>
          <h1>{state.configured ? "Run discovery again" : "Connect your system"}</h1>
          <p className="muted small">
            Overwatch finds your services, how they connect, what to watch and what data it can read, then writes <code>{state.config_path}</code>.
          </p>
        </div>
        {onCancel && <button type="button" className="btn" onClick={onCancel}>← Back to console</button>}
      </div>

      <ol className="stepper">
        {STEPS.map((s, i) => (
          <li key={s.id} className={clsx(i === stepIndex && "on", i < stepIndex && "done")}>
            <button type="button" disabled={i > stepIndex || saving} onClick={() => setStep(s.id)}>
              <span className="step-num">{i < stepIndex ? "✓" : i + 1}</span>
              <span><b>{s.label}</b><span className="field-hint">{s.hint}</span></span>
            </button>
          </li>
        ))}
      </ol>

      {step === "connect" && (
        <div className="s-stack">
          <Section title="Where your services run" description="Gives Overwatch your services, who calls whom, liveness, health endpoints, logs and container events.">
            <Switch checked={useDocker} onChange={setUseDocker} disabled={!state.docker_available} label="Docker / Docker Compose"
              hint={state.docker_available
                ? `Detected on this machine · ${state.docker_projects.length} Compose project${state.docker_projects.length === 1 ? "" : "s"}`
                : "Docker is not reachable from Overwatch"} />
            {useDocker && (
              <div className="field-grid">
                <Field label="Compose project">
                  <select value={project} onChange={(e) => setProject(e.target.value)}>
                    {state.docker_projects.map((p) => <option key={p}>{p}</option>)}
                  </select>
                </Field>
                <Field label="Service label" hint="Optional. Container label naming the service; default is the Compose service.">
                  <input value={serviceLabel} onChange={(e) => setServiceLabel(e.target.value)} placeholder="com.docker.compose.service" />
                </Field>
              </div>
            )}
          </Section>

          <Section title="Kubernetes" description="Reads pods, restarts and OOM kills, logs, events and rollouts from the API server. Give it a service account that can get and list pods, pod logs, events and deployments.">
            <Switch checked={useK8s} onChange={setUseK8s}
              label="Kubernetes cluster"
              hint={state.kubernetes_in_cluster ? "Overwatch is running inside a cluster; its service account is used" : "Overwatch is outside a cluster: give it the API server address and a token"} />
            {useK8s && (
              <div className="field-grid">
                {!state.kubernetes_in_cluster && (
                  <>
                    <Field label="API server"><input value={k8s.url} onChange={(e) => setK8s({ ...k8s, url: e.target.value })} placeholder="https://my-cluster:6443" /></Field>
                    <Field label="Token" hint="A service account token. Stored in .env.">
                      <input type="password" autoComplete="off" value={k8s.token} onChange={(e) => setK8s({ ...k8s, token: e.target.value })} />
                    </Field>
                    <Field label="Cluster CA certificate" hint="Optional. Path to the cluster's CA file, when its certificate is not publicly signed (local, on-prem and many managed clusters).">
                      <input value={k8s.ca_file} disabled={k8s.skip_tls} onChange={(e) => setK8s({ ...k8s, ca_file: e.target.value })} placeholder="/path/to/ca.crt" />
                    </Field>
                    <Field label="Certificate check" hint="A last resort for a cluster whose CA you do not have. Traffic is still encrypted, but the server is not verified.">
                      <Switch checked={k8s.skip_tls} onChange={(v) => setK8s({ ...k8s, skip_tls: v })} label="Skip certificate verification" />
                    </Field>
                  </>
                )}
                <Field label="Namespace" hint="Optional. Defaults to the service account's namespace, or “default”.">
                  <input value={k8s.namespace} onChange={(e) => setK8s({ ...k8s, namespace: e.target.value })} placeholder="shop" />
                </Field>
                <Field label="Service label" hint="Optional. Label naming a service; default app.kubernetes.io/name, then app.">
                  <input value={k8s.service_label} onChange={(e) => setK8s({ ...k8s, service_label: e.target.value })} placeholder="app" />
                </Field>
              </div>
            )}
          </Section>

          <Section title="Logs" description="Application logs, read straight from Loki. No log table needed; services are found from its labels.">
            <div className="field-grid">
              <Field label="Loki" wide hint="Optional. Becomes an error-rate check per service.">
                <input value={loki.url} onChange={(e) => setLoki({ ...loki, url: e.target.value })} placeholder="http://loki:3100" />
              </Field>
              {loki.url && (
                <>
                  <Field label="Tenant" hint="Optional. For multi-tenant Loki."><input value={loki.tenant} onChange={(e) => setLoki({ ...loki, tenant: e.target.value })} /></Field>
                  <Field label="Token" hint="Optional. Stored in .env."><input type="password" autoComplete="off" value={loki.token} onChange={(e) => setLoki({ ...loki, token: e.target.value })} /></Field>
                </>
              )}
            </div>
          </Section>

          <Section title="Traces" description="Distributed traces show where a request spent its time and where it failed first. Overwatch reads them from Grafana Tempo or Jaeger (both take OpenTelemetry) and builds the real call graph from them.">
            <div className="field-grid">
              <Field label="Tempo or Jaeger" wide hint="Optional. Becomes a request-time and an error check per service, and the map of who calls whom. Needs services that emit OpenTelemetry traces.">
                <input value={traces.url} onChange={(e) => setTraces({ ...traces, url: e.target.value })} placeholder="http://tempo:3200  or  http://jaeger:16686" />
              </Field>
              {traces.url && (
                <>
                  <Field label="Tenant" hint="Optional. For multi-tenant Tempo."><input value={traces.tenant} onChange={(e) => setTraces({ ...traces, tenant: e.target.value })} /></Field>
                  <Field label="Token" hint="Optional. Stored in .env."><input type="password" autoComplete="off" value={traces.token} onChange={(e) => setTraces({ ...traces, token: e.target.value })} /></Field>
                </>
              )}
            </div>
          </Section>

          <Section title="Metrics and data" description="Use read-only credentials. Tables, change logs, logs, metrics tables and database health views are recognised automatically.">
            <div className="field-grid">
              <Field label="Prometheus" wide hint="Optional. Standard request metrics become latency and error checks.">
                <input value={prometheus} onChange={(e) => setPrometheus(e.target.value)} placeholder="http://prometheus:9090" />
              </Field>
            </div>
            <div className="field-label">Databases or Zetaris</div>
            {databases.map((d, i) => (
              <div key={i} className="db-row">
                <input aria-label="Database name" value={d.name} placeholder="name, e.g. zetaris" onChange={(e) => setDatabases(databases.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                <input aria-label="Connection URL" value={d.url} placeholder="postgresql://readonly:password@host:5432/db" onChange={(e) => setDatabases(databases.map((x, j) => (j === i ? { ...x, url: e.target.value } : x)))} />
                {databases.length > 1 && <button type="button" className="btn btn-ghost" onClick={() => setDatabases(databases.filter((_, j) => j !== i))}>Remove</button>}
              </div>
            ))}
            <button type="button" className="btn btn-dashed" onClick={() => setDatabases([...databases, { name: "", url: "" }])}>+ Add database</button>
          </Section>

          <Section title="AI provider" description="Meterless Nightshift by default. Any OpenAI-compatible endpoint works.">
            <AiEditor ai={ai} onChange={setAi} />
          </Section>

          <div className="wizard-actions">
            <span className="field-hint">{canDiscover ? "Discovery only reads. Nothing is changed in your system." : "Choose at least one place to look."}</span>
            <button type="button" className="btn btn-primary" onClick={discover} disabled={busy || !canDiscover}>
              {busy ? "Discovering…" : "Discover my system →"}
            </button>
          </div>
        </div>
      )}

      {step === "system" && plan && (
        <div className="s-stack">
          {(plan.errors.length > 0 || plan.notes.length > 0) && (
            <div className="notice" role="status">
              {plan.errors.map((e) => <p key={e} className="err">{e}</p>)}
              {plan.notes.map((n) => <p key={n}>{n}</p>)}
            </div>
          )}

          <Section title={`Your system · ${Object.keys(plan.services).length} services`}
            description={`Discovered from ${[plan.docker.project && "Docker", ...Object.keys(plan.sources)].filter(Boolean).join(", ")}. Lines show who calls whom.`}>
            {Object.keys(previewTopology).length > 0
              ? <div className="s-preview"><SystemMap preview={previewTopology} /></div>
              : <p className="empty-box">No dependencies found. You can add them to the configuration later.</p>}
          </Section>

          <Section title="External services"
            description="Third-party APIs your system calls. Docker can't see them, so Overwatch looks for their addresses in your services' settings and error logs (only the hostname is kept). Add any it missed.">
            <ExternalsEditor externals={plan.externals ?? []} services={Object.keys(plan.services).filter((s) => !(plan.externals ?? []).some((e) => e.name === s))}
              onChange={(next) => updatePlan((p) => { p.externals = next; })}
              logHint={plan.log_source ? `Optional. Becomes a check on ${plan.log_source.type === "sql" ? plan.log_source.table : "container logs"}: more than 5 such errors in 2 minutes.` : "No logs were found to check."} />
          </Section>

          <Section title="Describe it" description="What an engineer would tell a newcomer. It helps the investigation more than anything else. Fields marked in amber still need you.">
            <div className="field-grid">
              <Field label="The system" wide>
                <textarea rows={3} className={clsx(todo(plan.system.description) && "todo", drafted.__system && "drafted")} value={plan.system.description}
                  onChange={(e) => { edited("__system"); updatePlan((p) => { p.system.description = e.target.value; }); }} />
                {drafted.__system && <span className="draft-tag">AI draft: check it</span>}
              </Field>
            </div>
            <DraftButton
              build={() => ({
                system: plan.system,
                services: Object.entries(plan.services).map(([name, f]) => ({ name, what: f.what ?? "", runs_on: plan.service_facts?.[name] || f.runs_on, image: f.image })),
                topology: plan.topology,
                checks: plan.checks.map((c) => ({ name: c.name, service: c.service })),
                ai: { base_url: ai.base_url, api_key: ai.api_key, model: ai.model, triage_model: ai.triage_model },
              })}
              apply={(r) => {
                const keys: Record<string, boolean> = {};
                Object.keys(plan.services).forEach((n) => { if (r.services[n]) keys[n] = true; });
                if (r.system) keys.__system = true;
                updatePlan((p) => { Object.keys(p.services).forEach((n) => { if (r.services[n]) p.services[n] = { ...p.services[n], what: r.services[n] }; }); if (r.system) p.system.description = r.system; });
                setDrafted((x) => ({ ...x, ...keys }));
                return Object.keys(keys).length;
              }} />
            <table className="s-table">
              <thead><tr><th>Service</th><th>What it does</th></tr></thead>
              <tbody>
                {Object.entries(plan.services).map(([name, facts]) => (
                  <tr key={name}>
                    <td className="mono">{name}{(plan.service_facts?.[name] || facts.runs_on) && <span className="field-hint fact-line" title={`Worked out by Overwatch, not written by anyone: ${plan.service_facts?.[name] || facts.runs_on}`}>{plan.service_facts?.[name] || facts.runs_on}</span>}</td>
                    <td><input aria-label={`${name}: what it does`} className={clsx(todo(facts.what ?? "") && "todo", drafted[name] && "drafted")} value={facts.what ?? ""}
                      onChange={(e) => { edited(name); updatePlan((p) => { p.services[name] = { ...p.services[name], what: e.target.value }; }); }} />
                      {drafted[name] && <span className="draft-tag">AI draft: check it</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          <div className="wizard-actions">
            <button type="button" className="btn" onClick={() => setStep("connect")}>← Back</button>
            <button type="button" className="btn btn-primary" onClick={() => setStep("watch")}>Continue →</button>
          </div>
        </div>
      )}

      {step === "watch" && plan && (
        <div className="s-stack">
          <Section title={`Checks · ${plan.checks.filter((c) => c.enabled).length} on`} description="What the Watcher evaluates on every pass.">
            <ul className="toggle-grid">
              {plan.checks.map((c, i) => (
                <li key={c.name}>
                  <Switch checked={c.enabled} onChange={(v) => updatePlan((p) => { p.checks[i].enabled = v; })}
                    label={<><span className="mono">{c.name}</span> <span className="tag">{c.kind ?? "metric"}</span></>} hint={c.why || c.description} />
                </li>
              ))}
            </ul>
          </Section>

          <Section title="Evidence" description="Gathered in parallel the moment something breaks, before the AI starts reasoning.">
            <ul className="toggle-grid">
              {plan.evidence.map((e, i) => (
                <li key={e.name}>
                  <Switch checked={e.enabled} onChange={(v) => updatePlan((p) => { p.evidence[i].enabled = v; })} label={e.label} hint={`from ${e.source}`} />
                </li>
              ))}
            </ul>
            <p className="field-hint">Always included: changes reported to Overwatch{plan.docker.project ? ", container state and container events" : ""}.</p>
          </Section>

          <Section title="Notifications" description="Where a diagnosis goes after it is approved.">
            <NotificationsEditor value={notifiers} onChange={setNotifiers} />
          </Section>

          <Section title="Watching & approval" description="How quickly Overwatch reacts, and who decides what gets sent.">
            <WatchEditor watch={watch} approval={approval} onWatch={setWatch} onApproval={setApproval} />
          </Section>

          <Section title="Configuration file" description="Secrets (database passwords, API keys, webhook URLs) go into .env next to it, never into this file.">
            <details className="yaml-details">
              <summary>Preview the generated overwatch.yaml</summary>
              <pre className="sql yaml-preview">{yaml}</pre>
            </details>
          </Section>

          <div className="wizard-actions">
            <button type="button" className="btn" onClick={() => setStep("system")} disabled={saving}>← Back</button>
            <button type="button" className="btn btn-primary" onClick={save} disabled={saving || !plan.checks.some((c) => c.enabled)}>
              {saving ? "Starting…" : "Save and start watching"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
