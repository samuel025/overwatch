import { formatCheckValue, humanize } from "./format";
import type { BlastRadius, Check, Incident } from "./types";

export type NodeState = "healthy" | "degraded" | "down" | "unknown";
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

export function nodeHealth(node: string, checks: Check[], blast?: BlastRadius | null): NodeHealth {
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
  if (!mine.length) return { state: "unknown", role, problems: [], metrics: [] };

  const silent = mine.filter((c) => c.failing && (c.kind === "heartbeat" || c.value === null));
  const failing = mine.filter((c) => c.failing);
  const state: NodeState = silent.length ? "down" : failing.length ? "degraded" : "healthy";
  const problems = silent.length
    ? ["Not responding: no data reported"]
    : failing.map((c) => `${shortLabel(c)} ${formatCheckValue(c, c.value)}`);
  const metrics = mine
    .filter((c) => c.kind !== "heartbeat")
    .slice(0, 2)
    .map((c) => ({ label: shortLabel(c), value: formatCheckValue(c, c.value), bad: c.failing }));
  return { state, role, problems, metrics };
}

/** The incident the whole console should be about right now: newest one that is still ongoing. */
export function focusIncident(order: string[], incidents: Record<string, Incident>): Incident | undefined {
  return order.map((id) => incidents[id]).find((i) => i && !i.recovered_at && i.status !== "dismissed");
}

export const STATE_LABEL: Record<NodeState, string> = {
  healthy: "Healthy",
  degraded: "Having problems",
  down: "Not responding",
  unknown: "No data",
};
