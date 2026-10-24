import clsx from "clsx";
import { currentActivity, firstSentence } from "../activity";
import { incidentHeadline, timeAgo } from "../format";
import { awaitingAfterRecovery, focusIncident, recoverySecondsLeft } from "../health";
import { useStore } from "../store";

type Tone = "ok" | "warn" | "bad" | "info" | "muted";

export function StatusBanner() {
  const connected = useStore((s) => s.connected);
  const checks = useStore((s) => s.checks);
  const order = useStore((s) => s.order);
  const incidents = useStore((s) => s.incidents);
  const events = useStore((s) => s.events);
  const lastTick = useStore((s) => s.lastTick);
  const followLive = useStore((s) => s.followLive);
  const follow = useStore((s) => s.follow);
  const topology = useStore((s) => s.status?.topology);

  const select = useStore((s) => s.select);
  const focus = focusIncident(order, incidents);
  const waiting = awaitingAfterRecovery(order, incidents);
  const recovering = focus ? recoverySecondsLeft(focus) : null;
  const blind = checks.length > 0 && checks.every((c) => c.failing && c.value === null);
  const breached = checks.filter((c) => c.failing);
  const services = topology ? new Set([...Object.keys(topology), ...Object.values(topology).flat()]).size : 0;

  let tone: Tone = "ok";
  let title = "All systems normal";
  let detail = `Watching ${services || checks.length} ${services ? "services" : "checks"}${lastTick ? ` · last check ${timeAgo(lastTick)}` : ""}`;
  let action: { label: string } | null = null;
  let pick: string | null = null;  // open this incident instead of following the live one

  if (!connected) {
    tone = "muted";
    title = "Reconnecting to Overwatch…";
    detail = "The console will catch up automatically.";
  } else if (blind) {
    tone = "bad";
    title = "Overwatch can't read the monitoring data";
    detail = "Every check is failing to run. The database or the data source may be down.";
  } else if (focus?.status === "investigating" && focus.hypothesis && "text" in focus.hypothesis) {
    tone = "warn";
    title = `Likely: ${firstSentence(focus.hypothesis.text)}`;
    detail = `First guess (${Math.round(focus.hypothesis.confidence * 100)}% sure, ${focus.hypothesis.seconds}s) · ${currentActivity(events, focus)}`;
    action = { label: "Show it" };
  } else if (focus?.status === "investigating") {
    tone = "warn";
    title = `${incidentHeadline(focus)}: investigating`;
    detail = currentActivity(events, focus);
    action = { label: "Show it" };
  } else if (focus?.status === "awaiting_approval") {
    tone = "info";
    title = `Diagnosis ready: ${firstSentence(focus.root_cause)}`;
    detail = `${Math.round(focus.confidence * 100)}% confidence · waiting for your approval`;
    action = { label: "Show it" };
  } else if (focus?.status === "failed") {
    tone = "bad";
    title = `${incidentHeadline(focus)}: problem detected`;
    detail = "The automatic investigation could not finish (is the AI provider reachable?).";
    action = { label: "Show it" };
  } else if (focus?.status === "reported") {
    tone = "warn";
    title = `${incidentHeadline(focus)}: reported, not recovered yet`;
    detail = firstSentence(focus.root_cause);
    action = { label: "Show it" };
  } else if (waiting.length) {
    tone = "info";
    title = "All systems normal";
    detail = `${waiting.length === 1 ? "1 recovered incident still has" : `${waiting.length} recovered incidents still have`} a diagnosis waiting for your approval`;
    action = { label: "Review it" };
    pick = waiting[0].id;
  } else if (breached.length) {
    tone = "warn";
    title = `${breached.length} ${breached.length === 1 ? "check is" : "checks are"} over the limit`;
    detail = "An incident opens automatically if it is new.";
  }

  if (recovering !== null && focus) detail += ` · looks healthy again, confirming (${recovering}s left)`;

  return (
    <section className={clsx("banner", `banner-${tone}`)} aria-live="polite">
      <span className="banner-dot" aria-hidden />
      <div className="banner-text">
        <div className="banner-title">{title}</div>
        <div className="banner-detail">{detail}</div>
      </div>
      {action && (pick || !follow) && (
        <button className="btn" onClick={() => (pick ? select(pick) : followLive())}>
          {action.label}
        </button>
      )}
    </section>
  );
}
