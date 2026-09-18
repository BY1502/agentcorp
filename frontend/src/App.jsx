import { useEffect, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";
const DEFAULT_CASES = JSON.stringify([
  {
    case_id: "auth",
    name: "인증 버그",
    mission_input: "인증 만료 버그를 수정한다",
    workspace_source: "missions/demo_auth_bug/repo",
    expected_test_target: "tests",
  },
], null, 2);

const STATUS_LABELS = {
  unknown: "알 수 없음", attention: "주의", ready: "대기",
  DRAFT: "초안", PUBLISHED: "게시됨", SEALED: "봉인됨", RUNNING: "실행 중",
  COMPLETED: "완료", FAILED: "실패", EXHAUSTED: "재시도 소진", PASSED: "통과",
  passed: "통과", failed: "실패", valid: "정상", "manifest valid": "매니페스트 정상",
  PENDING: "대기 중", APPROVED: "승인됨", REJECTED: "거절됨", EXPIRED: "만료됨",
};

const EVENT_LABELS = {
  mission_started: "미션 시작", agent_started: "에이전트 시작", prompt_compiled: "프롬프트 컴파일",
  model_request: "모델 요청", model_response: "모델 응답", tool_call: "도구 호출",
  tool_result: "도구 결과", handoff_created: "핸드오프 생성", checkpoint_created: "체크포인트 생성",
  approval_required: "승인 요청", approval_approved: "승인 완료", approval_rejected: "승인 거절",
  validation_error: "검증 오류", runtime_error: "런타임 오류", agent_finished: "에이전트 종료",
  mission_finished: "미션 종료",
};

const METRIC_LABELS = {
  status: "상태", duration_ms: "소요 시간", model_name: "모델", tool_call_count: "도구 호출",
  tool_failure_count: "도구 실패", qa_run_test_count: "QA 테스트", recovery_count: "복구 횟수",
  checkpoint_count: "체크포인트", integrity_status: "무결성",
};

const DEPARTMENTS = [
  { key: "pm", view: "missions", name: "PM 본부", member: "미나", role: "프로덕트 매니저", icon: "🧑🏻‍💼", task: "미션 설계 및 우선순위", accent: "pink" },
  { key: "dev", view: "missions", name: "개발 스튜디오", member: "준", role: "시니어 개발자", icon: "🧑🏻‍💻", task: "코드 수정 및 구현", accent: "cyan" },
  { key: "qa", view: "runs", name: "QA 랩", member: "소라", role: "품질 검증 담당", icon: "🧑🏻‍🔬", task: "테스트 evidence 검증", accent: "violet" },
];

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

function statusClass(value) {
  return String(value || "unknown").toLowerCase().replaceAll(" ", "-");
}

function StatusPill({ value }) {
  const raw = value || "unknown";
  return <span className={`status-pill status-${statusClass(raw)}`}>{STATUS_LABELS[raw] || raw}</span>;
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
  return <pre className="json-block">{value ? JSON.stringify(value, null, 2) : "불러온 데이터가 없습니다"}</pre>;
}

function Empty({ children }) {
  return <div className="empty-state"><span className="empty-mark">◇</span><p>{children}</p></div>;
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
      <header className="topbar"><div><span className="topbar-kicker">AGENTCORP // 가상 본부</span><h1>{viewTitle(view)}</h1></div><button className="button button-quiet" onClick={refreshOverview}>↻ 새로고침</button></header>
      {notice && <div className={`notice notice-${notice.type}`}>{notice.message}</div>}
      {view === "overview" && <Overview data={overview} navigate={navigate} />}
      {view === "suites" && <SuitesView suites={overview.suites} refresh={refreshOverview} notify={notify} />}
      {view === "experiments" && <ExperimentsView notify={notify} />}
      {view === "missions" && <MissionsView notify={notify} inspectRun={inspectRun} />}
      {view === "runs" && <RunsView initialRunId={openRun} notify={notify} />}
    </main>
  </div>;
}

function viewTitle(view) {
  return { overview: "가상 작전실", suites: "벤치마크 월드", experiments: "실험 시뮬레이션", missions: "미션 센터", runs: "실행 관제" }[view] || "AgentCorp";
}

function Sidebar({ view, navigate, health }) {
  const items = [
    ["overview", "⌂", "작전실"], ["missions", "↗", "미션 센터"], ["experiments", "◇", "실험실"],
    ["suites", "▦", "벤치마크 월드"], ["runs", "◌", "실행 관제"],
  ];
  return <aside className="sidebar">
    <div className="brand"><div className="brand-orbit"><span /></div><div><strong>AgentCorp</strong><small>AI 가상 회사</small></div></div>
    <div className="sidebar-label">본부 네비게이션</div>
    <nav>{items.map(([key, icon, label]) => <button key={key} className={view === key ? "nav-item active" : "nav-item"} onClick={() => navigate(key)}><span className="nav-icon">{icon}</span>{label}{key === "runs" && <span className="nav-arrow">→</span>}</button>)}</nav>
    <div className="sidebar-spacer" />
    <div className="agent-roster"><span className="roster-title">현재 접속 중인 직원</span><div><i className="roster-orb pm" /><span>PM</span><b>대기</b></div><div><i className="roster-orb dev" /><span>Developer</span><b>대기</b></div><div><i className="roster-orb qa" /><span>QA</span><b>대기</b></div></div>
    <div className="runtime-card"><div className="live-dot" /><div><strong>런타임 온라인</strong><span>{health?.status === "ok" ? "FastAPI 연결됨" : "API 연결 필요"}</span></div></div>
    <div className="sidebar-foot">LOCAL / 기본 안전<br /><span>관찰 · 재현 · 비교 가능</span></div>
  </aside>;
}

function Character({ department }) {
  return <div className={`character character-${department.accent}`} aria-label={`${department.member} 캐릭터`}>
    <div className="character-aura" />
    <div className="character-head">{department.icon}</div>
    <div className="character-body"><span>{department.key.toUpperCase()}</span></div>
    <div className="character-shadow" />
  </div>;
}

function DepartmentCard({ department, active, onEnter }) {
  return <button className={`department-card department-${department.accent}`} onClick={onEnter}>
    <div className="department-top"><span className="room-id">ROOM / {department.key.toUpperCase()}</span><span className="room-status"><i />{active ? "작업 중" : "대기 중"}</span></div>
    <div className="department-main"><Character department={department} /><div className="employee-copy"><span className="employee-role">{department.role}</span><strong>{department.member}</strong><small>{department.task}</small></div></div>
    <div className="department-desk"><span /><span /><span /></div>
    <div className="department-foot"><span>{department.name}</span><b>입장 ↗</b></div>
  </button>;
}

function OfficeFloor({ data, navigate }) {
  const aggregate = data.aggregate || {};
  const active = (aggregate.run_count || 0) > 0;
  return <section className="office-shell">
    <div className="office-topbar"><div><span className="eyebrow">AGENTCORP 본사 / 서울-01</span><h2>AI 회사 본부</h2><p>실제 직원처럼 배치된 에이전트들이 이 공간에서 미션을 처리합니다.</p></div><div className="office-live"><span className="live-badge"><i /> LIVE</span><span>직원 3명 · {active ? "작전 진행 중" : "첫 작전 대기"}</span></div></div>
    <div className="office-content"><div className="office-floor"><div className="floor-label">MAIN FLOOR / 01</div><div className="floor-lines" /><div className="department-grid">{DEPARTMENTS.map((department) => <DepartmentCard key={department.key} department={department} active={active} onEnter={() => navigate(department.view)} />)}</div><div className="office-lounge"><span>AC</span><small>공용 라운지</small></div><div className="floor-route route-one" /><div className="floor-route route-two" /></div><aside className="company-feed"><div className="feed-heading"><div><span className="eyebrow">회사 활동</span><h3>오늘의 흐름</h3></div><span className="feed-count">LIVE</span></div><div className="feed-item"><span className="feed-time">NOW</span><div><strong>실행 기록 수집 중</strong><small>{aggregate.run_count ?? 0}개의 MissionRun 관측됨</small></div></div><div className="feed-item"><span className="feed-time">QA</span><div><strong>품질 게이트 대기</strong><small>run_test evidence를 확인합니다</small></div></div><div className="feed-item"><span className="feed-time">SYS</span><div><strong>월드 동기화 완료</strong><small>재현 가능한 실행 환경 유지</small></div></div><div className="company-note"><span>회사 상태</span><strong>모든 시스템 정상</strong><i /></div></aside></div>
  </section>;
}

function Overview({ data, navigate }) {
  const aggregate = data.aggregate || {};
  const models = data.models || [];
  return <div className="page-stack">
    <OfficeFloor data={data} navigate={navigate} />
    <section className="hero"><div className="hero-copy"><span className="eyebrow">AGENTCORP 가상 본부</span><h2>모든 실행을<br /><em>하나의 세계에서.</em></h2><p>PM · Developer · QA 에이전트가 미션을 수행하는 실시간 AI 회사 시뮬레이션입니다.</p><div className="hero-readout"><span><i className="signal-dot" /> 시스템 정상</span><span>월드 시드 #V01</span></div></div><div className="hero-art"><div className="scene-grid" /><div className="hero-ring ring-one" /><div className="hero-ring ring-two" /><div className="hero-core">AC<span>·</span></div><div className="hero-node node-pm">PM</div><div className="hero-node node-dev">DEV</div><div className="hero-node node-qa">QA</div><div className="hero-caption">LIVE WORLD<br />OBSERVE / DECIDE</div></div></section>
    <div className="kpi-grid"><Kpi label="전체 실행" value={aggregate.run_count ?? 0} hint={`${aggregate.evaluated_run_count ?? 0}개 평가됨`} /><Kpi label="최종 통과율" value={aggregate.terminal_pass_rate == null ? "—" : `${Math.round(aggregate.terminal_pass_rate * 100)}%`} hint={`${aggregate.passed_count ?? 0}개 통과`} tone="teal" /><Kpi label="도구 호출" value={aggregate.tool_call_count ?? 0} hint={`${aggregate.tool_failure_count ?? 0}개 실패`} tone="orange" /><Kpi label="복구 실행" value={aggregate.recovery_run_count ?? 0} hint={`${aggregate.recovered_to_pass_count ?? 0}개 회복`} tone="violet" /></div>
    <div className="content-grid overview-grid"><Panel title="에이전트 성능" eyebrow="PHASE 8 · 관측 집계" action={<button className="text-button" onClick={() => navigate("experiments")}>실험실 열기 →</button>}>
      {models.length ? <div className="model-list">{models.map((model) => <div className="model-row" key={model.group_key?.join("/") || model.model_id}><div className="model-avatar">{(model.model_name || "?").slice(0, 1).toUpperCase()}</div><div className="model-info"><strong>{model.model_name || "이름 없는 모델"}</strong><span>{model.provider_type || "—"} · {model.run_count}회 실행</span></div><div className="model-result"><strong>{model.terminal_pass_rate == null ? "—" : `${Math.round(model.terminal_pass_rate * 100)}%`}</strong><span>통과율</span></div><StatusPill value={model.failed_count ? "attention" : "ready"} /></div>)}</div> : <Empty>아직 실행 기록이 없습니다. 미션을 시작하거나 실험을 실행하세요.</Empty>}
    </Panel><Panel title="벤치마크 월드" eyebrow="PHASE 10 · 재사용 가능한 월드" action={<button className="text-button" onClick={() => navigate("suites")}>월드 관리 →</button>}>
      {data.suites?.length ? <div className="suite-mini-list">{data.suites.slice(0, 4).map((suite) => <div className="suite-mini" key={`${suite.suite_id}:${suite.version}`}><span className="suite-icon">{suite.status === "PUBLISHED" ? "✓" : "·"}</span><div><strong>{suite.suite_id} <span>v{suite.version}</span></strong><small>{suite.cases.length}개 시나리오 · {STATUS_LABELS[suite.status] || suite.status}</small></div><span className="mini-arrow">→</span></div>)}</div> : <Empty>재사용 가능한 벤치마크 월드를 만들어보세요.</Empty>}
    </Panel></div>
    <section className="quick-actions"><div><span className="eyebrow">빠른 입장</span><h3>다음 행동을 선택하세요</h3></div><button onClick={() => navigate("missions")}><span>01</span><strong>미션 실행</strong><small>PM → Developer → QA</small> <b>↗</b></button><button onClick={() => navigate("experiments")}><span>02</span><strong>모델 비교</strong><small>실험 매트릭스 · 분석</small> <b>↗</b></button><button onClick={() => navigate("suites")}><span>03</span><strong>월드 만들기</strong><small>버전 고정 워크로드</small> <b>↗</b></button></section>
  </div>;
}

function SuitesView({ suites, refresh, notify }) {
  const [form, setForm] = useState({ suite_id: "coding-core", version: "1", name: "코딩 코어", description: "재사용 가능한 코딩 워크로드", cases: DEFAULT_CASES });
  const [busy, setBusy] = useState(false);
  const update = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.value }));
  const createSuite = async (event) => {
    event.preventDefault(); setBusy(true);
    try {
      const cases = JSON.parse(form.cases);
      await post("/benchmark-suites", { suite_id: form.suite_id.trim(), version: Number(form.version), name: form.name.trim(), description: form.description.trim() || null, cases });
      notify("벤치마크 월드 초안을 생성했습니다", "success"); await refresh();
    } catch (error) { notify(error.message || "시나리오 JSON을 확인하세요", "error"); } finally { setBusy(false); }
  };
  const publish = async (suite) => { try { await post(`/benchmark-suites/${encodeURIComponent(suite.suite_id)}/versions/${suite.version}/publish`); notify(`${suite.suite_id} v${suite.version} 월드를 게시했습니다`, "success"); await refresh(); } catch (error) { notify(error.message, "error"); } };
  return <div className="page-stack"><section className="page-intro"><div><span className="eyebrow">재사용 가능한 월드</span><h2>벤치마크 월드</h2><p>반복 가능한 에이전트 시나리오를 버전으로 고정합니다.</p></div><div className="intro-stat"><strong>{suites.length}</strong><span>저장된 버전</span></div></section><div className="content-grid suites-layout"><Panel title="새 월드 생성" eyebrow="시나리오 정의"><form className="form-stack" onSubmit={createSuite}><div className="form-row"><label>월드 ID<input value={form.suite_id} onChange={update("suite_id")} required /></label><label>버전<input type="number" min="1" value={form.version} onChange={update("version")} required /></label></div><label>이름<input value={form.name} onChange={update("name")} required /></label><label>설명<span className="optional">선택</span><input value={form.description} onChange={update("description")} /></label><label>시나리오 <span className="field-note">순서가 있는 JSON 배열</span><textarea className="code-input" rows="12" value={form.cases} onChange={update("cases")} required /></label><button className="button button-dark" disabled={busy}>{busy ? "생성 중…" : "초안 생성"}</button></form></Panel><Panel title="월드 보관소" eyebrow="저장된 정의"><div className="registry-list">{suites.length ? suites.map((suite) => <div className="registry-card" key={`${suite.suite_id}:${suite.version}`}><div className="registry-top"><div><span className="suite-icon large">▦</span><div className="registry-title"><strong>{suite.suite_id}</strong><span>v{suite.version}</span></div></div><StatusPill value={suite.status} /></div><p>{suite.description || "설명 없음"}</p><div className="registry-meta"><span>{suite.cases.length}개 시나리오</span><span>{suite.spec_digest ? `${suite.spec_digest.slice(0, 10)}…` : "다이제스트 대기"}</span><span>{new Date(suite.created_at).toLocaleDateString("ko-KR")}</span></div>{suite.status === "DRAFT" && <button className="button button-outline full" onClick={() => publish(suite)}>버전 게시</button>}</div>) : <Empty>아직 벤치마크 월드가 없습니다.</Empty>}</div></Panel></div></div>;
}

