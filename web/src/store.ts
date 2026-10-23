import { create } from "zustand";
import type { AgentName, AgentState, Check, Incident, MemoryOverview, OwEvent, Status } from "./types";

type Point = { time: number; value: number };
type LlmCall = Extract<OwEvent, { type: "llm.call" }>;

interface Store {
  status: Status | null;
  connected: boolean;
  checks: Check[];
  history: Record<string, Point[]>;
  lastTick: number | null;
  incidents: Record<string, Incident>;
  order: string[]; // incident ids, newest first
  selected: string | null;
  follow: boolean; // auto-select new incidents until the user picks one
  events: OwEvent[];
  llmCalls: LlmCall[];
  memory: MemoryOverview | null;
  hydrate: (s: Status) => void;
  apply: (e: OwEvent) => void;
  select: (id: string) => void;
  followLive: () => void;
  setConnected: (c: boolean) => void;
  setMemory: (m: MemoryOverview) => void;
}

const HISTORY_SECONDS = 15 * 60;

function upsert(state: Store, inc: Incident) {
  const order = state.order.includes(inc.id) ? state.order : [inc.id, ...state.order];
  return { incidents: { ...state.incidents, [inc.id]: inc }, order };
}

export const useStore = create<Store>((set) => ({
  status: null,
  connected: false,
  checks: [],
  history: {},
  lastTick: null,
  incidents: {},
  order: [],
  selected: null,
  follow: true,
  events: [],
  llmCalls: [],
  memory: null,

  hydrate: (s) =>
    set((state) => {
      const history: Record<string, Point[]> = {};
      for (const [name, pts] of Object.entries(s.history)) {
        history[name] = pts.filter(([, v]) => v !== null).map(([time, value]) => ({ time, value: value as number }));
      }
      const incidents = Object.fromEntries(s.incidents.map((i) => [i.id, i]));
      const order = s.incidents.map((i) => i.id);
      return {
        status: s,
        events: [], // the SSE backlog replays right after a (re)connect
        llmCalls: [],
        checks: s.checks,
        history,
        incidents,
        order,
        selected: state.selected ?? order[0] ?? null,
      };
    }),

  apply: (e) =>
    set((state) => {
      if (state.events.length && e.id <= state.events[state.events.length - 1].id) return {}; // replayed duplicate
      const events = [...state.events.slice(-1500), e];
      switch (e.type) {
        case "watch.tick": {
          const history = { ...state.history };
          for (const [name, value] of Object.entries(e.values)) {
            if (value === null) continue;
            const prev = history[name] ?? [];
            if (prev.length && prev[prev.length - 1].time >= e.ts) continue; // already in the hydrated history
            const cutoff = e.ts - HISTORY_SECONDS;
            history[name] = [...prev.filter((p) => p.time > cutoff), { time: e.ts, value }];
          }
          const failing = new Set(e.failing);
          const checks = state.checks.map((c) =>
            c.name in e.values ? { ...c, value: e.values[c.name], failing: failing.has(c.name) } : c,
          );
          return { events, history, checks, lastTick: e.ts };
        }
        case "incident.opened":
          return {
            events,
            ...upsert(state, e.incident),
            selected: state.follow || !state.selected ? e.incident.id : state.selected,
          };
        case "incident.updated":
          return { events, ...upsert(state, e.incident) };
        case "blast_radius.updated": {
          const inc = state.incidents[e.incident];
          if (!inc) return { events };
          return { events, incidents: { ...state.incidents, [inc.id]: { ...inc, blast_radius: e.blast_radius } } };
        }
        case "llm.call":
          return { events, llmCalls: [e, ...state.llmCalls].slice(0, 40) };
        default:
          return { events };
      }
    }),

  select: (id) => set({ selected: id, follow: false }),
  followLive: () => set({ follow: true }),
  setConnected: (connected) => set({ connected }),
  setMemory: (memory) => set({ memory }),
}));

/** Where each agent is for the selected incident, derived from the event stream. */
export function agentStates(events: OwEvent[], incident: Incident | undefined, lastTick: number | null): Record<AgentName, AgentState> {
  const states: Record<AgentName, AgentState> = {
    watcher: lastTick ? "done" : "idle",
    historian: "idle",
    investigator: "idle",
    responder: "idle",
  };
  if (!incident) return states;
  for (const e of events) {
    if (!("incident" in e)) continue;
    const id = typeof e.incident === "string" ? e.incident : e.incident.id;
    if (id !== incident.id) continue;
    switch (e.type) {
      case "incident.opened":
        states.watcher = "done";
        break;
      case "agent.working":
        states[e.agent] = "working";
        break;
      case "memory.recalled":
        states.historian = "done";
        break;
      case "agent.thinking":
      case "query.started":
        states.investigator = "working";
        break;
      case "investigator.concluded":
        states.investigator = "done";
        break;
      case "responder.sent":
        states.responder = "done";
        break;
    }
  }
  if (incident.status === "awaiting_approval") {
    states.investigator = "done";
    states.responder = "waiting";
  }
  if (incident.status === "reported") states.responder = "done";
  if (incident.status === "failed") states.investigator = "failed";
  return states;
}
