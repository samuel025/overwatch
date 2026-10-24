import clsx from "clsx";
import { currentActivity } from "../activity";
import { alertLine, clock, formatCheckValue, humanize, incidentHeadline, timeAgo } from "../format";
import { focusIncident } from "../health";
import { agentStates, useStore } from "../store";
import type { AgentName, Check, IncidentStatus } from "../types";

const STATUS: Record<IncidentStatus, string> = {
  investigating: "Investigating",
  awaiting_approval: "Needs approval",
  reported: "Reported",
  dismissed: "Dismissed",
  failed: "Investigation failed",
};

const AGENTS: { name: AgentName; title: string; idle: string }[] = [
  { name: "watcher", title: "Watcher", idle: "Checking the signals every few seconds" },
  { name: "historian", title: "Historian", idle: "Remembers past incidents (H-MEM)" },
  { name: "investigator", title: "Investigator", idle: "Finds the root cause (Nemotron via Nightshift)" },
  { name: "responder", title: "Responder", idle: "Sends the report after you approve" },
];

export function Agents() {
  const events = useStore((s) => s.events);
  const lastTick = useStore((s) => s.lastTick);
  const order = useStore((s) => s.order);
  const incidents = useStore((s) => s.incidents);
  const checks = useStore((s) => s.checks);
  const focus = focusIncident(order, incidents);
  const states = agentStates(events, focus, lastTick);

  const say = (a: AgentName): string => {
    const st = states[a];
    if (a === "watcher") return `Watching ${checks.length} checks${lastTick ? ` · ${timeAgo(lastTick)}` : ""}`;
    if (!focus) return AGENTS.find((x) => x.name === a)!.idle;
    if (a === "investigator" && st === "working") return currentActivity(events, focus);
    if (a === "investigator" && st === "failed") return "Could not finish (AI provider unreachable?)";
    if (a === "responder" && st === "waiting") return "Waiting for your approval";
    if (st === "done") {
      if (a === "historian") return `Checked memory: ${focus.similar_incidents.filter((m) => m.id !== "-").length} similar`;
      return a === "responder" ? "Report sent" : "Done";
    }
    if (st === "working") return "Working…";
    return AGENTS.find((x) => x.name === a)!.idle;
  };

  return (
    <section className="card" aria-label="Agents">
      <h2 className="card-title">Agents</h2>
      <ol className="agents">
        {AGENTS.map((a) => (
          <li key={a.name} className={clsx("agent", `agent-${states[a.name]}`)}>
            <span className="agent-dot" aria-hidden />
            <div>
              <div className="agent-name">{a.title}</div>
              <div className="agent-say">{say(a.name)}</div>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Sparkline({ check }: { check: Check }) {
  const points = useStore((s) => s.history[check.name]) ?? [];
  const recent = points.slice(-60);
  if (recent.length < 2) return <svg className="spark" aria-hidden />;
  const line = alertLine(check);
  const values = recent.map((p) => p.value);
  const max = Math.max(...values, line ?? 0) || 1;
  const min = Math.min(...values, 0);
  const x = (i: number) => (i / (recent.length - 1)) * 100;
  const y = (v: number) => 22 - ((v - min) / (max - min || 1)) * 20;
  return (
    <svg className={clsx("spark", check.failing && "spark-bad")} viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden>
      {line !== null && <line className="spark-limit" x1="0" x2="100" y1={y(line)} y2={y(line)} />}
      <polyline points={recent.map((p, i) => `${x(i)},${y(p.value)}`).join(" ")} />
    </svg>
  );
}

export function Signals() {
  const checks = useStore((s) => s.checks);
  const byService = new Map<string, Check[]>();
  checks.forEach((c) => byService.set(c.service, [...(byService.get(c.service) ?? []), c]));
  return (
    <section className="card" aria-label="Signals">
      <h2 className="card-title">
        Signals <span className="muted small">{checks.filter((c) => c.failing).length} over the limit</span>
      </h2>
      {[...byService.entries()].map(([service, list]) => (
        <div key={service} className="signal-group">
          <div className="signal-service">{service}</div>
          {list.map((c) => (
            <div
              key={c.name}
              className={clsx("signal-row", c.failing && "signal-row-bad", c.suspected && "signal-row-suspected")}
              title={c.description}
            >
              <span className="signal-label">{humanize(c.name).replace(new RegExp(`^${service}\\s*`, "i"), "") || c.name}</span>
              <Sparkline check={c} />
              <span className="signal-value">
                {c.kind === "heartbeat" ? (c.failing ? "silent" : "ok")
                  : c.kind === "probe" ? (c.value === 1 ? "up" : c.value === 0 ? "down" : "n/a")
                    : formatCheckValue(c, c.value)}
              </span>
            </div>
          ))}
        </div>
      ))}
    </section>
  );
}

export function Incidents() {
  const order = useStore((s) => s.order);
  const incidents = useStore((s) => s.incidents);
  const selected = useStore((s) => s.selected);
  const follow = useStore((s) => s.follow);
  const select = useStore((s) => s.select);
  const followLive = useStore((s) => s.followLive);
  return (
    <section className="card" aria-label="Incident history">
      <h2 className="card-title">
        Incidents <span className="muted small">{order.length}</span>
        {!follow && (
          <button className="btn btn-small" onClick={followLive}>
            Back to live
          </button>
        )}
      </h2>
      {order.length === 0 && <p className="empty">None yet.</p>}
      <ul className="incident-list">
        {order.slice(0, 12).map((id) => {
          const i = incidents[id];
          return (
            <li key={id}>
              <button
                className={clsx("incident-row", !follow && id === selected && "incident-row-on")}
                onClick={() => select(id)}
              >
                <span className={clsx("status-dot", i.recovered_at ? "st-recovered" : `st-${i.status}`)} />
                <span className="incident-row-text">
                  <span className="incident-row-title">{incidentHeadline(i)}</span>
                  <span className="muted small">
                    {i.recovered_at ? (i.status === "awaiting_approval" ? "Recovered · diagnosis waiting for you" : "Recovered") : STATUS[i.status]} · {timeAgo(i.opened_at)}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function UnderTheHood() {
  const memory = useStore((s) => s.memory);
  const calls = useStore((s) => s.llmCalls);
  const status = useStore((s) => s.status);
  return (
    <section className="card" aria-label="Under the hood">
      <details>
        <summary className="card-title">Under the hood</summary>
        <div className="hood">
          <div className="hood-block">
            <div className="sub-head">Data sources</div>
            <p className="small">{status?.sources.map((s) => `${s.name} (${s.type})`).join(", ")}</p>
          </div>
          <div className="hood-block">
            <div className="sub-head">
              Memory · {memory?.health.ok ? `H-MEM, ${memory.health.memories ?? 0} stored` : `${memory?.health.engine ?? "?"} offline`}
            </div>
            <ul className="mem-list">
              {memory?.memories.slice(0, 4).map((m) => (
                <li key={m.id} className="mem-row">
                  <span className="mem-id">{m.id}</span>
                  <span className="mem-content">{m.content}</span>
                  <span className="mem-conf">{Math.round(m.confidence * 100)}%</span>
                </li>
              ))}
            </ul>
            {memory && memory.ledger.length > 0 && (
              <ul className="ledger">
                {memory.ledger.slice(0, 4).map((e, i) => (
                  <li key={i}>
                    <span className={clsx("ledger-action", `la-${e.action}`)}>{e.action}</span>
                    <span className="mem-id">{e.memoryId}</span>
                    <span className="muted small">{clock(e.timestamp / 1000)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="hood-block">
            <div className="sub-head">AI calls · {status?.model}</div>
            {calls.length === 0 && <p className="empty">No calls yet.</p>}
            <ul className="calls">
              {calls.slice(0, 6).map((c) => (
                <li key={c.id} className="call">
                  <span className="call-agent">{c.agent}</span>
                  <span className="call-num">{c.seconds.toFixed(1)}s</span>
                  <span className="call-num">{c.usage?.total_tokens ? `${c.usage.total_tokens} tok` : ""}</span>
                </li>
              ))}
            </ul>
            <p className="small muted">via {status?.provider}</p>
          </div>
        </div>
      </details>
    </section>
  );
}