function ExperimentsView({ notify }) {
  const [source, setSource] = useState("suite");
  const [form, setForm] = useState({ name: "Qwen 비교 실험", suite_id: "coding-core", suite_version: "1", models: "fake-default", repetitions: "1", cases: DEFAULT_CASES });
  const [experiment, setExperiment] = useState(null); const [analytics, setAnalytics] = useState(null); const [lookupId, setLookupId] = useState(""); const [busy, setBusy] = useState(false);
  const update = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.value }));
  const create = async (event) => { event.preventDefault(); setBusy(true); try { const payload = { name: form.name.trim(), models: form.models.split(",").map((model_id) => ({ model_id: model_id.trim() })).filter((item) => item.model_id), repetitions: Number(form.repetitions) }; if (source === "suite") payload.benchmark_suite = { suite_id: form.suite_id.trim(), version: Number(form.suite_version) }; else payload.cases = JSON.parse(form.cases); const created = await post("/experiments", payload); setExperiment(created); setAnalytics(null); notify("실험 초안을 생성했습니다", "success"); } catch (error) { notify(error.message || "실험 입력을 확인하세요", "error"); } finally { setBusy(false); } };
  const load = async (id = lookupId) => { if (!id.trim()) return notify("실험 ID를 입력하세요", "error"); try { setExperiment(await api(`/experiments/${id.trim()}`)); setAnalytics(null); } catch (error) { notify(error.message, "error"); } };
  const action = async (verb) => { if (!experiment) return; setBusy(true); try { const next = await post(`/experiments/${experiment.experiment_id}/${verb}`); setExperiment(next); notify(verb === "seal" ? "실험 조건을 봉인했습니다" : "실험 실행을 완료했습니다", "success"); } catch (error) { notify(error.message, "error"); } finally { setBusy(false); } };
  const inspect = async () => { if (!experiment) return; try { setAnalytics(await api(`/experiments/${experiment.experiment_id}/analytics`)); } catch (error) { notify(error.message, "error"); } };
  return <div className="page-stack"><section className="page-intro"><div><span className="eyebrow">통제된 시뮬레이션</span><h2>실험실</h2><p>워크로드와 모델 조건을 고정하고 결과를 비교합니다.</p></div><div className="id-loader"><input placeholder="실험 ID 불러오기" value={lookupId} onChange={(event) => setLookupId(event.target.value)} /><button className="button button-outline" onClick={() => load()}>불러오기</button></div></section><div className="content-grid experiments-layout"><Panel title="새 실험" eyebrow="하나의 워크로드 소스"><form className="form-stack" onSubmit={create}><label>실험 이름<input value={form.name} onChange={update("name")} required /></label><div className="segmented"><button type="button" className={source === "suite" ? "selected" : ""} onClick={() => setSource("suite")}>게시된 월드</button><button type="button" className={source === "inline" ? "selected" : ""} onClick={() => setSource("inline")}>직접 입력</button></div>{source === "suite" ? <div className="form-row"><label>월드 ID<input value={form.suite_id} onChange={update("suite_id")} required /></label><label>버전<input type="number" min="1" value={form.suite_version} onChange={update("suite_version")} required /></label></div> : <label>시나리오 <span className="field-note">순서가 있는 JSON 배열</span><textarea className="code-input" rows="10" value={form.cases} onChange={update("cases")} required /></label>}<div className="form-row"><label>모델 ID <span className="field-note">쉼표로 구분</span><input value={form.models} onChange={update("models")} required /></label><label>반복 횟수<input type="number" min="1" value={form.repetitions} onChange={update("repetitions")} required /></label></div><button className="button button-dark" disabled={busy}>{busy ? "작업 중…" : "실험 생성"}</button></form></Panel><Panel title={experiment ? "선택된 실험" : "선택된 실험 없음"} eyebrow="생명주기"><ExperimentCard experiment={experiment} action={action} inspect={inspect} analytics={analytics} busy={busy} /></Panel></div></div>;
}

