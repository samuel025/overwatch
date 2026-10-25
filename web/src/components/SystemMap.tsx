import "@xyflow/react/dist/style.css";
import {
  applyNodeChanges,
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Panel,
  Position,
  ReactFlow,
  useReactFlow,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import clsx from "clsx";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { nodeHealth, STATE_LABEL, type NodeHealth, type NodeState } from "../health";
import { useStore } from "../store";
import type { BlastRadius } from "../types";

const W = 172; // node card
const H = 70;
const FIT = { padding: 0.1, maxZoom: 1 }; // fills the box, with a little room around the cards
const COL_GAP = 96;
const ROW_GAP = 28;

type Pos = { x: number; y: number };
type NodeData = { name: string; h: NodeHealth };
type MapNode = Node<NodeData, "service">;

/** Columns by call depth: entry points on the left, what they depend on to the right. */
function layout(topology: Record<string, string[]>): Map<string, Pos> {
  const nodes = new Set<string>(Object.keys(topology));
  Object.values(topology).forEach((deps) => deps.forEach((d) => nodes.add(d)));
  const called = new Set(Object.values(topology).flat());
  const depth = new Map<string, number>();
  const visit = (n: string, d: number, seen: Set<string>) => {
    if (seen.has(n)) return;
    depth.set(n, Math.max(depth.get(n) ?? 0, d));
    (topology[n] ?? []).forEach((c) => visit(c, d + 1, new Set([...seen, n])));
  };
  [...nodes].filter((n) => !called.has(n)).forEach((root) => visit(root, 0, new Set()));
  nodes.forEach((n) => depth.has(n) || depth.set(n, 0));

  const cols: string[][] = [];
  depth.forEach((d, n) => (cols[d] ??= []).push(n));
  cols.forEach((c) => c.sort());
  const tallest = Math.max(...cols.map((c) => c.length));
  const height = tallest * H + (tallest - 1) * ROW_GAP;
  const pos = new Map<string, Pos>();
  cols.forEach((col, ci) => {
    const colH = col.length * H + (col.length - 1) * ROW_GAP;
    col.forEach((n, ri) => pos.set(n, { x: ci * (W + COL_GAP), y: (height - colH) / 2 + ri * (H + ROW_GAP) }));
  });
  return pos;
}

// Where someone dragged each service to, remembered per system in this browser.
const storageKey = (system: string | undefined) => `overwatch.map.${system ?? "default"}`;
function loadPositions(key: string | null): Record<string, Pos> {
  if (!key) return {};
  try {
    return JSON.parse(window.localStorage.getItem(key) ?? "{}");
  } catch {
    return {};
  }
}
function savePositions(key: string | null, nodes: MapNode[]) {
  if (!key) return;
  try {
    window.localStorage.setItem(key, JSON.stringify(Object.fromEntries(nodes.map((n) => [n.id, n.position]))));
  } catch {
    /* private window or blocked storage: the layout just is not remembered */
  }
}

const ROLE_TAG = { cause: "Likely cause", affected: "Affected", risk: "At risk" } as const;
const TONE: Record<NodeState, string> = {
  healthy: "#22c55e",
  suspected: "#f59e0b",
  degraded: "#ef4444",
  down: "#64748b",
  unknown: "#2a3447",
};

function ServiceNode({ data }: NodeProps<MapNode>) {
  const { name, h } = data;
  const external = name.includes(".");
  const tag = h.role ? ROLE_TAG[h.role] : null;
  return (
    <div
      className={clsx("node", `node-${h.state}`, h.role && `node-role-${h.role}`, external && "node-external")}
      title={h.problems.join("\n") || STATE_LABEL[h.state]}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} className="port" />
      <div className="node-top">
        <span className="node-name">{name}</span>
        {tag && <span className="node-tag">{tag}</span>}
      </div>
      <div className="node-state">
        <span className="node-dot" />
        {STATE_LABEL[h.state]}
        {external && <span className="node-ext">external</span>}
      </div>
      <div className="node-metrics">
        {h.state === "down"
          ? h.problems[0]?.replace(/^container \S+ /, "") ?? "No data reported"
          : h.metrics.map((m) => (
              <span key={m.label} className={clsx(m.bad && "node-metric-bad")}>
                {m.label} <b>{m.value}</b>
              </span>
            ))}
      </div>
      <Handle type="source" position={Position.Right} isConnectable={false} className="port" />
    </div>
  );
}

const nodeTypes = { service: ServiceNode };

/** Lives inside <ReactFlow> so it can use the canvas: put everything back where the layout puts it. */
function ResetButton({ onReset }: { onReset: () => void }) {
  const { fitView } = useReactFlow();
  return (
    <button
      type="button"
      className="btn map-reset"
      title="Put every service back in its automatic place"
      onClick={() => {
        onReset();
        window.requestAnimationFrame(() => fitView({ ...FIT, duration: 300 }));
      }}
    >
      Reset layout
    </button>
  );
}

