import { useEffect, useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";
const DEFAULT_CASES = JSON.stringify([
  {
    case_id: "auth",
    name: "Authentication bug",
    mission_input: "Fix the authentication expiry bug",
    workspace_source: "missions/demo_auth_bug/repo",
    expected_test_target: "tests",
  },
], null, 2);

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!response.ok) {
    const detail = typeof body === "object" && body?.detail ? body.detail : response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }
  return body;
}

function post(path, payload) {
  return api(path, { method: "POST", body: payload === undefined ? undefined : JSON.stringify(payload) });
}

function StatusPill({ value }) {
  const label = value || "unknown";
  return <span className={`status-pill status-${String(label).toLowerCase()}`}>{label}</span>;
}

function Kpi({ label, value, hint, tone = "ink" }) {
  return <article className={`kpi kpi-${tone}`}><span>{label}</span><strong>{value ?? "—"}</strong>{hint && <small>{hint}</small>}</article>;
}

function Panel({ title, eyebrow, action, children, className = "" }) {
  return <section className={`panel ${className}`}>
    <div className="panel-heading">{eyebrow && <span className="eyebrow">{eyebrow}</span>}<div className="panel-title-row"><h2>{title}</h2>{action}</div></div>
    {children}
  </section>;
}

function JsonBlock({ value }) {
  return <pre className="json-block">{value ? JSON.stringify(value, null, 2) : "No data loaded"}</pre>;
}

function Empty({ children }) {
  return <div className="empty-state"><span className="empty-mark">○</span><p>{children}</p></div>;
}

function App() {
  const [view, setView] = useState("overview");
  const [notice, setNotice] = useState(null);
  const [overview, setOverview] = useState({ health: null, aggregate: null, models: [], suites: [] });
  const [openRun, setOpenRun] = useState("");

  const notify = (message, type = "info") => {
    setNotice({ message, type });
    window.setTimeout(() => setNotice(null), 4200);
  };

  const refreshOverview = async () => {
    try {
      const [health, aggregate, models, suites] = await Promise.all([
        api("/health"), api("/analytics/runs"), api("/analytics/models"), api("/benchmark-suites"),
      ]);
      setOverview({ health, aggregate, models, suites });
    } catch (error) { notify(error.message, "error"); }
  };

  useEffect(() => { refreshOverview(); }, []);

  const navigate = (next) => { setView(next); if (next !== "runs") setOpenRun(""); };
  const inspectRun = (runId) => { setOpenRun(runId); setView("runs"); };

  return <div className="app-shell">
    <Sidebar view={view} navigate={navigate} health={overview.health} />
    <main className="main-shell">
      <header className="topbar"><div><span className="topbar-kicker">LOCAL OPERATIONS / V0.1</span><h1>{viewTitle(view)}</h1></div><button className="button button-quiet" onClick={refreshOverview}>↻ Refresh</button></header>
      {notice && <div className={`notice notice-${notice.type}`}>{notice.message}</div>}
      {view === "overview" && <Overview data={overview} navigate={navigate} inspectRun={inspectRun} />}
      {view === "suites" && <SuitesView suites={overview.suites} refresh={refreshOverview} notify={notify} />}
      {view === "experiments" && <ExperimentsView notify={notify} inspectRun={inspectRun} />}
      {view === "missions" && <MissionsView notify={notify} inspectRun={inspectRun} />}
      {view === "runs" && <RunsView initialRunId={openRun} notify={notify} />}
    </main>
  </div>;
}

function viewTitle(view) {
  return { overview: "Operations overview", suites: "Benchmark suites", experiments: "Experiments", missions: "Missions", runs: "Run inspector" }[view] || "AgentCorp";
}

function Sidebar({ view, navigate, health }) {
  const items = [
    ["overview", "⌂", "Overview"],
    ["missions", "↗", "Missions"],
    ["experiments", "◇", "Experiments"],
    ["suites", "▦", "Benchmark suites"],
    ["runs", "◌", "Run inspector"],
  ];
  return <aside className="sidebar">
    <div className="brand"><div className="brand-orbit"><span /></div><div><strong>AgentCorp</strong><small>AI company lab</small></div></div>
    <div className="sidebar-label">Workspace</div>
    <nav>{items.map(([key, icon, label]) => <button key={key} className={view === key ? "nav-item active" : "nav-item"} onClick={() => navigate(key)}><span className="nav-icon">{icon}</span>{label}{key === "runs" && <span className="nav-arrow">→</span>}</button>)}</nav>
    <div className="sidebar-spacer" />
    <div className="runtime-card"><div className="live-dot" /><div><strong>Runtime online</strong><span>{health?.status === "ok" ? "FastAPI connected" : "Connect to API"}</span></div></div>
    <div className="sidebar-foot">LOCAL / SAFE BY DEFAULT<br /><span>observable · replayable · comparable</span></div>
  </aside>;
}

