import json
from uuid import UUID
from app.domain.contracts import ModelRequest
from app.domain.models import AgentState, TraceEvent
from app.domain.policy import PendingApproval, PolicyAction, PolicyDecision, PolicyEvaluator, ToolCallSnapshot
from app.models.lmstudio import ProviderError
from app.domain.handoffs import PMToDeveloperHandoff, DeveloperToQAHandoff, QAResult

TOOL_SPECS = {
    "list_files": {"name": "list_files", "description": "List files below a workspace path.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False}},
    "read_file": {"name": "read_file", "description": "Read a UTF-8 file in the workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
    "search_code": {"name": "search_code", "description": "Search text below a workspace path.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}}, "required": ["query"], "additionalProperties": False}},
    "edit_file": {"name": "edit_file", "description": "Replace text in one workspace file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "allow_multiple": {"type": "boolean"}}, "required": ["path", "old_text", "new_text"], "additionalProperties": False}},
    "run_test": {"name": "run_test", "description": "Run pytest below a workspace path.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "timeout": {"type": "integer"}}, "additionalProperties": False}},
}

class BasicAgentRuntime:
    def __init__(self, provider, compiler, tools, recorder, mission_id, run_id, checkpoint=None, policy_evaluator=None, approval_handler=None): self.provider=provider; self.compiler=compiler; self.tools=tools; self.recorder=recorder; self.mission_id=mission_id; self.run_id=run_id; self.seq=0; self.checkpoint=checkpoint; self.policy_evaluator=policy_evaluator or PolicyEvaluator(); self.approval_handler=approval_handler
    def emit(self, typ, agent_id, payload=None): self.seq+=1; self.recorder.record(TraceEvent(mission_id=self.mission_id,mission_run_id=self.run_id,agent_run_id=agent_id,sequence=len(self.recorder.for_run(self.run_id))+1,event_type=typ,payload=payload or {}))
    def run(self, agent_run_id: UUID, state: AgentState) -> AgentState:
        schema_models={"PMToDeveloperHandoff":PMToDeveloperHandoff,"DeveloperToQAHandoff":DeveloperToQAHandoff,"QAResult":QAResult}
        self.emit("agent_started",agent_run_id,{"recovery_attempt":state.recovery_attempt})
        while not state.finished:
            if state.profile is None: raise ValueError("agent skill profile is required")
            request_expected_output = (state.expected_output or "") if not state.allowed_tools else ""
            response_schema=schema_models[state.expected_output].model_json_schema() if not state.allowed_tools and state.expected_output in schema_models else None
            final_output=json.dumps(schema_models[state.expected_output].model_json_schema(),separators=(",",":")) if state.allowed_tools and state.expected_output in schema_models else ""
            prompt=self.compiler.compile({"messages":state.messages,"handoff":state.handoffs,"expected_output":request_expected_output,"final_output":final_output},state.profile)
            self.emit("prompt_compiled",agent_run_id,{"checksums":prompt["skill_checksums"]})
            self.emit("model_request",agent_run_id,{"role":state.role.value,"message_count":len(prompt["messages"])})
            try: response=self.provider.complete(ModelRequest(messages=prompt["messages"],tools=[TOOL_SPECS[n] for n in state.allowed_tools],role=state.role.value,expected_output=request_expected_output,response_schema=response_schema,metadata={"agent_run_id":str(agent_run_id)}))
            except ProviderError as error:
                self.emit("runtime_error",agent_run_id,{"category":"provider_error","provider_error_type":error.category,"metadata":error.metadata}); state.finished=True; return state
            self.emit("model_response",agent_run_id,{"kind":response.kind,"has_tool_call":response.tool_call is not None,"output_keys":sorted(response.output),"latency_ms":response.latency_ms})
            if response.kind=="tool" and response.tool_call:
                if state.allowed_tools and response.tool_call.name not in state.allowed_tools:
                    self.emit("validation_error",agent_run_id,{"category":"unauthorized_tool","tool":response.tool_call.name}); state.finished=True; return state
                try:
                    snapshot = ToolCallSnapshot.from_parts(response.tool_call.name, state.role, response.tool_call.arguments)
                    decision = self.policy_evaluator.evaluate(state.role, response.tool_call)
                except ValueError as error:
                    snapshot = None
                    decision = PolicyDecision(decision=PolicyAction.DENY, policy_id="security.tool_arguments", reason=str(error))
                proposed = {"name": response.tool_call.name}
                if snapshot is not None and decision.decision != PolicyAction.DENY:
                    proposed.update({"arguments": snapshot.arguments, "arguments_digest": snapshot.arguments_digest})
                self.emit("tool_call", agent_run_id, proposed)
                if getattr(self.policy_evaluator, "mode", "disabled") != "disabled":
                    self.emit("policy_evaluated", agent_run_id, {"decision": decision.decision, "policy_id": decision.policy_id, "reason": decision.reason, "tool_name": response.tool_call.name})
                if decision.decision == PolicyAction.DENY:
                    self.emit("validation_error", agent_run_id, {"category": "policy_denied", "reason": "policy_denied", "policy_id": decision.policy_id, "tool_name": response.tool_call.name})
                    state.finished = True
                    return state
                if decision.decision == PolicyAction.REQUIRE_APPROVAL:
                    if snapshot is None:
                        self.emit("validation_error", agent_run_id, {"category": "policy_denied", "reason": "policy_denied", "policy_id": decision.policy_id, "tool_name": response.tool_call.name})
                        state.finished = True
                        return state
                    state.waiting_approval = True
                    state.pending_tool_call = snapshot.model_dump(mode="json")
                    approval = PendingApproval(run_id=self.run_id, agent_role=state.role, tool_call=snapshot, policy_id=decision.policy_id, reason=decision.reason)
                    if self.approval_handler:
                        approval = self.approval_handler(approval, state)
                    state.pending_approval_id = approval.approval_id
                    checkpoint_id = self.checkpoint(state) if self.checkpoint else None
                    payload = {"approval_id": str(approval.approval_id), "agent_role": state.role.value, "tool_name": snapshot.tool_name, "policy_id": decision.policy_id}
                    self.emit("approval_required", agent_run_id, payload)
                    if checkpoint_id:
                        self.emit("checkpoint_created", agent_run_id, {"reason": "approval_required", "checkpoint_id": str(checkpoint_id), **payload})
                    return state
                result=self.tools.execute(response.tool_call); result_record=result.model_dump() | {"tool_name":response.tool_call.name,"arguments":response.tool_call.arguments}; self.emit("tool_result",agent_run_id,result_record); state.tool_results.append(result_record); call_id=f"call_{len(state.tool_results)}"; state.messages += [{"role":"assistant","content":"","tool_calls":[{"id":call_id,"type":"function","function":{"name":response.tool_call.name,"arguments":json.dumps(response.tool_call.arguments)}}]},{"role":"tool","tool_call_id":call_id,"content":result.output or result.error or ""}]
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
