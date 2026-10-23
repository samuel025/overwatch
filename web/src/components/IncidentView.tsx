import { useState } from "react";
import NumberFlow from "@number-flow/react";
import clsx from "clsx";
import { toast } from "sonner";
import { currentActivity, eventsFor } from "../activity";
import { api, refreshMemory } from "../api";
import { clock, duration, formatCheckValue, humanize, incidentHeadline } from "../format";
import { useStore } from "../store";
import type { BlastRadius, Evidence, ImpactItem, Incident, Memory, OwEvent } from "../types";

type Row = Record<string, unknown>;
interface Step {
  step: number;
  thought: string;
  query?: { source: string; sql: string; purpose: string; key: boolean; done: boolean; error: string | null; rows: Row[]; count: number; ms: number };
}

const STATUS_CHIP: Record<Incident["status"], { label: string; tone: string }> = {
  investigating: { label: "Investigating", tone: "warn" },
  awaiting_approval: { label: "Needs your approval", tone: "info" },
  reported: { label: "Reported", tone: "ok" },
  dismissed: { label: "Dismissed", tone: "muted" },
  failed: { label: "Investigation failed", tone: "bad" },
};

/** Steps from the live event stream, or from the stored evidence for incidents loaded from history. */
function buildStory(events: OwEvent[], incident: Incident): { memories: Memory[] | null; steps: Step[]; thinking: number | null } {
  const mine = eventsFor(events, incident.id);
  const used = new Set(incident.similar_incidents.filter((m) => m.used).map((m) => m.id));
  const keyNotes = incident.evidence.map((e) => e.note.startsWith("[key] "));

  if (!mine.some((e) => e.type === "agent.thinking" || e.type === "memory.recalled")) {
    const memories = incident.similar_incidents.filter((m) => m.id !== "-");
    const steps = incident.evidence.map((ev: Evidence, i): Step => ({
      step: i + 1,
      thought: "",
      query: {
        source: ev.source, sql: ev.query, purpose: ev.note.replace(/^\[key\] /, ""), key: keyNotes[i], done: true,
        error: ev.error, rows: ev.rows.slice(0, 5), count: ev.rows.length, ms: ev.ms,
      },
    }));
    return { memories: memories.length || incident.status !== "investigating" ? memories : null, steps, thinking: null };
  }

  let memories: Memory[] | null = null;
  const steps = new Map<number, Step>();
  let thinking: number | null = null;
  let q = 0;
  for (const e of mine) {
    if (e.type === "memory.recalled") {
      memories = e.memories.filter((m) => m.id !== "-").map((m) => ({ ...m, used: used.has(m.id) }));
    } else if (e.type === "agent.thinking") {
      thinking = e.step;
    } else if (e.type === "investigator.thought") {
      thinking = null;
      steps.set(e.step, { ...(steps.get(e.step) ?? { step: e.step }), thought: e.thought } as Step);
    } else if (e.type === "query.started") {
      const s = steps.get(e.step) ?? { step: e.step, thought: "" };
      s.query = { source: e.source, sql: e.query, purpose: e.purpose, key: keyNotes[q++] ?? false, done: false, error: null, rows: [], count: 0, ms: 0 };
      steps.set(e.step, s);
    } else if (e.type === "query.finished") {
      const s = steps.get(e.step);
      if (s?.query) Object.assign(s.query, { done: true, error: e.error, rows: e.rows, count: e.row_count, ms: e.ms });
    }
  }
  return { memories, steps: [...steps.values()].sort((a, b) => a.step - b.step), thinking: incident.status === "investigating" ? thinking : null };
}