function ExperimentCard({ experiment, action, inspect, analytics, busy }) {
  if (!experiment) return <Empty>실험을 만들거나 불러오면 생명주기를 제어할 수 있습니다.</Empty>;
  return <div className="experiment-detail"><div className="detail-heading"><div><span className="mono muted">{experiment.experiment_id}</span><h3>{experiment.name}</h3></div><StatusPill value={experiment.status} /></div>{experiment.benchmark_suite && <div className="provenance"><span>월드 출처</span><strong>{experiment.benchmark_suite.suite_id} <i>v{experiment.benchmark_suite.version}</i></strong><code>{experiment.benchmark_suite.digest?.slice(0, 16)}…</code></div>}<div className="detail-stats"><div><strong>{experiment.cases?.length || 0}</strong><span>시나리오</span></div><div><strong>{experiment.models?.length || 0}</strong><span>모델</span></div><div><strong>{experiment.expected_run_count || 0}</strong><span>예상 실행</span></div></div><div className="case-order"><span className="eyebrow">고정된 시나리오 순서</span>{experiment.cases?.map((item, index) => <div key={item.case_id}><b>{String(index + 1).padStart(2, "0")}</b><span>{item.case_id}</span><small>{item.name}</small></div>)}</div><div className="action-row"><button className="button button-outline" disabled={experiment.status !== "DRAFT" || busy} onClick={() => action("seal")}>조건 봉인</button><button className="button button-dark" disabled={experiment.status === "DRAFT" || experiment.status === "COMPLETED" || busy} onClick={() => action("execute")}>실험 실행</button><button className="button button-teal" disabled={experiment.status === "DRAFT" || busy} onClick={inspect}>분석 보기</button></div>{analytics && <AnalyticsPanel report={analytics} />}</div>;
}

