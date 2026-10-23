import type { Incident, OwEvent } from "./types";

export function eventsFor(events: OwEvent[], incidentId: string) {
  return events.filter(
    (e) => "incident" in e && (typeof e.incident === "string" ? e.incident : e.incident.id) === incidentId,
  );
}

/** One plain sentence about what the agents are doing on this incident right now. */
export function currentActivity(events: OwEvent[], incident: Incident): string {
  if (incident.status !== "investigating") return "";
  const mine = eventsFor(events, incident.id);
  const last = [...mine].reverse().find((e) =>
    ["agent.working", "memory.recalled", "agent.thinking", "investigator.thought", "query.started", "query.finished"].includes(e.type),
  );
  switch (last?.type) {
    case "agent.working":
      return last.agent === "historian" ? "Checking memory for similar past incidents" : "Sending the report";
    case "memory.recalled":
    case "agent.thinking":
      return `Thinking about what to check next (step ${last.type === "agent.thinking" ? last.step : 1})`;
    case "investigator.thought":
    case "query.finished":
      return `Reading the results (step ${last.step})`;
    case "query.started":
      return `Checking ${last.purpose || "the data"} (step ${last.step})`;
    default:
      return "Starting the investigation";
  }
}

export function firstSentence(text: string) {
  const m = text.match(/^(.+?[.!?])(\s|$)/);
  return (m ? m[1] : text).trim();
}