function Overview({ data, navigate, inspectRun }) {
  const aggregate = data.aggregate || {};
  const models = data.models || [];
  return <div className="page-stack">
    <section className="hero"><div><span className="eyebrow">AGENTCORP CONTROL ROOM</span><h2>Make every run<br /><em>worth comparing.</em></h2><p>Mission execution, evidence, and benchmark comparisons in one local workspace.</p></div><div className="hero-art"><div className="hero-ring ring-one" /><div className="hero-ring ring-two" /><div className="hero-core">AC<span>·</span></div><div className="hero-caption">OBSERVE<br />THEN DECIDE</div></div></section>
    <div className="kpi-grid"><Kpi label="Total runs" value={aggregate.run_count ?? 0} hint={`${aggregate.evaluated_run_count ?? 0} evaluated`} /><Kpi label="Terminal pass rate" value={aggregate.terminal_pass_rate == null ? "—" : `${Math.round(aggregate.terminal_pass_rate * 100)}%`} hint={`${aggregate.passed_count ?? 0} passed`} tone="teal" /><Kpi label="Tool calls" value={aggregate.tool_call_count ?? 0} hint={`${aggregate.tool_failure_count ?? 0} failures`} tone="orange" /><Kpi label="Recovery runs" value={aggregate.recovery_run_count ?? 0} hint={`${aggregate.recovered_to_pass_count ?? 0} recovered to pass`} /></div>
    <div className="content-grid overview-grid"><Panel title="Model comparison" eyebrow="PHASE 8 AGGREGATES" action={<button className="text-button" onClick={() => navigate("experiments")}>Open experiments →</button>}>
      {models.length ? <div className="model-list">{models.map((model) => <div className="model-row" key={model.group_key?.join("/") || model.model_id}><div className="model-avatar">{(model.model_name || "?").slice(0, 1).toUpperCase()}</div><div className="model-info"><strong>{model.model_name || "Unknown model"}</strong><span>{model.provider_type || "—"} · {model.run_count} runs</span></div><div className="model-result"><strong>{model.terminal_pass_rate == null ? "—" : `${Math.round(model.terminal_pass_rate * 100)}%`}</strong><span>pass rate</span></div><StatusPill value={model.failed_count ? "attention" : "ready"} /></div>)}</div> : <Empty>No runs yet. Start a mission or execute an experiment.</Empty>}
    </Panel><Panel title="Benchmark library" eyebrow="PHASE 10" action={<button className="text-button" onClick={() => navigate("suites")}>Manage suites →</button>}>
      {data.suites?.length ? <div className="suite-mini-list">{data.suites.slice(0, 4).map((suite) => <div className="suite-mini" key={`${suite.suite_id}:${suite.version}`}><span className="suite-icon">{suite.status === "PUBLISHED" ? "✓" : "·"}</span><div><strong>{suite.suite_id} <span>v{suite.version}</span></strong><small>{suite.cases.length} cases · {suite.status.toLowerCase()}</small></div><span className="mini-arrow">→</span></div>)}</div> : <Empty>Publish a suite to create reusable workloads.</Empty>}
    </Panel></div>
    <section className="quick-actions"><div><span className="eyebrow">START HERE</span><h3>Choose your next move</h3></div><button onClick={() => navigate("missions")}><span>01</span><strong>Run a mission</strong><small>PM → Developer → QA</small> <b>↗</b></button><button onClick={() => navigate("experiments")}><span>02</span><strong>Compare models</strong><small>Matrix + analytics</small> <b>↗</b></button><button onClick={() => navigate("suites")}><span>03</span><strong>Build a suite</strong><small>Versioned workload</small> <b>↗</b></button></section>
  </div>;
}

