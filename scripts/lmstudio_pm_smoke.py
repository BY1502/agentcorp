import json
import os
from pathlib import Path
from uuid import uuid4
from app.domain.models import AgentState, Role, Level, SkillProfile, TraceEvent
from app.domain.handoffs import PMToDeveloperHandoff
from app.models.factory import ProviderFactory
from app.domain.models import ModelConfig
from app.skills.filesystem import FilesystemSkillLoader, DeterministicPromptCompiler
from app.runtime.agent import BasicAgentRuntime
from app.tracing.recorder import InMemoryTraceRecorder

def main():
    config=ModelConfig(model_id="lmstudio-qwen",provider_type="lmstudio",model_name=os.getenv("AGENTCORP_LMSTUDIO_MODEL","qwen/qwen3.8-27b"),base_url=os.getenv("AGENTCORP_LMSTUDIO_BASE_URL","http://127.0.0.1:1234"),timeout=float(os.getenv("AGENTCORP_LMSTUDIO_TIMEOUT","180")))
    from app.models.lmstudio import LMStudioProvider
    provider=ProviderFactory(providers={"lmstudio":lambda c:LMStudioProvider(c.model_name,c.base_url,c.timeout)}).create(config)
    run,mission,agent=uuid4(),uuid4(),uuid4(); recorder=InMemoryTraceRecorder(); state=AgentState(mission_id=mission,mission_run_id=run,agent_run_id=agent,role=Role.PM,level=Level.SENIOR,profile=SkillProfile(name="pm",skills=("common/tool_usage.md","common/handoff.md","roles/pm/SKILL.md")),expected_output="PMToDeveloperHandoff")
    result=BasicAgentRuntime(provider,DeterministicPromptCompiler(FilesystemSkillLoader(Path("skills"))),object(),recorder,mission,run).run(agent,state)
    validation = False
    if result.handoffs:
        try:
            PMToDeveloperHandoff.model_validate(result.handoffs)
            validation = True
        except Exception:
            validation = False
    response_event = next((e for e in recorder.events if e.event_type == "model_response"), None)
    failure_events = [e.payload for e in recorder.events if e.event_type in {"runtime_error", "validation_error"}]
    latency_ms = response_event.payload.get("latency_ms") if response_event else None
    if latency_ms is None and failure_events:
        latency_ms = failure_events[0].get("metadata", {}).get("latency_ms")
    report = {
        "model": config.model_name,
        "pm_validation": "passed" if validation else "failed",
        "latency_ms": latency_ms,
        "failure_events": failure_events,
        "event_types": [e.event_type for e in recorder.events],
    }
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if validation else 1)
if __name__ == "__main__": main()