function AnalyticsPanel({ report }) {
  const aggregate = report.overall || {};
  return <div className="analytics-panel"><div className="analytics-heading"><span className="eyebrow">실험 분석 결과</span><StatusPill value={report.integrity_status} /></div><div className="analytics-kpis"><Kpi label="생성된 실행" value={`${report.materialized_run_count}/${report.expected_run_count}`} /><Kpi label="통과율" value={aggregate.terminal_pass_rate == null ? "—" : `${Math.round(aggregate.terminal_pass_rate * 100)}%`} tone="teal" /><Kpi label="도구 호출" value={aggregate.tool_call_count} tone="orange" /></div><div className="analytics-foot"><span>{report.terminal_run_count}개 종료 · {report.non_terminal_run_count}개 진행 중</span><span>{report.integrity_issues?.length ? report.integrity_issues.join(", ") : "매핑 무결성 확인됨"}</span></div></div>;
}

function MissionsView({ notify, inspectRun }) {
  const [form, setForm] = useState({ title: "데모 인증 미션", fixture: "missions/demo_auth_bug/repo", model_id: "fake-default", approval_mode: "disabled" });
  const [mission, setMission] = useState(null); const [run, setRun] = useState(null); const [busy, setBusy] = useState(false);
  const update = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.value }));
  const create = async (event) => { event.preventDefault(); setBusy(true); try { setMission(await post("/missions", { title: form.title, fixture: form.fixture })); setRun(null); notify("미션을 생성했습니다", "success"); } catch (error) { notify(error.message, "error"); } finally { setBusy(false); } };
  const start = async () => { if (!mission) return; setBusy(true); try { const result = await post(`/missions/${mission.id}/runs`, { model_id: form.model_id, approval_mode: form.approval_mode }); setRun(result); notify("미션 실행이 완료되었습니다", "success"); } catch (error) { notify(error.message, "error"); } finally { setBusy(false); } };
  return <div className="page-stack"><section className="page-intro"><div><span className="eyebrow">수직 슬라이스</span><h2>미션 센터</h2><p>로컬 결정성 provider로 PM → Developer → QA 작전을 실행합니다.</p></div><div className="mission-path"><span>PM</span><b>→</b><span>DEV</span><b>→</b><span>QA</span></div></section><div className="content-grid mission-layout"><Panel title="미션 입장" eyebrow="미션 정의"><form className="form-stack" onSubmit={create}><label>미션 제목<input value={form.title} onChange={update("title")} required /></label><label>픽스처 경로<input value={form.fixture} onChange={update("fixture")} required /></label><div className="form-row"><label>모델 ID<input value={form.model_id} onChange={update("model_id")} /></label><label>승인 모드<select value={form.approval_mode} onChange={update("approval_mode")}><option value="disabled">자동 실행</option><option value="policy">정책 승인</option></select></label></div><div className="action-row"><button className="button button-dark" disabled={busy}>{busy ? "작업 중…" : "미션 생성"}</button>{mission && <button type="button" className="button button-teal" onClick={start} disabled={busy}>실행 시작 ↗</button>}</div></form></Panel><Panel title={mission ? "미션 준비 완료" : "선택된 미션 없음"} eyebrow="실행 핸드오프">{mission ? <div className="mission-result"><div className="result-icon">✓</div><span className="mono">{mission.id}</span><h3>{mission.title}</h3><p>픽스처 <code>{mission.fixture}</code></p>{run && <div className="run-result"><div><span>최근 실행</span><strong><StatusPill value={run.status} /></strong></div><span className="mono">{run.run_id}</span><button className="button button-outline full" onClick={() => inspectRun(run.run_id)}>실행 관제 열기 →</button></div>}</div> : <Empty>미션을 생성하면 가상 작전이 시작됩니다.</Empty>}</Panel></div></div>;
}