function SuitesView({ suites, refresh, notify }) {
  const [form, setForm] = useState({ suite_id: "coding-core", version: "1", name: "Coding Core", description: "Reusable coding workload", cases: DEFAULT_CASES });
  const [busy, setBusy] = useState(false);
  const update = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.value }));
  const createSuite = async (event) => {
    event.preventDefault(); setBusy(true);
    try {
      const cases = JSON.parse(form.cases);
      await post("/benchmark-suites", { suite_id: form.suite_id.trim(), version: Number(form.version), name: form.name.trim(), description: form.description.trim() || null, cases });
      notify("Draft suite created", "success"); await refresh();
    } catch (error) { notify(error.message || "Cases must be valid JSON", "error"); } finally { setBusy(false); }
  };
  const publish = async (suite) => { try { await post(`/benchmark-suites/${encodeURIComponent(suite.suite_id)}/versions/${suite.version}/publish`); notify(`${suite.suite_id} v${suite.version} published`, "success"); await refresh(); } catch (error) { notify(error.message, "error"); } };
  return <div className="page-stack"><section className="page-intro"><div><span className="eyebrow">REUSABLE WORKLOADS</span><h2>Benchmark suites</h2><p>Versioned, immutable test papers for repeatable experiments.</p></div><div className="intro-stat"><strong>{suites.length}</strong><span>stored versions</span></div></section><div className="content-grid suites-layout"><Panel title="Create a suite" eyebrow="DRAFT DEFINITION"><form className="form-stack" onSubmit={createSuite}><div className="form-row"><label>Suite ID<input value={form.suite_id} onChange={update("suite_id")} required /></label><label>Version<input type="number" min="1" value={form.version} onChange={update("version")} required /></label></div><label>Name<input value={form.name} onChange={update("name")} required /></label><label>Description<span className="optional">optional</span><input value={form.description} onChange={update("description")} /></label><label>Cases <span className="field-note">ordered JSON array</span><textarea className="code-input" rows="12" value={form.cases} onChange={update("cases")} required /></label><button className="button button-dark" disabled={busy}>{busy ? "Creating…" : "Create draft"}</button></form></Panel><Panel title="Suite registry" eyebrow="PERSISTED DEFINITIONS"><div className="registry-list">{suites.length ? suites.map((suite) => <div className="registry-card" key={`${suite.suite_id}:${suite.version}`}><div className="registry-top"><div><span className="suite-icon large">▦</span><div className="registry-title"><strong>{suite.suite_id}</strong><span>v{suite.version}</span></div></div><StatusPill value={suite.status} /></div><p>{suite.description || "No description"}</p><div className="registry-meta"><span>{suite.cases.length} cases</span><span>{suite.spec_digest ? `${suite.spec_digest.slice(0, 10)}…` : "digest pending"}</span><span>{new Date(suite.created_at).toLocaleDateString()}</span></div>{suite.status === "DRAFT" && <button className="button button-outline full" onClick={() => publish(suite)}>Publish version</button>}</div>) : <Empty>No benchmark suites yet.</Empty>}</div></Panel></div></div>;
}

