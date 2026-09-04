from uuid import UUID
from app.domain.contracts import ModelRequest
from app.domain.models import AgentState, TraceEvent
from app.models.lmstudio import ProviderError
from app.domain.handoffs import PMToDeveloperHandoff, DeveloperToQAHandoff, QAResult

class BasicAgentRuntime:
    def __init__(self, provider, compiler, tools, recorder, mission_id, run_id, checkpoint=None): self.provider=provider; self.compiler=compiler; self.tools=tools; self.recorder=recorder; self.mission_id=mission_id; self.run_id=run_id; self.seq=0; self.checkpoint=checkpoint
    def emit(self, typ, agent_id, payload=None): self.seq+=1; self.recorder.record(TraceEvent(mission_id=self.mission_id,mission_run_id=self.run_id,agent_run_id=agent_id,sequence=len(self.recorder.for_run(self.run_id))+1,event_type=typ,payload=payload or {}))
    def run(self, agent_run_id: UUID, state: AgentState) -> AgentState:
        self.emit("agent_started",agent_run_id)
        while not state.finished:
            if state.profile is None: raise ValueError("agent skill profile is required")
            prompt=self.compiler.compile({"messages":state.messages,"handoff":state.handoffs,"expected_output":state.expected_output},state.profile)
            self.emit("prompt_compiled",agent_run_id,{"checksums":prompt["skill_checksums"]})
            self.emit("model_request",agent_run_id,{"role":state.role.value,"message_count":len(prompt["messages"])})
            schema_models={"PMToDeveloperHandoff":PMToDeveloperHandoff,"DeveloperToQAHandoff":DeveloperToQAHandoff,"QAResult":QAResult}
            schema=schema_models[state.expected_output].model_json_schema() if state.expected_output in schema_models else None
            try: response=self.provider.complete(ModelRequest(messages=prompt["messages"],tools=[{"name":n} for n in state.allowed_tools],role=state.role.value,expected_output=state.expected_output or "",response_schema=schema,metadata={"agent_run_id":str(agent_run_id)}))
            except ProviderError as error:
                self.emit("runtime_error",agent_run_id,{"category":"provider_error","provider_error_type":error.category,"metadata":error.metadata}); state.finished=True; return state
            self.emit("model_response",agent_run_id,{"kind":response.kind,"has_tool_call":response.tool_call is not None,"output_keys":sorted(response.output),"latency_ms":response.latency_ms})
            if response.kind=="tool" and response.tool_call:
                if state.allowed_tools and response.tool_call.name not in state.allowed_tools:
                    self.emit("validation_error",agent_run_id,{"category":"unauthorized_tool","tool":response.tool_call.name}); state.finished=True; return state
                self.emit("tool_call",agent_run_id,response.tool_call.model_dump()); result=self.tools.execute(response.tool_call); self.emit("tool_result",agent_run_id,result.model_dump()); state.tool_results.append(result.model_dump()); state.messages += [{"role":"tool","content":result.output or result.error or ""}]
                if result.success and response.tool_call.name == "edit_file" and self.checkpoint:
                    self.checkpoint(state)
                    self.emit("checkpoint_created",agent_run_id,{"reason":"successful_edit_file"})
            else:
                try:
                    validators={"PMToDeveloperHandoff":PMToDeveloperHandoff,"DeveloperToQAHandoff":DeveloperToQAHandoff,"QAResult":QAResult}
                    if state.expected_output in validators: validators[state.expected_output](**response.output)
                    state.handoffs.update(response.output); state.finished=True
                except Exception as error:
                    self.emit("validation_error",agent_run_id,{"category":"structured_output","schema":state.expected_output or "unknown","error":str(error)[:160]}); state.finished=True
        self.emit("agent_finished",agent_run_id); return state
