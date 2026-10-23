import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { toast } from "sonner";
import { settingsApi, type Settings as SettingsData } from "../api";
import { AiEditor, ExternalsEditor, Field, NotificationsEditor, Section, Switch, WatchEditor } from "./SettingsParts";
import { SystemMap } from "./SystemMap";

const SECTIONS = [
  { id: "system", label: "System & services" },
  { id: "externals", label: "External services" },
  { id: "checks", label: "Checks" },
  { id: "ai", label: "AI provider" },
  { id: "notify", label: "Notifications" },
  { id: "watch", label: "Watching & approval" },
  { id: "connections", label: "Connections" },
] as const;
type SectionId = (typeof SECTIONS)[number]["id"];

/** Edit the running configuration: everything is saved to overwatch.yaml (secrets to .env) and applied live. */
export function Settings({ onClose }: { onClose: () => void }) {
  const [saved, setSaved] = useState<SettingsData | null>(null);
  const [draft, setDraft] = useState<SettingsData | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [tab, setTab] = useState<SectionId>(() => {
    const want = window.location.hash.split("/")[1];
    return (SECTIONS.find((s) => s.id === want)?.id ?? "system") as SectionId;
  });

  useEffect(() => {
    settingsApi.read().then((s) => { setSaved(s); setDraft(structuredClone(s)); }).catch((e) => setError(String(e)));
  }, []);
  useEffect(() => { window.history.replaceState(null, "", `#settings/${tab}`); }, [tab]);

  const dirty = useMemo(() => !!draft && !!saved && JSON.stringify(draft) !== JSON.stringify(saved), [draft, saved]);
  const update = (fn: (d: SettingsData) => void) => setDraft((d) => { if (!d) return d; const n = structuredClone(d); fn(n); return n; });

  async function save() {
    if (!draft) return;
    setSaving(true);
    try {
      const res = await settingsApi.save(draft);
      const fresh = await settingsApi.read();
      setSaved(fresh);
      setDraft(structuredClone(fresh));
      toast.success("Settings saved and applied", { description: `Previous version kept as ${res.backup.split("/").pop()}` });
    } catch (e) {
      toast.error("Could not save", { description: String(e).replace(/^Error: \d+ /, "") });
    } finally {
      setSaving(false);
    }
  }

  if (error) return <div className="settings-shell"><p className="err">Could not load settings: {error}</p></div>;
  if (!draft) return <div className="settings-shell"><p className="muted">Loading settings…</p></div>;

  const counts: Partial<Record<SectionId, number>> = {
    externals: draft.externals.filter((e) => e.enabled).length,
    checks: draft.checks.filter((c) => c.enabled).length,
    notify: Object.values(draft.notifiers).filter((n) => n?.enabled).length,
  };
  const previewTopology = (() => {
    const t: Record<string, string[]> = {};
    for (const e of draft.externals) {
      if (!e.enabled || !e.name.trim()) continue;
      for (const c of e.callers) t[c] = [...new Set([...(t[c] ?? []), e.name.trim()])];
    }
    return t;
  })();

  return (
    <div className="settings-shell">
      <div className="settings-top">
        <div>
          <h1>Settings</h1>
          <p className="muted small">Changes are written to <code>{draft.config_path}</code> and applied without a restart. Secrets go to <code>.env</code>.</p>
        </div>
        <button type="button" className="btn" onClick={onClose}>← Back to console</button>
      </div>

      <div className="settings-layout">
        <nav className="settings-nav" aria-label="Settings sections">
          {SECTIONS.map((s) => (
            <button key={s.id} type="button" className={clsx("nav-item", tab === s.id && "on")} aria-current={tab === s.id ? "page" : undefined} onClick={() => setTab(s.id)}>
              {s.label}
              {counts[s.id] !== undefined && <span className="nav-count">{counts[s.id]}</span>}
            </button>
          ))}
        </nav>

        <div className="settings-content">
          {tab === "system" && (
            <Section id="system" title="System & services" description="What an engineer would tell a newcomer. The Investigator reads this as fact, so it matters more than anything else here.">
              <div className="field-grid">
                <Field label="System name"><input value={draft.system.name} onChange={(e) => update((d) => { d.system.name = e.target.value; })} /></Field>
                <Field label="How it works" wide hint="One or two sentences: what it does and how a request flows through it.">
                  <textarea rows={4} value={draft.system.description} onChange={(e) => update((d) => { d.system.description = e.target.value; })} />
                </Field>
              </div>
              <table className="s-table">
                <thead><tr><th>Service</th><th>What it does</th><th>Normal behaviour</th></tr></thead>
                <tbody>
                  {draft.services.map((s, i) => (
                    <tr key={s.name}>
                      <td className="mono">{s.name}{s.runs_on && <span className="field-hint">{s.runs_on}</span>}</td>
                      <td><input aria-label={`${s.name}: what it does`} className={clsx(s.what.startsWith("TODO") && "todo")} value={s.what} placeholder="e.g. takes orders, calls payments"
                        onChange={(e) => update((d) => { d.services[i].what = e.target.value; })} /></td>
                      <td><input aria-label={`${s.name}: normal behaviour`} value={s.normal} placeholder="e.g. p95 under 100 ms"
                        onChange={(e) => update((d) => { d.services[i].normal = e.target.value; })} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Section>
          )}

          {tab === "externals" && (
            <Section id="externals" title="External services"
              description="Third-party APIs and managed services your system calls. Overwatch can't see inside them, so it watches them from the outside: a status URL, and errors that mention them in your logs.">
              {Object.keys(previewTopology).length > 0 && <div className="s-preview"><SystemMap preview={previewTopology} /></div>}
              <ExternalsEditor externals={draft.externals} services={draft.service_names}
                onChange={(next) => update((d) => { d.externals = next; })}
                logHint="Optional. Becomes a check: more than 5 such errors in 2 minutes." />
            </Section>
          )}

          {tab === "checks" && (
            <Section id="checks" title="Checks" description="What the Watcher evaluates on every pass. Switch off noisy ones or adjust their limits.">
              <table className="s-table">
                <thead><tr><th>On</th><th>Check</th><th>Service</th><th>Severity</th><th>Limit</th></tr></thead>
                <tbody>
                  {draft.checks.map((c, i) => (
                    <tr key={c.name} className={clsx(!c.enabled && "row-off")}>
                      <td><Switch checked={c.enabled} onChange={(v) => update((d) => { d.checks[i].enabled = v; })} label={<span className="sr-only">{c.name}</span>} /></td>
                      <td><span className="mono">{c.name}</span> <span className="tag">{c.kind}</span><span className="field-hint">{c.description}</span></td>
                      <td className="mono">{c.service}</td>
                      <td>
                        <select aria-label={`${c.name} severity`} value={c.severity} onChange={(e) => update((d) => { d.checks[i].severity = e.target.value; })}>
                          {["low", "medium", "high", "critical"].map((s) => <option key={s}>{s}</option>)}
                        </select>
                      </td>
                      <td>
                        {c.kind === "probe" || c.kind === "container" ? (
                          <span className="muted small">up / down</span>
                        ) : c.threshold === null ? (
                          <span className="muted small">{c.relative_factor}× its normal</span>
                        ) : (
                          <span className="unit-input">
                            <span>{c.comparison}</span>
                            <input aria-label={`${c.name} limit`} type="number" step="any" value={c.threshold}
                              onChange={(e) => update((d) => { d.checks[i].threshold = e.target.value === "" ? null : Number(e.target.value); })} />
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Section>
          )}

          {tab === "ai" && (
            <Section id="ai" title="AI provider" description="Any OpenAI-compatible endpoint. Meterless Nightshift is the default; NVIDIA Nemotron works directly too.">
              <AiEditor ai={draft.ai} onChange={(ai) => update((d) => { d.ai = ai; })} />
            </Section>
          )}

          {tab === "notify" && (
            <Section id="notify" title="Notifications" description="Where a diagnosis goes after it is approved.">
              <NotificationsEditor value={draft.notifiers} onChange={(n) => update((d) => { d.notifiers = n; })} />
            </Section>
          )}

          {tab === "watch" && (
            <Section id="watch" title="Watching & approval" description="How quickly Overwatch reacts, and who decides what gets sent.">
              <WatchEditor watch={draft.watch} approval={draft.approval}
                onWatch={(w) => update((d) => { d.watch = { ...d.watch, ...w }; })}
                onApproval={(a) => update((d) => { d.approval = a; })} />
            </Section>
          )}

          {tab === "connections" && (
            <Section id="connections" title="Connections" description="Where Overwatch reads data. To connect something new (another database, Prometheus, a different Compose project), run discovery again.">
              <table className="s-table">
                <thead><tr><th>Name</th><th>Type</th></tr></thead>
                <tbody>{draft.sources.map((s) => <tr key={s.name}><td className="mono">{s.name}</td><td>{s.type}</td></tr>)}</tbody>
              </table>
              <div className="inline-row">
                <a className="btn" href="#setup">Run discovery again…</a>
                <span className="field-hint">Discovery writes a new configuration; the current one is backed up first.</span>
              </div>
            </Section>
          )}
        </div>
      </div>

      <div className={clsx("save-bar", dirty && "save-bar-on")} aria-hidden={!dirty}>
        <span>You have unsaved changes</span>
        <span className="save-bar-actions">
          <button type="button" className="btn btn-ghost" tabIndex={dirty ? 0 : -1} onClick={() => setDraft(structuredClone(saved))} disabled={saving}>Discard</button>
          <button type="button" className="btn btn-primary" tabIndex={dirty ? 0 : -1} onClick={save} disabled={saving}>{saving ? "Saving…" : "Save and apply"}</button>
        </span>
      </div>
    </div>
  );
}