function Flow({ topology, blast, preview }: { topology: Record<string, string[]>; blast?: BlastRadius | null; preview: boolean }) {
  const liveChecks = useStore((s) => s.checks);
  const liveDown = useStore((s) => s.down);
  const system = useStore((s) => s.status?.system);
  // In preview mode (setup page) we only know the structure, not the health, and nothing is remembered.
  const checks = preview ? [] : liveChecks;
  const down = preview ? {} : liveDown;
  const key = preview ? null : storageKey(system);

  const structure = JSON.stringify(topology);
  const auto = useMemo(() => layout(topology), [structure]); // eslint-disable-line react-hooks/exhaustive-deps
  const names = useMemo(() => [...auto.keys()], [auto]);
  const health = useMemo(
    () => new Map(names.map((n) => [n, nodeHealth(n, checks, blast, down)])),
    [names, checks, blast, down],
  );
  const healthKey = JSON.stringify([...health.entries()]);

  const build = (previous: MapNode[]): MapNode[] => {
    const saved = loadPositions(key);
    return names.map((n) => {
      const old = previous.find((p) => p.id === n);
      return {
        ...(old ?? {}),
        id: n,
        type: "service" as const,
        position: old?.position ?? saved[n] ?? auto.get(n)!,
        data: { name: n, h: health.get(n)! },
      };
    });
  };
  const [nodes, setNodes] = useState<MapNode[]>(() => build([]));
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  // New health or a changed topology: update what the cards say, keep wherever they were dragged to.
  useEffect(() => {
    setNodes((prev) => build(prev));
  }, [structure, healthKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const onNodesChange = useCallback((changes: NodeChange<MapNode>[]) => setNodes((ns) => applyNodeChanges(changes, ns)), []);
  const onDragStop = useCallback(() => savePositions(key, nodesRef.current), [key]);
  const reset = useCallback(() => {
    if (key) {
      try {
        window.localStorage.removeItem(key);
      } catch {
        /* ignore */
      }
    }
    setNodes((ns) => ns.map((n) => ({ ...n, position: auto.get(n.id)! })));
  }, [key, auto]);

  const edges: Edge[] = useMemo(() => {
    const hot = new Set((blast?.edges ?? []).map((e) => `${e.from}>${e.to}`));
    const broken = (n: string) => ["down", "degraded"].includes(health.get(n)?.state ?? "unknown");
    return Object.entries(topology).flatMap(([from, deps]) =>
      deps.map((to) => {
        const failing = hot.has(`${from}>${to}`) || (broken(to) && broken(from));
        const isDown = health.get(to)?.state === "down";
        const color = failing ? "#ef4444" : isDown ? "#64748b" : "#475569";  // the broken dependency itself reads red
        return {
          id: `${from}>${to}`,
          source: from,
          target: to,
          animated: failing && !isDown,
          style: { stroke: color, strokeWidth: failing ? 2.5 : 2, strokeDasharray: isDown ? "2 6" : undefined },
          markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
        };
      }),
    );
  }, [structure, health, blast]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className={clsx("map-flow", preview && "map-flow-preview")} aria-label="System map: drag the services to rearrange them">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onNodeDragStop={onDragStop}
        colorMode="dark"
        fitView
        fitViewOptions={FIT}
        minZoom={0.3}
        maxZoom={1.8}
        nodesConnectable={false}
        edgesFocusable={false}
        deleteKeyCode={null}
        zoomOnScroll={false}
        zoomActivationKeyCode={["Control", "Meta"]}
        preventScrolling={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1.4} color="#252d3d" />
        <Controls showInteractive={false} position="bottom-left" />
        {!preview && names.length > 8 && (
          <MiniMap
            pannable
            zoomable
            position="bottom-right"
            style={{ width: 140, height: 90 }}
            nodeColor={(n) => TONE[(n.data as NodeData).h.state]}
            maskColor="rgba(10, 15, 28, 0.65)"
          />
        )}
        <Panel position="top-right">
          <ResetButton onReset={reset} />
        </Panel>
        <Panel position="top-left" className="map-hint">
          Drag a service to move it · scroll the page normally · Ctrl+scroll to zoom
        </Panel>
      </ReactFlow>
    </div>
  );
}

export function SystemMap({ blast, preview }: { blast?: BlastRadius | null; preview?: Record<string, string[]> }) {
  const liveTopology = useStore((s) => s.status?.topology);
  const topology = preview ?? liveTopology;
  if (!topology || !Object.keys(topology).length) {
    return <p className="empty">Add a <code>topology:</code> section to overwatch.yaml to see the system map.</p>;
  }
  return <Flow topology={topology} blast={blast} preview={!!preview} />;
}

export function MapLegend() {
  return (
    <div className="map-legend">
      <span><i className="lg lg-healthy" /> Healthy</span>
      <span><i className="lg lg-suspected" /> Possible problem</span>
      <span><i className="lg lg-degraded" /> Having problems</span>
      <span><i className="lg lg-down" /> Not responding</span>
      <span><i className="lg lg-risk" /> At risk</span>
      <span><i className="lg lg-edge" /> Calls · red = failing path</span>
    </div>
  );
}