function ExperimentsView({ notify, inspectRun }) {
  const [source, setSource] = useState("suite");
  const [form, setForm] = useState({ name: "Qwen control experiment", suite_id: "coding-core", suite_version: "1", models: "fake-default", repetitions: "1", cases: DEFAULT_CASES });
  const [experiment, setExperiment] = useState(null);
  const [analytics, setAnalytics] = useState(null);
  const [lookupId, setLookupId] = useState("");
  const [busy, setBusy] = useState(false);
  const update = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.value }));
  const create = async (event) => {
    event.preventDefault(); setBusy(true);
    try {
      const payload = { name: form.name.trim(), models: form.models.split(",").map((model_id) => ({ model_id: model_id.trim() })).filter((item) => item.model_id), repetitions: Number(form.repetitions) };
      if (source === "suite") payload.benchmark_suite = { suite_id: form.suite_id.trim(), version: Number(form.suite_version) };
      else payload.cases = JSON.parse(form.cases);
      const created = await post("/experiments", payload); setExperiment(created); setAnalytics(null); notify("Experiment created in DRAFT", "success");
    } catch (error) { notify(error.message || "Invalid experiment input", "error"); } finally { setBusy(false); }
  };
  const load = async (id = lookupId) => { if (!id.trim()) return notify("Enter an experiment ID", "error"); try { setExperiment(await api(`/experiments/${id.trim()}`)); setAnalytics(null); } catch (error) { notify(error.message, "error"); } };
  const action = async (verb) => { if (!experiment) return; setBusy(true); try { const next = await post(`/experiments/${experiment.experiment_id}/${verb}`); setExperiment(next); notify(`Experiment ${verb} complete`, "success"); } catch (error) { notify(error.message, "error"); } finally { setBusy(false); } };
  const inspect = async () => { if (!experiment) return; try { setAnalytics(await api(`/experiments/${experiment.experiment_id}/analytics`)); } catch (error) { notify(error.message, "error"); } };
  return <div className="page-stack"><section className="page-intro"><div><span className="eyebrow">CONTROLLED EXECUTION</span><h2>Experiments</h2><p>Freeze workload, model conditions, and compare observable outcomes.</p></div><div className="id-loader"><input placeholder="Load experiment ID" value={lookupId} onChange={(event) => setLookupId(event.target.value)} /><button className="button button-outline" onClick={() => load()}>Load</button></div></section><div className="content-grid experiments-layout"><Panel title="New experiment" eyebrow="ONE WORKLOAD SOURCE"><form className="form-stack" onSubmit={create}><label>Name<input value={form.name} onChange={update("name")} required /></label><div className="segmented"><button type="button" className={source === "suite" ? "selected" : ""} onClick={() => setSource("suite")}>Published suite</button><button type="button" className={source === "inline" ? "selected" : ""} onClick={() => setSource("inline")}>Inline cases</button></div>{source === "suite" ? <div className="form-row"><label>Suite ID<input value={form.suite_id} onChange={update("suite_id")} required /></label><label>Version<input type="number" min="1" value={form.suite_version} onChange={update("suite_version")} required /></label></div> : <label>Cases <span className="field-note">ordered JSON array</span><textarea className="code-input" rows="10" value={form.cases} onChange={update("cases")} required /></label>}<div className="form-row"><label>Model IDs <span className="field-note">comma separated</span><input value={form.models} onChange={update("models")} required /></label><label>Repetitions<input type="number" min="1" value={form.repetitions} onChange={update("repetitions")} required /></label></div><button className="button button-dark" disabled={busy}>{busy ? "Working…" : "Create experiment"}</button></form></Panel><Panel title={experiment ? "Selected experiment" : "No experiment selected"} eyebrow="LIFECYCLE"><ExperimentCard experiment={experiment} action={action} inspect={inspect} analytics={analytics} busy={busy} inspectRun={inspectRun} /></Panel></div></div>;
}

function ExperimentCard({ experiment, action, inspect, analytics, busy, inspectRun }) {
  if (!experiment) return <Empty>Create or load an experiment to control its lifecycle.</Empty>;
  return <div className="experiment-detail"><div className="detail-heading"><div><span className="mono muted">{experiment.experiment_id}</span><h3>{experiment.name}</h3></div><StatusPill value={experiment.status} /></div>{experiment.benchmark_suite && <div className="provenance"><span>SUITE PROVENANCE</span><strong>{experiment.benchmark_suite.suite_id} <i>v{experiment.benchmark_suite.version}</i></strong><code>{experiment.benchmark_suite.digest?.slice(0, 16)}…</code></div>}<div className="detail-stats"><div><strong>{experiment.cases?.length || 0}</strong><span>cases</span></div><div><strong>{experiment.models?.length || 0}</strong><span>models</span></div><div><strong>{experiment.expected_run_count || 0}</strong><span>expected runs</span></div></div><div className="case-order"><span className="eyebrow">FROZEN CASE ORDER</span>{experiment.cases?.map((item, index) => <div key={item.case_id}><b>{String(index + 1).padStart(2, "0")}</b><span>{item.case_id}</span><small>{item.name}</small></div>)}</div><div className="action-row"><button className="button button-outline" disabled={experiment.status !== "DRAFT" || busy} onClick={() => action("seal")}>Seal</button><button className="button button-dark" disabled={experiment.status === "DRAFT" || experiment.status === "COMPLETED" || busy} onClick={() => action("execute")}>Execute</button><button className="button button-teal" disabled={experiment.status === "DRAFT" || busy} onClick={inspect}>Analytics</button></div>{analytics && <AnalyticsPanel report={analytics} inspectRun={inspectRun} />}</div>;
}