function RunsView({ initialRunId, notify }) {
  const [runId, setRunId] = useState(initialRunId || ""); const [run, setRun] = useState(null); const [details, setDetails] = useState({ events: [], metrics: null, evaluation: null, replay: null, approvals: [] }); const [busy, setBusy] = useState(false); const [resumeId, setResumeId] = useState("");
  const load = async (id = runId) => { if (!id.trim()) return notify("실행 ID를 입력하세요", "error"); setBusy(true); try { const [loaded, events, metrics, evaluation, replay, approvals] = await Promise.all([api(`/runs/${id}`), api(`/runs/${id}/events`), api(`/runs/${id}/metrics`), api(`/runs/${id}/evaluation`), api(`/runs/${id}/replay`), api(`/runs/${id}/approvals`)]); setRun(loaded); setDetails({ events, metrics, evaluation, replay, approvals }); notify("실행 관제 데이터를 불러왔습니다", "success"); } catch (error) { notify(error.message, "error"); } finally { setBusy(false); } };
  useEffect(() => { if (initialRunId) { setRunId(initialRunId); load(initialRunId); } }, [initialRunId]);
  const decision = async (approvalId, action) => { try { await post(`/approvals/${approvalId}/${action}`, action === "reject" ? { reason: "콘솔에서 거절" } : undefined); notify(action === "approve" ? "도구 실행을 승인했습니다" : "도구 실행을 거절했습니다", "success"); await load(); } catch (error) { notify(error.message, "error"); } };
  const resume = async () => { if (!resumeId.trim() || !run) return; try { const next = await post(`/runs/${run.run_id}/resume`, { checkpoint_id: resumeId.trim() }); setRun(next); notify("체크포인트에서 재개했습니다", "success"); } catch (error) { notify(error.message, "error"); } };
  return <div className="page-stack"><section className="page-intro"><div><span className="eyebrow">읽기 전용 관제</span><h2>실행 관제</h2><p>하나의 MissionRun에 대한 trace, metrics, evidence, checkpoint, approval을 확인합니다.</p></div><div className="id-loader"><input placeholder="실행 ID 붙여넣기" value={runId} onChange={(event) => setRunId(event.target.value)} /><button className="button button-dark" onClick={() => load()} disabled={busy}>{busy ? "불러오는 중…" : "관제 열기"}</button></div></section>{run ? <><section className="run-banner"><div><span className="mono muted">{run.run_id}</span><h3>{STATUS_LABELS[run.status] || run.status}</h3><p>미션 <span className="mono">{run.mission_id}</span></p></div><div className="run-banner-stats"><div><span>관찰 이벤트</span><strong>{run.event_count}</strong></div><div><span>도구 호출</span><strong>{run.tool_call_count}</strong></div><div><span>복구</span><strong>{run.recovery_count}</strong></div></div></section><div className="content-grid run-grid"><Panel title="실행 metrics" eyebrow="관측된 실행"><div className="metrics-list">{Object.entries(details.metrics || {}).filter(([key]) => Object.hasOwn(METRIC_LABELS, key)).map(([key, value]) => <div key={key}><span>{METRIC_LABELS[key]}</span><strong>{typeof value === "object" ? JSON.stringify(value) : String(value ?? "—")}</strong></div>)}</div></Panel><Panel title="QA 평가" eyebrow="결정적 evidence"><div className="evaluation-status"><StatusPill value={details.evaluation?.status} /><p>{details.evaluation?.summary || "평가 요약이 없습니다"}</p></div><JsonBlock value={details.evaluation?.rules} /></Panel><Panel title="trace 타임라인" eyebrow={`${details.events.length}개 관찰`} className="wide-panel"><div className="timeline">{details.events.length ? details.events.map((event) => <div className="timeline-row" key={`${event.sequence}-${event.event_type}`}><span className="timeline-seq">{String(event.sequence).padStart(2, "0")}</span><span className="timeline-dot" /><div><strong>{EVENT_LABELS[event.event_type] || event.event_type.replaceAll("_", " ")}</strong><small>{new Date(event.timestamp).toLocaleTimeString("ko-KR")}</small></div></div>) : <Empty>trace 이벤트가 없습니다.</Empty>}</div></Panel><Panel title="승인 게이트" eyebrow="정책 확인"><div className="approval-list">{details.approvals.length ? details.approvals.map((approval) => <div className="approval-row" key={approval.approval_id}><div><strong>{approval.tool_call?.tool_name || approval.tool_name}</strong><span>{STATUS_LABELS[approval.status] || approval.status} · {approval.agent_role}</span></div>{approval.status === "PENDING" && <div className="approval-actions"><button className="text-button" onClick={() => decision(approval.approval_id, "approve")}>승인</button><button className="text-button danger" onClick={() => decision(approval.approval_id, "reject")}>거절</button></div>}</div>) : <Empty>승인 기록이 없습니다.</Empty>}</div></Panel><Panel title="체크포인트에서 재개" eyebrow="CONTINUATION"><div className="resume-box"><input placeholder="체크포인트 ID" value={resumeId} onChange={(event) => setResumeId(event.target.value)} /><button className="button button-outline" onClick={resume}>재개</button></div></Panel><Panel title="Replay 스냅샷" eyebrow="과거 실행 보기" className="wide-panel"><div className="replay-summary"><div><strong>{details.replay?.timeline?.length || 0}</strong><span>타임라인 항목</span></div><div><strong>{details.replay?.checkpoints?.length || 0}</strong><span>체크포인트</span></div><StatusPill value={details.replay?.integrity?.manifest_present ? "manifest valid" : "attention"} /></div></Panel></div></> : <Panel title="MissionRun 하나를 관제하세요" eyebrow="실행 ID 필요"><Empty>위에 실행 ID를 입력하면 안전한 실행 evidence와 historical state를 확인할 수 있습니다.</Empty></Panel>}</div>;
}

export default App;