function ResultTable({ rows }: { rows: Row[] }) {
  if (!rows.length) return null;
  const cols = Object.keys(rows[0]).slice(0, 6);
  const show = (v: unknown) => {
    if (v === null || v === undefined) return "∅";
    if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
    const s = String(v);
    return s.length > 60 ? `${s.slice(0, 57)}…` : s;
  };
  return (
    <div className="result-scroll">
      <table className="result">
        <thead>
          <tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.slice(0, 5).map((r, i) => (
            <tr key={i}>{cols.map((c) => <td key={c}>{show(r[c])}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ImpactValue({ item }: { item: ImpactItem }) {
  if (item.value === null) return <span className="muted">n/a</span>;
  if (item.unit === "USD") return <NumberFlow value={item.value} format={{ style: "currency", currency: "USD", maximumFractionDigits: 0 }} />;
  return <NumberFlow value={item.value} format={{ maximumFractionDigits: 0 }} />;
}

function Impact({ blast, live }: { blast: BlastRadius; live: boolean }) {
  const recovered = blast.recovered_at != null;
  const lasting = live && !recovered ? Date.now() / 1000 - blast.started_at : blast.duration_s;
  return (
    <>
      <dl className="impact">
        {blast.impact.map((item) => (
          <div key={item.name} className={clsx("impact-item", item.unit === "USD" && "impact-money")}>
            <dt>{item.label}</dt>
            <dd><ImpactValue item={item} /></dd>
          </div>
        ))}
      </dl>
      <p className="small muted impact-time">
        Started {clock(blast.started_at)} · {blast.time_to_detect_s < 1 ? "caught on the first failing check" : `caught after ${duration(blast.time_to_detect_s)}`}
        {" · "}
        {recovered ? `recovered after ${duration(lasting)}` : live ? `ongoing for ${duration(lasting)}` : `lasted ${duration(lasting)}`}
      </p>
      {(blast.affected.length > 0 || blast.at_risk.length > 0) && (
        <p className="small">
          {blast.origin && <>Cause in <b>{blast.origin}</b>. </>}
          Affected: {blast.affected.join(", ") || "none"}. At risk: {blast.at_risk.join(", ") || "none"}.
        </p>
      )}
    </>
  );
}

function Detected({ incident }: { incident: Incident }) {
  return (
    <ul className="detected">
      {incident.anomalies.map((a) => {
        const name = humanize(a.check);
        const fmt = (v: number | null) => formatCheckValue({ name: a.check, threshold: a.comparison === "x_normal" ? null : a.threshold }, v);
        let text: React.ReactNode;
        if (a.check === "manual_report") text = <>Someone reported: <b>“{a.description}”</b></>;
        else if (a.comparison === "no_data") text = <><b>{name}</b>: no data (the service went silent)</>;
        else if (a.comparison === "error") text = <><b>{name}</b> could not be checked</>;
        else if (a.comparison === "x_normal") text = <><b>{name}</b> was {fmt(a.value)}, {a.description.split(" over")[0]}</>;
        else text = <><b>{name}</b> was {fmt(a.value)} (limit {a.comparison} {fmt(a.threshold)})</>;
        return <li key={a.check}>{text}</li>;
      })}
    </ul>
  );
}

function Decision({ incident }: { incident: Incident }) {
  const [busy, setBusy] = useState<"approve" | "dismiss" | null>(null);
  async function act(kind: "approve" | "dismiss") {
    setBusy(kind);
    try {
      if (kind === "approve") {
        const { receipts } = await api.approve(incident.id);
        const failed = Object.entries(receipts).filter(([, v]) => v.startsWith("FAILED"));
        if (failed.length) toast.error(`Sent with ${failed.length} failure(s)`, { description: failed.map(([k]) => k).join(", ") });
        else toast.success("Report sent", { description: Object.keys(receipts).join(" · ") });
      } else {
        await api.dismiss(incident.id);
        toast("Dismissed", { description: "Nothing was sent. Memory learns the diagnosis was off." });
      }
      refreshMemory();
    } catch (e) {
      toast.error("Action failed", { description: String(e) });
    } finally {
      setBusy(null);
    }
  }
  return (
    <div className="decision">
      <button className="btn btn-primary" disabled={!!busy} onClick={() => act("approve")}>
        {busy === "approve" ? "Sending…" : "Approve & send report"}
      </button>
      <button className="btn" disabled={!!busy} onClick={() => act("dismiss")}>
        Dismiss
      </button>
      <span className="small muted">Nothing is sent until you approve.</span>
    </div>
  );
}

function Story({ incident }: { incident: Incident }) {
  const events = useStore((s) => s.events);
  const { memories, steps, thinking } = buildStory(events, incident);
  const live = incident.status === "investigating";
  return (
    <details className="story" open={live}>
      <summary>
        How Overwatch figured it out
        <span className="muted"> · {steps.length} {steps.length === 1 ? "step" : "steps"}</span>
      </summary>
      <ol className="story-list">
        {memories && (
          <li className="story-item story-memory">
            <div className="story-who">Historian</div>
            <p>
              {memories.length
                ? `Found ${memories.length} similar past ${memories.length === 1 ? "incident" : "incidents"} in memory.`
                : "Checked memory: nothing similar has happened before."}
            </p>
            {memories.length > 0 && (
              <ul className="memories">
                {memories.map((m) => (
                  <li key={m.id} className={clsx(m.used && "memory-used")}>
                    {m.content}
                    <span className="memory-meta">
                      {m.id} · match {Math.round(m.relevance * 100)}%{m.used && " · used in the diagnosis"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </li>
        )}
        {steps.map((s) => (
          <li key={s.step} className={clsx("story-item", s.query?.error && "story-error")}>
            <div className="story-who">Investigator · step {s.step}</div>
            {s.thought && <p>{s.thought}</p>}
            {s.query && (
              <div className="story-query">
                <span>
                  Looked at <b>{s.query.purpose || "the data"}</b> in {s.query.source}
                  {s.query.key && <span className="key-badge">key evidence</span>}
                </span>
                <span className="muted small">
                  {!s.query.done ? <span className="shimmer">running…</span> : s.query.error ? <span className="err">failed: {s.query.error}</span> : `${s.query.count} ${s.query.count === 1 ? "row" : "rows"} · ${s.query.ms} ms`}
                </span>
                <details className="query-details">
                  <summary>Show query and results</summary>
                  <pre className="sql">{s.query.sql.trim()}</pre>
                  <ResultTable rows={s.query.rows} />
                </details>
              </div>
            )}
          </li>
        ))}
        {thinking !== null && (
          <li className="story-item">
            <div className="story-who">Investigator · step {thinking}</div>
            <span className="shimmer">Thinking about what to check next…</span>
          </li>
        )}
        {live && steps.length === 0 && thinking === null && !memories && (
          <li className="story-item"><span className="shimmer">Starting…</span></li>
        )}
      </ol>
    </details>
  );
}

export function IncidentView({ incident }: { incident: Incident }) {
  const events = useStore((s) => s.events);
  const chip = incident.recovered_at ? { label: "Recovered", tone: "ok" } : STATUS_CHIP[incident.status];
  const blast = "started_at" in incident.blast_radius ? (incident.blast_radius as BlastRadius) : null;
  const live = !incident.recovered_at && ["investigating", "awaiting_approval", "reported"].includes(incident.status);
  const failed = incident.status === "failed";

  return (
    <section className="card incident" aria-label="Incident">
      <header className="incident-head">
        <div>
          <h2 className="incident-title">{incidentHeadline(incident)}</h2>
          <div className="small muted">
            {incident.services.filter((s) => s !== "unknown").map((s) => `${s} · `).join("")}opened {clock(Date.parse(incident.opened_at) / 1000)}
            {incident.recovered_at && ` · recovered ${clock(Date.parse(incident.recovered_at) / 1000)}`} · #{incident.id}
          </div>
        </div>
        <span className={clsx("chip", `chip-${chip.tone}`)}>{chip.label}</span>
      </header>

      <div className="summary-grid">
        <div className="summary-block">
          <h3>What happened</h3>
          <Detected incident={incident} />
        </div>
        <div className="summary-block summary-why">
          <h3>Why</h3>
          {incident.status === "investigating" ? (
            <p className="shimmer">{currentActivity(events, incident)}…</p>
          ) : failed ? (
            <p className="err">{incident.root_cause.replace(/^Investigation failed: /, "The investigation could not finish: ")}</p>
          ) : (
            <>
              <p className="root-cause">{incident.root_cause}</p>
              <div className="confidence" title="How sure the Investigator is">
                <span className="confidence-bar"><span style={{ transform: `scaleX(${incident.confidence})` }} /></span>
                {Math.round(incident.confidence * 100)}% sure
              </div>
            </>
          )}
        </div>
        <div className="summary-block">
          <h3>Impact</h3>
          {blast ? <Impact blast={blast} live={live} /> : <p className="muted small">Measured when the investigation finishes.</p>}
        </div>
        <div className="summary-block">
          <h3>What to do</h3>
          {incident.recommended_actions.length ? (
            <ol className="actions-list">{incident.recommended_actions.map((a, i) => <li key={i}>{a}</li>)}</ol>
          ) : (
            <p className="muted small">{failed ? "Check that the AI provider is reachable, then report the problem again." : "Suggestions appear with the diagnosis."}</p>
          )}
        </div>
      </div>

      {incident.status === "awaiting_approval" && <Decision incident={incident} />}
      {!incident.recovered_at && incident.status !== "investigating" && incident.status !== "dismissed" && (
        <div className="resolve-row">
          <button
            className="btn"
            onClick={async () => {
              await api.resolve(incident.id);
              toast.success("Marked resolved");
            }}
          >
            Mark resolved
          </button>
          <span className="small muted">
            {incident.anomalies.every((a) => a.check === "manual_report")
              ? "Reported problems close when you say they are fixed."
              : "Closes automatically once its checks stay healthy; use this if you know it is fixed."}
          </span>
        </div>
      )}
      {incident.status === "reported" && <p className="status-note ok">Report sent and remembered.</p>}
      {incident.status === "dismissed" && <p className="status-note muted">Dismissed. Nothing was sent.</p>}

      <Story incident={incident} />
    </section>
  );
}
