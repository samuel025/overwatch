import { useId, useState } from "react";
import clsx from "clsx";
import { setupApi, type AiSettings, type External, type Notifiers, type WatchSettings } from "../api";

/** Form building blocks shared by the setup wizard and the settings page. */

export function Section({ id, title, description, aside, children }: {
  id?: string;
  title: string;
  description?: React.ReactNode;
  aside?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="s-section" id={id} aria-labelledby={id && `${id}-title`}>
      <header className="s-section-head">
        <div>
          <h2 id={id && `${id}-title`}>{title}</h2>
          {description && <p className="s-desc">{description}</p>}
        </div>
        {aside}
      </header>
      <div className="s-section-body">{children}</div>
    </section>
  );
}

export function Field({ label, hint, children, wide }: { label: string; hint?: React.ReactNode; children: React.ReactNode; wide?: boolean }) {
  return (
    <label className={clsx("field", wide && "field-wide")}>
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function Switch({ checked, onChange, label, hint, disabled }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: React.ReactNode;
  hint?: React.ReactNode;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className={clsx("switch-row", disabled && "switch-disabled")}>
      <button id={id} type="button" role="switch" aria-checked={checked} className="switch" disabled={disabled}
        onClick={() => onChange(!checked)}>
        <span className="switch-thumb" />
      </button>
      <label htmlFor={id} className="switch-text">
        <span className="switch-label">{label}</span>
        {hint && <span className="field-hint">{hint}</span>}
      </label>
    </div>
  );
}

export function SecretInput({ value, isSet, onChange, placeholder }: { value: string; isSet?: boolean; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <input type="password" value={value} autoComplete="off" onChange={(e) => onChange(e.target.value)}
      placeholder={isSet ? "•••••••• saved — type to replace" : placeholder} />
  );
}

// ---------- external services ----------

export function ExternalsEditor({ externals, services, onChange, logHint }: {
  externals: External[];
  services: string[];
  onChange: (next: External[]) => void;
  logHint: string;
}) {
  const set = (i: number, patch: Partial<External>) => onChange(externals.map((e, j) => (j === i ? { ...e, ...patch } : e)));
  return (
    <>
      {externals.length === 0 && <p className="empty-box">No external services yet. Add the third-party APIs and managed services your system calls.</p>}
      <ul className="ext-list">
        {externals.map((e, i) => (
          <li key={i} className={clsx("ext", !e.enabled && "ext-off")}>
            <div className="ext-head">
              <input className="ext-name" value={e.name} placeholder="api.example.com" aria-label="Hostname or name"
                onChange={(ev) => set(i, { name: ev.target.value })} />
              {e.suggested && <span className="tag">found automatically</span>}
              <span className="ext-actions">
                <Switch checked={e.enabled} onChange={(v) => set(i, { enabled: v })} label="Watch" />
                <button type="button" className="btn btn-ghost btn-xs" onClick={() => onChange(externals.filter((_, j) => j !== i))}>Remove</button>
              </span>
            </div>
            {e.evidence?.length > 0 && <p className="field-hint">Why: {e.evidence.join(" · ")}</p>}
            <div className="field-grid">
              <Field label="What it is" wide>
                <input value={e.what ?? ""} placeholder="e.g. card processor; we call it to charge cards"
                  onChange={(ev) => set(i, { what: ev.target.value })} />
              </Field>
              <div className="field field-wide">
                <span className="field-label">Called by</span>
                <div className="chip-select" role="group" aria-label="Services that call it">
                  {services.filter((s) => s !== e.name).map((s) => {
                    const on = e.callers.includes(s);
                    return (
                      <button key={s} type="button" aria-pressed={on} className={clsx("chip-opt", on && "chip-opt-on")}
                        onClick={() => set(i, { callers: on ? e.callers.filter((c) => c !== s) : [...e.callers, s] })}>
                        {s}
                      </button>
                    );
                  })}
                </div>
              </div>
              <Field label="Health or status URL" hint="Optional. Overwatch calls it to tell “them” from “us”.">
                <input value={e.health_url} placeholder="https://status.example.com/api/v2/status.json"
                  onChange={(ev) => set(i, { health_url: ev.target.value })} />
              </Field>
              <Field label="Text in error logs when it fails" hint={logHint}>
                <input value={e.log_text} placeholder="e.g. the hostname, or a word from the error"
                  onChange={(ev) => set(i, { log_text: ev.target.value })} />
              </Field>
            </div>
          </li>
        ))}
      </ul>
      <button type="button" className="btn btn-dashed"
        onClick={() => onChange([...externals, { name: "", original: "", what: "", callers: [], evidence: [], health_url: "", log_text: "", enabled: true, suggested: false }])}>
        + Add external service
      </button>
    </>
  );
}

// ---------- AI provider ----------

export const AI_PRESETS = {
  nightshift: { label: "Meterless Nightshift", base_url: "http://127.0.0.1:8088/v1", model: "meterless/text.deep", triage_model: "meterless/text.fast" },
  nvidia: { label: "NVIDIA Nemotron (direct)", base_url: "https://integrate.api.nvidia.com/v1", model: "nvidia/nemotron-3-ultra-550b-a55b", triage_model: "nvidia/nemotron-3.5-lightning-30b-a3b" },
};

export function AiEditor({ ai, onChange }: { ai: AiSettings; onChange: (next: AiSettings) => void }) {
  const [test, setTest] = useState<{ ok: boolean; text: string } | null>(null);
  const [testing, setTesting] = useState(false);
  async function runTest() {
    setTest(null);
    setTesting(true);
    try {
      const res = await setupApi.testAi(ai);
      if (!res.ok) return setTest({ ok: false, text: res.error ?? "failed" });
      const listed = !ai.model || res.models?.includes(ai.model);
      setTest({ ok: true, text: `Connected · ${res.models?.length ?? 0} models${ai.model && !listed ? ` · “${ai.model}” not listed (it may still work)` : ""}` });
    } finally {
      setTesting(false);
    }
  }
  const preset = Object.entries(AI_PRESETS).find(([, p]) => p.base_url === ai.base_url)?.[0];
  return (
    <>
      <div className="segmented" role="radiogroup" aria-label="Provider preset">
        {Object.entries(AI_PRESETS).map(([k, p]) => (
          <button key={k} type="button" role="radio" aria-checked={preset === k} className={clsx(preset === k && "on")}
            onClick={() => onChange({ ...ai, base_url: p.base_url, model: p.model, triage_model: p.triage_model })}>
            {p.label}
          </button>
        ))}
        <button type="button" role="radio" aria-checked={!preset} className={clsx(!preset && "on")}
          onClick={() => onChange({ ...ai, base_url: preset ? "" : ai.base_url })}>
          Other (OpenAI-compatible)
        </button>
      </div>
      <div className="field-grid">
        <Field label="Address"><input value={ai.base_url} onChange={(e) => onChange({ ...ai, base_url: e.target.value })} placeholder="http://127.0.0.1:8088/v1" /></Field>
        <Field label="Key or token" hint={preset === "nightshift" ? "The Nightshift proxy token" : undefined}>
          <SecretInput value={ai.api_key} isSet={ai.api_key_set} onChange={(v) => onChange({ ...ai, api_key: v })} />
        </Field>
        <Field label="Investigation model" hint="Reasons through the evidence"><input value={ai.model} onChange={(e) => onChange({ ...ai, model: e.target.value })} placeholder="meterless/text.deep" /></Field>
        <Field label="Fast first-guess model" hint="Answers in seconds, before the full investigation"><input value={ai.triage_model} onChange={(e) => onChange({ ...ai, triage_model: e.target.value })} placeholder="meterless/text.fast" /></Field>
        <Field label="Investigation steps" hint="Most queries the Investigator may run per incident">
          <input type="number" min={2} max={20} value={ai.max_steps ?? 8} onChange={(e) => onChange({ ...ai, max_steps: Number(e.target.value) })} />
        </Field>
      </div>
      <div className="inline-row">
        <button type="button" className="btn" onClick={runTest} disabled={!ai.base_url || testing}>{testing ? "Testing…" : "Test connection"}</button>
        {test && <span className={clsx("small", test.ok ? "ok" : "err")} role="status">{test.ok ? "✓" : "✕"} {test.text}</span>}
      </div>
    </>
  );
}

// ---------- notifications ----------

export function NotificationsEditor({ value, onChange }: { value: Notifiers; onChange: (next: Notifiers) => void }) {
  const slack = value.slack ?? { enabled: false, webhook_url: "" };
  const jira = value.jira ?? { enabled: false, base_url: "", email: "", api_token: "", project_key: "" };
  const hook = value.webhook ?? { enabled: false, url: "" };
  return (
    <div className="channel-list">
      <div className="channel">
        <Switch checked={slack.enabled} onChange={(v) => onChange({ ...value, slack: { ...slack, enabled: v } })}
          label="Slack" hint="Post each diagnosis to a channel once you approve it." />
        {slack.enabled && (
          <div className="field-grid channel-body">
            <Field label="Incoming webhook URL" wide hint="Slack → Apps → Incoming Webhooks. Stored in .env.">
              <SecretInput value={slack.webhook_url} isSet={slack.webhook_url_set} placeholder="https://hooks.slack.com/services/…"
                onChange={(v) => onChange({ ...value, slack: { ...slack, webhook_url: v } })} />
            </Field>
          </div>
        )}
      </div>
      <div className="channel">
        <Switch checked={jira.enabled} onChange={(v) => onChange({ ...value, jira: { ...jira, enabled: v } })}
          label="Jira" hint="Open a ticket with the root cause, evidence and fix." />
        {jira.enabled && (
          <div className="field-grid channel-body">
            <Field label="Site URL"><input value={jira.base_url} placeholder="https://yourteam.atlassian.net" onChange={(e) => onChange({ ...value, jira: { ...jira, base_url: e.target.value } })} /></Field>
            <Field label="Project key"><input value={jira.project_key} placeholder="OPS" onChange={(e) => onChange({ ...value, jira: { ...jira, project_key: e.target.value } })} /></Field>
            <Field label="Account email"><input value={jira.email} placeholder="oncall@example.com" onChange={(e) => onChange({ ...value, jira: { ...jira, email: e.target.value } })} /></Field>
            <Field label="API token" hint="Stored in .env.">
              <SecretInput value={jira.api_token} isSet={jira.api_token_set} onChange={(v) => onChange({ ...value, jira: { ...jira, api_token: v } })} />
            </Field>
          </div>
        )}
      </div>
      <div className="channel">
        <Switch checked={hook.enabled} onChange={(v) => onChange({ ...value, webhook: { ...hook, enabled: v } })}
          label="Webhook" hint="POST the full incident as JSON: PagerDuty bridge, Teams relay, your own API." />
        {hook.enabled && (
          <div className="field-grid channel-body">
            <Field label="URL" wide hint="Stored in .env.">
              <SecretInput value={hook.url} isSet={hook.url_set} placeholder="https://example.com/overwatch-hook"
                onChange={(v) => onChange({ ...value, webhook: { ...hook, url: v } })} />
            </Field>
          </div>
        )}
      </div>
      <p className="field-hint">The console always shows every incident; these channels are in addition.</p>
    </div>
  );
}

// ---------- watching & approval ----------

const WATCH_FIELDS: { key: keyof WatchSettings; label: string; hint: string; unit?: string }[] = [
  { key: "interval_seconds", label: "Check every", unit: "s", hint: "How often each check runs" },
  { key: "confirm_samples", label: "Confirm after", unit: "passes", hint: "Bad passes in a row before an incident opens" },
  { key: "resolve_after_seconds", label: "Recovered after", unit: "s", hint: "Healthy this long and the incident closes" },
  { key: "merge_window_seconds", label: "Merge alerts within", unit: "s", hint: "New alerts join a recent incident" },
  { key: "cooldown_seconds", label: "Cooldown", unit: "s", hint: "Quiet period before the same check alerts again" },
  { key: "max_parallel_investigations", label: "Parallel investigations", hint: "Incidents investigated at once" },
];

export function WatchEditor({ watch, approval, onWatch, onApproval }: {
  watch: Partial<WatchSettings>;
  approval: "manual" | "auto";
  onWatch: (next: Partial<WatchSettings>) => void;
  onApproval: (v: "manual" | "auto") => void;
}) {
  return (
    <>
      <div className="field-grid field-grid-3">
        {WATCH_FIELDS.map((f) => (
          <Field key={f.key} label={f.label} hint={f.hint}>
            <span className="unit-input">
              <input type="number" min={1} value={watch[f.key] ?? ""} onChange={(e) => onWatch({ ...watch, [f.key]: Number(e.target.value) })} />
              {f.unit && <span>{f.unit}</span>}
            </span>
          </Field>
        ))}
      </div>
      <div className="field">
        <span className="field-label">When a diagnosis is ready</span>
        <div className="radio-cards" role="radiogroup">
          {([["manual", "Wait for my approval", "A person reviews the diagnosis before it is sent to Slack or Jira."],
            ["auto", "Send automatically", "Notifications go out as soon as the Investigator concludes."]] as const).map(([v, t, d]) => (
            <button key={v} type="button" role="radio" aria-checked={approval === v} className={clsx("radio-card", approval === v && "on")} onClick={() => onApproval(v)}>
              <span className="radio-dot" />
              <span><b>{t}</b><span className="field-hint">{d}</span></span>
            </button>
          ))}
        </div>
      </div>
    </>
  );
}
