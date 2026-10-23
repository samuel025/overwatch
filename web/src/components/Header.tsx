import { useState } from "react";
import { toast } from "sonner";
import { api } from "../api";
import { useStore } from "../store";

export function Header() {
  const status = useStore((s) => s.status);
  const memory = useStore((s) => s.memory);
  const [symptom, setSymptom] = useState("");

  const dataLabel = status?.sources.some((s) => s.type === "zetaris")
    ? "Zetaris"
    : status?.sources.map((s) => s.name).join(" + ");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const text = symptom.trim();
    if (!text) return;
    setSymptom("");
    await api.investigate(text);
    toast("Investigation started", { description: text });
  }

  return (
    <header className="header">
      <div className="brand">
        <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden>
          <circle cx="16" cy="16" r="12.5" fill="none" stroke="currentColor" strokeWidth="2.5" />
          <circle cx="16" cy="16" r="4.5" fill="currentColor" />
        </svg>
        <span className="brand-name">Overwatch</span>
        <span className="brand-system">{status?.system ?? "…"}</span>
      </div>

      <div className="stack" aria-label="Powered by">
        <span className="stack-item" title="Where agents read data">
          <span className="stack-label">Data</span>
          {dataLabel || "…"}
        </span>
        <span className="stack-item" title="Incident memory engine">
          <span className="stack-label">Memory</span>
          {memory?.health.ok ? `H-MEM · ${memory.health.memories ?? 0}` : status?.memory_engine ?? "…"}
        </span>
        <span className="stack-item" title={status?.provider}>
          <span className="stack-label">AI</span>
          {status?.provider.includes("127.0.0.1:8088") || status?.model.startsWith("meterless/") ? "Nightshift" : "Nemotron"} ·{" "}
          {status?.model.replace(/^nvidia\//, "") ?? "…"}
        </span>
      </div>

      <form className="report" onSubmit={submit}>
        <input
          value={symptom}
          onChange={(e) => setSymptom(e.target.value)}
          placeholder="Report a problem, e.g. “checkout feels slow”"
          aria-label="Describe a problem to investigate"
        />
        <button className="btn btn-primary" disabled={!symptom.trim()}>
          Investigate
        </button>
      </form>

      <a className="btn header-settings" href="#settings" title="Services, external services, checks, AI provider, notifications">
        <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0" />
          <circle cx="16" cy="6" r="2" />
          <circle cx="10" cy="12" r="2" />
          <circle cx="18" cy="18" r="2" />
        </svg>
        Settings
      </a>
    </header>
  );
}
