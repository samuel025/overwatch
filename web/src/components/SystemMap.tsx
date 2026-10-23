import clsx from "clsx";
import { nodeHealth, STATE_LABEL, type NodeHealth } from "../health";
import { useStore } from "../store";
import type { BlastRadius } from "../types";

const W = 196; // node card
const H = 84;
const COL_GAP = 84;
const ROW_GAP = 22;

/** Columns by call depth: entry points on the left, what they depend on to the right. */
function layout(topology: Record<string, string[]>) {
  const nodes = new Set<string>(Object.keys(topology));
  Object.values(topology).forEach((deps) => deps.forEach((d) => nodes.add(d)));
  const called = new Set(Object.values(topology).flat());
  const depth = new Map<string, number>();
  const visit = (n: string, d: number, seen: Set<string>) => {
    if (seen.has(n)) return;
    depth.set(n, Math.max(depth.get(n) ?? 0, d));
    (topology[n] ?? []).forEach((c) => visit(c, d + 1, new Set([...seen, n])));
  };
  [...nodes].filter((n) => !called.has(n)).forEach((root) => visit(root, 0, new Set()));
  nodes.forEach((n) => depth.has(n) || depth.set(n, 0));

  const cols: string[][] = [];
  depth.forEach((d, n) => (cols[d] ??= []).push(n));
  cols.forEach((c) => c.sort());
  const tallest = Math.max(...cols.map((c) => c.length));
  const height = tallest * H + (tallest - 1) * ROW_GAP;
  const pos = new Map<string, { x: number; y: number }>();
  cols.forEach((col, ci) => {
    const colH = col.length * H + (col.length - 1) * ROW_GAP;
    col.forEach((n, ri) => pos.set(n, { x: ci * (W + COL_GAP), y: (height - colH) / 2 + ri * (H + ROW_GAP) }));
  });
  return { pos, width: cols.length * W + (cols.length - 1) * COL_GAP, height };
}

const ROLE_TAG = { cause: "Likely cause", affected: "Affected", risk: "At risk" } as const;

function NodeCard({ name, h, x, y }: { name: string; h: NodeHealth; x: number; y: number }) {
  const external = name.includes(".");
  const tag = h.role ? ROLE_TAG[h.role] : null;
  return (
    <div
      className={clsx("node", `node-${h.state}`, h.role && `node-role-${h.role}`, external && "node-external")}
      style={{ transform: `translate(${x}px, ${y}px)`, width: W, height: H }}
      title={h.problems.join("\n") || STATE_LABEL[h.state]}
    >
      <div className="node-top">
        <span className="node-name">{name}</span>
        {tag && <span className="node-tag">{tag}</span>}
      </div>
      <div className="node-state">
        <span className="node-dot" />
        {STATE_LABEL[h.state]}
        {external && <span className="node-ext">external</span>}
      </div>
      <div className="node-metrics">
        {h.state === "down"
          ? h.problems[0]?.replace(/^container \S+ /, "") ?? "No data reported"
          : h.metrics.map((m) => (
              <span key={m.label} className={clsx(m.bad && "node-metric-bad")}>
                {m.label} <b>{m.value}</b>
              </span>
            ))}
      </div>
    </div>
  );
}

export function SystemMap({ blast, preview }: { blast?: BlastRadius | null; preview?: Record<string, string[]> }) {
  const liveTopology = useStore((s) => s.status?.topology);
  const liveChecks = useStore((s) => s.checks);
  const liveDown = useStore((s) => s.down);
  // In preview mode (setup page) we only know the structure, not the health.
  const topology = preview ?? liveTopology;
  const checks = preview ? [] : liveChecks;
  const down = preview ? {} : liveDown;
  if (!topology || !Object.keys(topology).length) {
    return <p className="empty">Add a <code>topology:</code> section to overwatch.yaml to see the system map.</p>;
  }

  const { pos, width, height } = layout(topology);
  const health = new Map([...pos.keys()].map((n) => [n, nodeHealth(n, checks, blast, down)]));
  const hot = new Set((blast?.edges ?? []).map((e) => `${e.from}>${e.to}`));
  const broken = (n: string) => ["down", "degraded"].includes(health.get(n)!.state);

  return (
    <div className="map-wrap">
      <div className="map-canvas" style={{ width, height }}>
        <svg className="map-edges" width={width} height={height} aria-hidden>
          {Object.entries(topology).flatMap(([from, deps]) =>
            deps.map((to) => {
              const a = pos.get(from)!;
              const b = pos.get(to)!;
              const x1 = a.x + W;
              const y1 = a.y + H / 2;
              const x2 = b.x;
              const y2 = b.y + H / 2;
              const mid = (x1 + x2) / 2;
              const failing = hot.has(`${from}>${to}`) || (broken(to) && broken(from));
              const down = health.get(to)!.state === "down";
              return (
                <path
                  key={`${from}>${to}`}
                  className={clsx("edge", failing && "edge-failing", down && "edge-down")}
                  d={`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`}
                />
              );
            }),
          )}
        </svg>
        {[...pos.entries()].map(([n, p]) => (
          <NodeCard key={n} name={n} h={health.get(n)!} x={p.x} y={p.y} />
        ))}
      </div>
    </div>
  );
}

export function MapLegend() {
  return (
    <div className="map-legend">
      <span><i className="lg lg-healthy" /> Healthy</span>
      <span><i className="lg lg-suspected" /> Possible problem</span>
      <span><i className="lg lg-degraded" /> Having problems</span>
      <span><i className="lg lg-down" /> Not responding</span>
      <span><i className="lg lg-risk" /> At risk</span>
      <span><i className="lg lg-edge" /> Calls · red = failing path</span>
    </div>
  );
}
