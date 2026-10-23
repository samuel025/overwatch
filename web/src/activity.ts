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
    ["agent.working", "memory.recalled", "agent.thinking", "investigator.thought", "query.started", "query.finished",
      "investigator.hypothesis"].includes(e.type),
  );
  switch (last?.type) {
    case "agent.working":
      if (last.agent === "evidence") return "Gathering the evidence pack";
      return last.agent === "historian" ? "Checking memory for similar past incidents" : "Sending the report";
    case "memory.recalled":
      return "Thinking";
    case "agent.thinking":
      return last.step === 0 ? "Making a quick first guess" : `Confirming with a deeper look (step ${last.step})`;
    case "investigator.hypothesis":
      return "First guess ready; confirming it with data";
    case "investigator.thought":
    case "query.finished":
      return last.step === 0 ? "Evidence pack gathered" : `Reading the results (step ${last.step})`;
    case "query.started":
      return last.step === 0 ? "Gathering the evidence pack" : `Checking ${last.purpose || "the data"} (step ${last.step})`;
    default:
      return "Starting the investigation";
  }
}

export function firstSentence(text: string) {
  const m = text.match(/^(.+?[.!?])(\s|$)/);
  return (m ? m[1] : text).trim();
}
