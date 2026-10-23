import { useEffect, useReducer, useState } from "react";
import { Toaster, toast } from "sonner";
import { connect, refreshMemory, setupApi, type SetupState } from "./api";
import { Setup } from "./components/Setup";
import { Header } from "./components/Header";
import { IncidentView } from "./components/IncidentView";
import { Agents, Incidents, Signals, UnderTheHood } from "./components/Side";
import { StatusBanner } from "./components/StatusBanner";
import { MapLegend, SystemMap } from "./components/SystemMap";
import { focusIncident } from "./health";
import { useStore } from "./store";
import type { BlastRadius, OwEvent } from "./types";

let live = false; // replayed backlog should not toast; only events after the first live tick

function onEvent(e: OwEvent) {
  useStore.getState().apply(e);
  if (e.ts > Date.now() / 1000 - 5) live = true;
  if (!live) return;
  switch (e.type) {
    case "incident.opened":
      toast.warning("Problem detected", { description: e.incident.anomalies.map((a) => a.check.replace(/_/g, " ")).join(", ") });
      break;
    case "investigator.concluded":
      toast("Diagnosis ready", { description: e.root_cause.slice(0, 140) });
      break;
    case "incident.recovered":
      toast.success("Recovered", { description: "Everything behind the incident is healthy again." });
      break;
    case "incident.merged":
      toast("Related alert joined the incident", { description: e.checks.map((c) => c.replace(/_/g, " ")).join(", ") });
      break;
    case "incident.updated":
    case "responder.sent":
      refreshMemory();
      break;
  }
}

export default function App() {
  const [setup, setSetup] = useState<SetupState | null>(null);
  const [showSetup, setShowSetup] = useState(window.location.hash === "#setup");

  useEffect(() => {
    setupApi.state().then(setSetup).catch(() => setSetup(null));
    const onHash = () => setShowSetup(window.location.hash === "#setup");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (setup && (!setup.configured || showSetup)) {
    return (
      <div className="app">
        <Setup
          state={setup}
          onCancel={setup.configured ? () => { window.location.hash = ""; setShowSetup(false); } : undefined}
        />
        <Toaster theme="dark" position="bottom-right" />
      </div>
    );
  }
  if (!setup) return <div className="app" />;
  return <Console />;
}

function Console() {
  // Relative times ("12s ago", "ongoing for 3m") need a periodic re-render.
  const [, rerender] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    const stop = connect(onEvent);
    const t = window.setInterval(rerender, 5_000);
    return () => {
      stop();
      window.clearInterval(t);
    };
  }, []);

  const order = useStore((s) => s.order);
  const incidents = useStore((s) => s.incidents);
  const selectedId = useStore((s) => s.selected);
  const follow = useStore((s) => s.follow);
  const focus = focusIncident(order, incidents);
  // Follow what is happening now, unless the user picked an incident from the history.
  const picked = !follow && selectedId ? incidents[selectedId] : undefined;
  const shown = picked || focus;
  const blast =
    focus && "started_at" in focus.blast_radius ? (focus.blast_radius as BlastRadius) : null;

  return (
    <div className="app">
      <Header />
      <main className="layout">
        <div className="main-col">
          <StatusBanner />
          <section className="card map-card" aria-label="System map">
            <div className="card-head">
              <h2 className="card-title">Your system</h2>
              <span className="small muted">Live. Each box is a service; lines show who calls whom.</span>
            </div>
            <SystemMap blast={blast} />
            <MapLegend />
          </section>
          {shown ? (
            <IncidentView key={shown.id} incident={shown} />
          ) : (
            <section className="card quiet">
              <p>Nothing is wrong right now.</p>
              <p className="small muted">
                When a signal crosses its limit, Overwatch opens an incident here and you can watch it investigate. You can
                also describe a problem in the box at the top.
              </p>
            </section>
          )}
        </div>
        <aside className="side-col">
          <Agents />
          <Signals />
          <Incidents />
          <UnderTheHood />
        </aside>
      </main>
      <Toaster theme="dark" position="bottom-right" />
    </div>
  );
}
