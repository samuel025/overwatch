import { useStore } from "./store";
import type { MemoryOverview, OwEvent, Status } from "./types";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export const api = {
  status: () => fetch("/api/status").then(json<Status>),
  memory: () => fetch("/api/memory").then(json<MemoryOverview>),
  checkNow: () => fetch("/api/check", { method: "POST" }).then(json<unknown>),
  investigate: (symptom: string) =>
    fetch("/api/investigate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ symptom }),
    }).then(json<unknown>),
  approve: (id: string) =>
    fetch(`/api/incidents/${id}/approve`, { method: "POST" }).then(json<{ receipts: Record<string, string> }>),
  dismiss: (id: string) => fetch(`/api/incidents/${id}/dismiss`, { method: "POST" }).then(json<unknown>),
  resolve: (id: string) => fetch(`/api/incidents/${id}/resolve`, { method: "POST" }).then(json<unknown>),
};

export interface SetupState {
  configured: boolean;
  config_path: string;
  docker_available: boolean;
  docker_projects: string[];
  kubernetes_in_cluster: boolean;
  ai_configured: boolean;
  ai_base_url: string;
  ai_model: string;
}

export interface PlanCheck {
  name: string;
  kind?: string;
  service: string;
  description: string;
  why?: string;
  enabled: boolean;
  [key: string]: unknown;
}

export interface Plan {
  system: { name: string; description: string };
  docker: { project?: string; service_label?: string };
  docker_projects?: string[];
  services: Record<string, Record<string, string>>;
  service_facts?: Record<string, string>; // what Overwatch worked out about each service (calls, callers, where it runs)
  runtime_facts: string;
  sources: Record<string, { type: string; url: string }>;
  catalog: string;
  topology: Record<string, string[]>;
  checks: PlanCheck[];
  evidence: { name: string; label: string; source: string; query: string; enabled: boolean }[];
  notes: string[];
  errors: string[];
  externals: External[];
  log_source: { type: "sql" | "docker" | "kubernetes" | "loki"; source: string; table?: string } | null;
  settings?: { notifiers: Notifiers; watch: Partial<WatchSettings>; approval: "manual" | "auto"; ai?: { max_steps?: number } };
}

export interface Notifiers {
  slack?: { enabled: boolean; webhook_url: string; webhook_url_set?: boolean };
  jira?: { enabled: boolean; base_url: string; email: string; api_token: string; api_token_set?: boolean; project_key: string; issue_type?: string };
  webhook?: { enabled: boolean; url: string; url_set?: boolean };
}

export interface WatchSettings {
  interval_seconds: number;
  confirm_samples: number;
  cooldown_seconds: number;
  merge_window_seconds: number;
  resolve_after_seconds: number;
  max_parallel_investigations: number;
}

export interface AiSettings {
  base_url: string;
  api_key: string;
  api_key_set?: boolean;
  model: string;
  triage_model: string;
  max_steps?: number;
}

export interface SettingsCheck {
  name: string;
  kind: string;
  service: string;
  description: string;
  severity: string;
  threshold: number | null;
  comparison: string;
  relative_factor: number | null;
  enabled: boolean;
}

export interface Settings {
  config_path: string;
  system: { name: string; description: string };
  services: { name: string; what: string; normal: string; runs_on: string; facts?: string }[];
  externals: External[];
  service_names: string[];
  checks: SettingsCheck[];
  sources: { name: string; type: string }[];
  ai: AiSettings;
  notifiers: Notifiers;
  watch: WatchSettings;
  approval: "manual" | "auto";
}

export const settingsApi = {
  read: () => fetch("/api/settings").then(json<Settings>),
  save: (body: Partial<Settings>) =>
    fetch("/api/settings", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) })
      .then(json<{ ok: boolean; config: string; backup: string; secrets: string[] }>),
};

export interface External {
  name: string;
  callers: string[];
  evidence: string[];
  health_url: string;
  log_text: string;
  enabled: boolean;
  suggested: boolean;
  what?: string;
  original?: string;
}

const post = <T,>(path: string, body: unknown) =>
  fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then(json<T>);

export const setupApi = {
  state: () => fetch("/api/setup/state").then(json<SetupState>),
  discover: (body: unknown) => post<{ plan: Plan; yaml: string }>("/api/setup/discover", body),
  testAi: (body: unknown) => post<{ ok: boolean; models?: string[]; error?: string }>("/api/setup/test-ai", body),
  apply: (body: unknown) => post<{ ok: boolean; config: string; backup: string | null; env: string }>("/api/setup/apply", body),
};

export async function refreshMemory() {
  try {
    useStore.getState().setMemory(await api.memory());
  } catch {
    /* panel shows the last known state */
  }
}

/**
 * Hydrate from /api/status, then stream /api/events. The server replays its recent backlog on
 * every connect, so on any drop we re-hydrate first and reconnect, instead of EventSource's
 * blind auto-retry.
 */
export function connect(onEvent: (e: OwEvent) => void): () => void {
  let source: EventSource | null = null;
  let retry: number | undefined;
  let stopped = false;
  const { hydrate, setConnected } = useStore.getState();

  const open = async () => {
    try {
      hydrate(await api.status());
    } catch {
      setConnected(false);
      retry = window.setTimeout(open, 2000);
      return;
    }
    if (stopped) return;
    source = new EventSource("/api/events");
    source.onopen = () => setConnected(true);
    source.onmessage = (m) => onEvent(JSON.parse(m.data) as OwEvent);
    source.onerror = () => {
      source?.close();
      setConnected(false);
      if (!stopped) retry = window.setTimeout(open, 2000);
    };
  };

  open();
  refreshMemory();
  return () => {
    stopped = true;
    source?.close();
    window.clearTimeout(retry);
  };
}