function AnalyticsPanel({ report }) {
  const aggregate = report.overall || {};
  return <div className="analytics-panel"><div className="analytics-heading"><span className="eyebrow">EXPERIMENT ANALYTICS</span><StatusPill value={report.integrity_status} /></div><div className="analytics-kpis"><Kpi label="Materialized" value={`${report.materialized_run_count}/${report.expected_run_count}`} /><Kpi label="Pass rate" value={aggregate.terminal_pass_rate == null ? "—" : `${Math.round(aggregate.terminal_pass_rate * 100)}%`} tone="teal" /><Kpi label="Tools" value={aggregate.tool_call_count} tone="orange" /></div><div className="analytics-foot"><span>{report.terminal_run_count} terminal · {report.non_terminal_run_count} non-terminal</span><span>{report.integrity_issues?.length ? report.integrity_issues.join(", ") : "Mapping integrity verified"}</span></div></div>;
}

function MissionsView({ notify, inspectRun }) {
  const [form, setForm] = useState({ title: "Demo authentication mission", fixture: "missions/demo_auth_bug/repo", model_id: "fake-default", approval_mode: "disabled" });
  const [mission, setMission] = useState(null); const [run, setRun] = useState(null); const [busy, setBusy] = useState(false);
  const update = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.value }));
  const create = async (event) => { event.preventDefault(); setBusy(true); try { setMission(await post("/missions", { title: form.title, fixture: form.fixture })); setRun(null); notify("Mission created", "success"); } catch (error) { notify(error.message, "error"); } finally { setBusy(false); } };
  const start = async () => { if (!mission) return; setBusy(true); try { const result = await post(`/missions/${mission.id}/runs`, { model_id: form.model_id, approval_mode: form.approval_mode }); setRun(result); notify("Mission run completed", "success"); } catch (error) { notify(error.message, "error"); } finally { setBusy(false); } };
  return <div className="page-stack"><section className="page-intro"><div><span className="eyebrow">VERTICAL SLICE</span><h2>Missions</h2><p>Run the PM → Developer → QA path with the local deterministic provider.</p></div><div className="mission-path"><span>PM</span><b>→</b><span>DEV</span><b>→</b><span>QA</span></div></section><div className="content-grid mission-layout"><Panel title="Start a mission" eyebrow="MISSION DEFINITION"><form className="form-stack" onSubmit={create}><label>Title<input value={form.title} onChange={update("title")} required /></label><label>Fixture path<input value={form.fixture} onChange={update("fixture")} required /></label><div className="form-row"><label>Model ID<input value={form.model_id} onChange={update("model_id")} /></label><label>Approval mode<select value={form.approval_mode} onChange={update("approval_mode")}><option value="disabled">disabled</option><option value="policy">policy</option></select></label></div><div className="action-row"><button className="button button-dark" disabled={busy}>{busy ? "Working…" : "Create mission"}</button>{mission && <button type="button" className="button button-teal" onClick={start} disabled={busy}>Start run ↗</button>}</div></form></Panel><Panel title={mission ? "Mission ready" : "No mission selected"} eyebrow="RUN HANDOFF">{mission ? <div className="mission-result"><div className="result-icon">✓</div><span className="mono">{mission.id}</span><h3>{mission.title}</h3><p>Fixture <code>{mission.fixture}</code></p>{run && <div className="run-result"><div><span>Latest run</span><strong><StatusPill value={run.status} /></strong></div><span className="mono">{run.run_id}</span><button className="button button-outline full" onClick={() => inspectRun(run.run_id)}>Inspect run →</button></div>}</div> : <Empty>Create a mission to begin the vertical slice.</Empty>}</Panel></div></div>;
}

