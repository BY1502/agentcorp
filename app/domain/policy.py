import hashlib
import json
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .models import Role


class PolicyAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)
    decision: PolicyAction
    policy_id: str
    reason: str


class ToolCallSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    tool_name: str
    agent_role: Role
    arguments: dict[str, Any] = Field(default_factory=dict)
    arguments_digest: str

    @property
    def safe_arguments(self) -> dict[str, Any]:
        return self.arguments

    @classmethod
    def from_parts(cls, tool_name: str, agent_role: Role | str, arguments: dict[str, Any]):
        try:
            encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        except (TypeError, ValueError) as error:
            raise ValueError("tool arguments are not JSON serializable") from error
        if len(encoded) > 65536:
            raise ValueError("tool arguments exceed the approval size limit")
        if _contains_sensitive(arguments):
            raise ValueError("secret-bearing tool arguments are not supported")
        return cls(
            tool_name=tool_name,
            agent_role=Role(agent_role),
            arguments=json.loads(encoded),
            arguments_digest=hashlib.sha256(encoded).hexdigest(),
        )


class PendingApproval(BaseModel):
    approval_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    agent_role: Role
    tool_call: ToolCallSnapshot
    policy_id: str
    reason: str
    status: str = "PENDING"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "credential",
    "credential_ref",
    "reasoning",
    "reasoning_content",
}
_SENSITIVE_TEXT = re.compile(r"(?i)\b(api[_-]?key|authorization|access[_-]?token|password|secret|credential(?:_ref)?|reasoning(?:[_-]?content)?)\s*[:=]")


def _contains_sensitive(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _SENSITIVE_KEYS or _contains_sensitive(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive(item) for item in value)
    return isinstance(value, str) and bool(_SENSITIVE_TEXT.search(value))


class PolicyEvaluator:
    def __init__(self, mode: str = "disabled", policy_version: str = "1", rules: dict[str, str] | None = None):
        if mode not in {"disabled", "policy"}:
            raise ValueError(f"unknown approval mode: {mode}")
        self.mode = mode
        self.policy_version = policy_version
        self.rules = {name: PolicyAction(value) for name, value in (rules or {}).items()}

    def evaluate(self, role: Role | str, tool_call: Any) -> PolicyDecision:
        del role
        if _contains_sensitive(getattr(tool_call, "arguments", {})):
            return PolicyDecision(
                decision=PolicyAction.DENY,
                policy_id="security.tool_arguments",
                reason="secret-bearing tool arguments are not supported",
            )
        if self.mode == "disabled":
            return PolicyDecision(
                decision=PolicyAction.ALLOW,
                policy_id="compatibility.disabled",
                reason="approval policy is disabled",
            )
        action = self.rules.get(getattr(tool_call, "name", ""))
        if action is None:
            action = {
                "list_files": PolicyAction.ALLOW,
                "read_file": PolicyAction.ALLOW,
                "search_code": PolicyAction.ALLOW,
                "run_test": PolicyAction.ALLOW,
                "edit_file": PolicyAction.REQUIRE_APPROVAL,
            }.get(getattr(tool_call, "name", ""), PolicyAction.DENY)
        reasons = {
            PolicyAction.ALLOW: "tool is safe to execute automatically",
            PolicyAction.DENY: "tool execution is denied by policy",
            PolicyAction.REQUIRE_APPROVAL: "write operation requires approval",
        }
        policy_ids = {
            PolicyAction.ALLOW: "tool.read_only",
            PolicyAction.DENY: "tool.denied",
            PolicyAction.REQUIRE_APPROVAL: "filesystem.write",
        }
        return PolicyDecision(decision=action, policy_id=policy_ids[action], reason=reasons[action])
