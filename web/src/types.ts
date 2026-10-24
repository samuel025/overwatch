export type Severity = "low" | "medium" | "high" | "critical";
export type IncidentStatus = "investigating" | "awaiting_approval" | "reported" | "dismissed" | "failed";

export interface Check {
  name: string;
  service: string;
  value: number | null;
  threshold: number | null;
  comparison: ">" | ">=" | "<" | "<=";
  description: string;
  severity: Severity;
  relative_factor: number | null;
  relative_min: number;
  baseline: number | null;
  kind: "metric" | "heartbeat" | "probe";
  failing: boolean;
  suspected: boolean;
}

export interface Hypothesis {
  text: string;
  confidence: number;
  origin_service: string | null;
  check_next: string;
  model: string;
  seconds: number;
}

export type DownMap = Record<string, { service: string; since: number; detail: string }>;

export interface Anomaly {
  check: string;
  service: string;
  value: number | null;
  threshold: number;
  comparison: string;
  description: string;
  detected_at: string;
}

export interface Evidence {
  source: string;
  query: string;
  rows: Record<string, unknown>[];
  note: string;
  error: string | null;
  ms: number;
}

export interface Memory {
  id: string;
  content: string;
  relevance: number;
  tags?: string[];
  used?: boolean;
}

export interface ImpactItem {
  name: string;
  label: string;
  unit: string;
  value: number | null;
  error?: string;
}

export interface BlastRadius {
  origin: string | null;
  affected: string[];
  at_risk: string[];
  edges: { from: string; to: string }[];
  started_at: number;
  detected_at: number;
  time_to_detect_s: number;
  duration_s: number;
  recovered_at: number | null;
  impact: ImpactItem[];
  measured_at: number;
}

export interface Incident {
  id: string;
  title: string;
  services: string[];
  opened_at: string;
  status: IncidentStatus;
  root_cause: string;
  confidence: number;
  severity: Severity;
  recommended_actions: string[];
  evidence: Evidence[];
  similar_incidents: Memory[];
  reasoning: string[];
  anomalies: Anomaly[];
  origin_service: string | null;
  blast_radius: BlastRadius | Record<string, never>;
  recovered_at: string | null;
  healthy_since?: number | null; // unix time its checks began to look healthy again (recovering)
  recover_wait?: number; // seconds they must stay healthy before it closes
  pack: Evidence[];
  hypothesis: Hypothesis | Record<string, never>;
  started_at: number | null;
  hypotheses: { cause: string; verdict: string; why: string }[];
  cited: { step: number; quote: string; shows: string }[];
  first_guess_verdict: { verdict?: string; why?: string };
  grounding: {
    verified?: number;
    total?: number;
    unverified?: { step: number; quote: string }[];
    unsupported_terms?: string[];
    unsupported_claims?: string[];
    ok?: boolean;
  };
}

export interface Status {
  system: string;
  last_check_at: string | null;
  checks: Check[];
  history: Record<string, [number, number | null][]>;
  sources: { name: string; language: string; type: string }[];
  data_layer: { backend: "zetaris" | "direct"; source?: string; federated: string[] };
  interval_seconds: number;
  topology: Record<string, string[]>;
  down: DownMap;
  incidents: Incident[];
  approval: "manual" | "auto";
  memory_engine: string;
  model: string;
  provider: string;
}

export interface MemoryOverview {
  health: { ok: boolean; engine: string; memories?: number; error?: string };
  memories: { id: string; content: string; layer: string; confidence: number; accessCount: number; source: string }[];
  ledger: { memoryId: string; action: string; actor: string; timestamp: number }[];
}

/** Everything the backend publishes on /api/events. */
export type OwEvent = { id: number; ts: number } & (
  | { type: "watch.tick"; values: Record<string, number | null>; failing: string[]; suspected: string[] }
  | { type: "service.event"; state: "down" | "up"; service: string; instance: string; detail: string }
  | ({ type: "investigator.hypothesis"; incident: string } & Hypothesis)
  | { type: "incident.opened" | "incident.updated"; incident: Incident }
  | { type: "agent.working"; agent: "evidence" | "historian" | "responder"; incident: string }
  | { type: "agent.thinking"; agent: "investigator"; incident: string; step: number }
  | { type: "memory.recalled"; incident: string; engine: string; memories: Memory[] }
  | { type: "investigator.thought"; incident: string; step: number; thought: string; action: string }
  | { type: "query.started"; incident: string; step: number; source: string; query: string; purpose: string }
  | {
      type: "query.finished";
      incident: string;
      step: number;
      source: string;
      error: string | null;
      row_count: number;
      rows: Record<string, unknown>[];
      ms: number;
      purpose?: string;
    }
  | {
      type: "investigator.concluded";
      incident: string;
      root_cause: string;
      confidence: number;
      severity: Severity;
      recommended_actions: string[];
    }
  | { type: "responder.sent"; incident: string; receipts: Record<string, string> }
  | { type: "investigator.failed"; incident: string; error: string }
  | { type: "blast_radius.updated"; incident: string; blast_radius: BlastRadius }
  | { type: "incident.recovered"; incident: string; recovered_at: string }
  | { type: "incident.merged"; incident: string; checks: string[] }
  | {
      type: "llm.call";
      agent: string;
      model: string;
      seconds: number;
      usage: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number } | null;
      provider_base_url: string;
    }
);

export type AgentName = "watcher" | "historian" | "investigator" | "responder";
export type AgentState = "idle" | "working" | "done" | "waiting" | "failed";