function RunsView({ initialRunId, notify }) {
  const [runId, setRunId] = useState(initialRunId || ""); const [run, setRun] = useState(null); const [details, setDetails] = useState({ events: [], metrics: null, evaluation: null, replay: null, approvals: [] }); const [busy, setBusy] = useState(false); const [resumeId, setResumeId] = useState("");
  const load = async (id = runId) => { if (!id.trim()) return notify("Enter a run ID", "error"); setBusy(true); try { const [loaded, events, metrics, evaluation, replay, approvals] = await Promise.all([api(`/runs/${id}`), api(`/runs/${id}/events`), api(`/runs/${id}/metrics`), api(`/runs/${id}/evaluation`), api(`/runs/${id}/replay`), api(`/runs/${id}/approvals`)]); setRun(loaded); setDetails({ events, metrics, evaluation, replay, approvals }); notify("Run inspection loaded", "success"); } catch (error) { notify(error.message, "error"); } finally { setBusy(false); } };
  useEffect(() => { if (initialRunId) { setRunId(initialRunId); load(initialRunId); } }, [initialRunId]);
  const decision = async (approvalId, action) => { try { await post(`/approvals/${approvalId}/${action}`, action === "reject" ? { reason: "Rejected from console" } : undefined); notify(`Approval ${action}d`, "success"); await load(); } catch (error) { notify(error.message, "error"); } };
  const resume = async () => { if (!resumeId.trim() || !run) return; try { const next = await post(`/runs/${run.run_id}/resume`, { checkpoint_id: resumeId.trim() }); setRun(next); notify("Resume started", "success"); } catch (error) { notify(error.message, "error"); } };
  return <div className="page-stack"><section className="page-intro"><div><span className="eyebrow">READ-ONLY INSPECTION</span><h2>Run inspector</h2><p>Trace, metrics, evidence, checkpoints, and approvals for one MissionRun.</p></div><div className="id-loader"><input placeholder="Paste run ID" value={runId} onChange={(event) => setRunId(event.target.value)} /><button className="button button-dark" onClick={() => load()} disabled={busy}>{busy ? "Loading…" : "Inspect"}</button></div></section>{run ? <><section className="run-banner"><div><span className="mono muted">{run.run_id}</span><h3>{run.status}</h3><p>Mission <span className="mono">{run.mission_id}</span></p></div><div className="run-banner-stats"><div><span>Events</span><strong>{run.event_count}</strong></div><div><span>Tools</span><strong>{run.tool_call_count}</strong></div><div><span>Recovery</span><strong>{run.recovery_count}</strong></div></div></section><div className="content-grid run-grid"><Panel title="Execution metrics" eyebrow="OBSERVED RUN"><div className="metrics-list">{Object.entries(details.metrics || {}).filter(([key]) => ["status", "duration_ms", "model_name", "tool_call_count", "tool_failure_count", "qa_run_test_count", "recovery_count", "checkpoint_count", "integrity_status"].includes(key)).map(([key, value]) => <div key={key}><span>{key.replaceAll("_", " ")}</span><strong>{typeof value === "object" ? JSON.stringify(value) : String(value ?? "—")}</strong></div>)}</div></Panel><Panel title="QA evaluation" eyebrow="DETERMINISTIC EVIDENCE"><div className="evaluation-status"><StatusPill value={details.evaluation?.status} /><p>{details.evaluation?.summary || "No evaluation summary"}</p></div><JsonBlock value={details.evaluation?.rules} /></Panel><Panel title="Trace timeline" eyebrow={`${details.events.length} OBSERVATIONS`} className="wide-panel"><div className="timeline">{details.events.length ? details.events.map((event) => <div className="timeline-row" key={`${event.sequence}-${event.event_type}`}><span className="timeline-seq">{String(event.sequence).padStart(2, "0")}</span><span className="timeline-dot" /><div><strong>{event.event_type.replaceAll("_", " ")}</strong><small>{new Date(event.timestamp).toLocaleTimeString()}</small></div></div>) : <Empty>No trace events.</Empty>}</div></Panel><Panel title="Approvals" eyebrow="POLICY GATE"><div className="approval-list">{details.approvals.length ? details.approvals.map((approval) => <div className="approval-row" key={approval.approval_id}><div><strong>{approval.tool_call?.tool_name || approval.tool_name}</strong><span>{approval.status} · {approval.agent_role}</span></div>{approval.status === "PENDING" && <div className="approval-actions"><button className="text-button" onClick={() => decision(approval.approval_id, "approve")}>Approve</button><button className="text-button danger" onClick={() => decision(approval.approval_id, "reject")}>Reject</button></div>}</div>) : <Empty>No approval records.</Empty>}</div></Panel><Panel title="Resume from checkpoint" eyebrow="CONTINUATION"><div className="resume-box"><input placeholder="Checkpoint ID" value={resumeId} onChange={(event) => setResumeId(event.target.value)} /><button className="button button-outline" onClick={resume}>Resume</button></div></Panel><Panel title="Replay snapshot" eyebrow="HISTORICAL VIEW" className="wide-panel"><div className="replay-summary"><div><strong>{details.replay?.timeline?.length || 0}</strong><span>timeline items</span></div><div><strong>{details.replay?.checkpoints?.length || 0}</strong><span>checkpoints</span></div><StatusPill value={details.replay?.integrity?.manifest_present ? "manifest valid" : "attention"} /></div></Panel></div></> : <Panel title="Inspect one MissionRun" eyebrow="RUN ID REQUIRED"><Empty>Paste a run ID above to see safe execution evidence and historical state.</Empty></Panel>}</div>;
}

export default App;
