import { formatCheckValue, humanize } from "./format";
import type { BlastRadius, Check, DownMap, Incident } from "./types";

export type NodeState = "healthy" | "suspected" | "degraded" | "down" | "unknown";
export type NodeRole = "cause" | "affected" | "risk" | null;

export interface NodeHealth {
  state: NodeState;
  role: NodeRole;
  problems: string[]; // plain-language reasons
  metrics: { label: string; value: string; bad: boolean }[];
}

/** Short label for a check on a node card: "p95", "errors", "cpu". */
function shortLabel(c: Check) {
  const words = humanize(c.name)
    .replace(new RegExp(`^${c.service}\\s*`, "i"), "")
    .replace(/^database\s+/i, "")
    .toLowerCase();
  if (/p9\d/.test(words)) return words.match(/p9\d/)![0];
  if (/error/.test(words)) return "errors";
  if (/cpu/.test(words)) return "cpu";
  if (/restart/.test(words)) return "restarts";
  return words.trim() || c.name;
}

export function nodeHealth(node: string, checks: Check[], blast?: BlastRadius | null, down: DownMap = {}): NodeHealth {
  const mine = checks.filter((c) => c.service === node);
  const role: NodeRole = blast
    ? blast.origin === node
      ? "cause"
      : blast.affected.includes(node)
        ? "affected"
        : blast.at_risk.includes(node)
          ? "risk"
          : null
    : null;
  const events = Object.values(down).filter((d) => d.service === node);
  if (!mine.length && !events.length) return { state: "unknown", role, problems: [], metrics: [] };

  const silent = mine.filter((c) => c.failing && (c.kind !== "metric" || c.value === null));
  const failing = mine.filter((c) => c.failing);
  const suspected = mine.filter((c) => c.suspected);
  const isDown = events.length > 0 || silent.length > 0;
  const state: NodeState = isDown ? "down" : failing.length ? "degraded" : suspected.length ? "suspected" : "healthy";
  const problems = events.length
    ? events.map((d) => d.detail)
    : silent.length
      ? ["Not responding: no data reported"]
      : [...failing, ...suspected].map((c) => `${shortLabel(c)} ${formatCheckValue(c, c.value)}`);
  const metrics = mine
    .filter((c) => c.kind === "metric")
    .slice(0, 2)
    .map((c) => ({ label: shortLabel(c), value: formatCheckValue(c, c.value), bad: c.failing || c.suspected }));
  return { state, role, problems, metrics };
}

/** The incident the whole console should be about right now: newest one that is still ongoing. */
export function focusIncident(order: string[], incidents: Record<string, Incident>): Incident | undefined {
  return order.map((id) => incidents[id]).find((i) => i && !i.recovered_at && i.status !== "dismissed");
}

export const STATE_LABEL: Record<NodeState, string> = {
  healthy: "Healthy",
  suspected: "Possible problem",
  degraded: "Having problems",
  down: "Not responding",
  unknown: "No data",
};

/** Seconds left before a recovering incident closes, or null when it is not recovering. */
export function recoverySecondsLeft(i: Incident, now = Date.now() / 1000): number | null {
  if (i.recovered_at || !i.healthy_since || !i.recover_wait) return null;
  return Math.max(0, Math.ceil(i.recover_wait - (now - i.healthy_since)));
}

/** Incidents whose problem has gone but whose diagnosis still waits for a person to approve or dismiss it. */
export function awaitingAfterRecovery(order: string[], incidents: Record<string, Incident>): Incident[] {
  return order.map((id) => incidents[id]).filter((i): i is Incident => !!i && !!i.recovered_at && i.status === "awaiting_approval");
}
