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
