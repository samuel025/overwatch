import type { Check } from "./types";

/** Rates (thresholds below 1) read better as percentages; everything else keeps its unit from the name. */
export function isRate(check: { threshold: number | null; name: string }) {
  return (check.threshold !== null && check.threshold < 1) || /rate|ratio/.test(check.name);
}

export function unit(check: { threshold: number | null; name: string }) {
  if (isRate(check)) return "%";
  if (/_ms\b|latency/.test(check.name)) return "ms";
  if (/_pct\b|percent/.test(check.name)) return "%";
  return "";
}

export function formatCheckValue(check: { threshold: number | null; name: string }, v: number | null) {
  if (v === null || Number.isNaN(v)) return "n/a";
  if (isRate(check)) return `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`;
  const u = unit(check);
  return `${v >= 100 ? Math.round(v) : Number.isInteger(v) ? v : v.toFixed(1)}${u === "ms" ? " ms" : u === "%" ? "%" : ""}`;
}

/** The relative limit in force right now: factor x the check's normal, but never below relative_min. */
export function relativeLine(check: Check): number | null {
  if (!check.relative_factor || !check.baseline) return null;
  return Math.max(check.relative_factor * check.baseline, check.relative_min ?? 0);
}

/** The line a check alerts above, matching the Watcher: the lower of its fixed and relative limits. */
export function alertLine(check: Check): number | null {
  const lines = [check.threshold, relativeLine(check)].filter((x): x is number => x !== null);
  return lines.length ? Math.min(...lines) : null;
}

export function breached(check: Check) {
  const v = check.value;
  if (v === null) return null;
  if (check.threshold !== null && (check.comparison === "<" || check.comparison === "<=")) {
    return check.comparison === "<" ? v < check.threshold : v <= check.threshold;
  }
  const line = alertLine(check);
  if (line === null) return false;
  return check.comparison === ">=" && line === check.threshold ? v >= line : v > line;
}

export function humanize(name: string) {
  const words = name.replace(/_(ms|pct|percent)$/, "").replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** What broke, in a few words: the headline for an incident. Root cause lives in the diagnosis card. */
export function incidentHeadline(inc: { services: string[]; anomalies: { check: string; description: string }[] }) {
  const first = inc.anomalies[0];
  if (!first) return inc.services.join(", ");
  if (first.check === "manual_report") return `Reported: “${first.description}”`;
  const more = inc.anomalies.length > 1 ? ` +${inc.anomalies.length - 1} more` : "";
  return `${humanize(first.check)}${more}`;
}

export function timeAgo(iso: string | number, now = Date.now()) {
  const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export function clock(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function duration(seconds: number) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}
