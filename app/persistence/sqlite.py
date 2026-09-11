import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID

from app.domain.models import CheckpointState, MissionRecord, MissionRunResult, TraceEvent, WorkspaceSnapshot
from app.domain.policy import PendingApproval
from app.tracing.recorder import sanitize


_SKIP_KEYS = {
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
    "raw_request",
    "raw_response",
    "provider_request",
    "provider_response",
    "chain_of_thought",
}


def _safe(value: Any) -> Any:
    """Serialize explicit JSON while dropping fields forbidden from storage."""
    if isinstance(value, dict):
        return {
            key: _safe(item)
            for key, item in value.items()
            if str(key).lower() not in _SKIP_KEYS
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return sanitize(value)
    return value


def _json(model: Any) -> str:
    return json.dumps(_safe(model.model_dump(mode="json")), separators=(",", ":"), sort_keys=True)


class SQLiteStore:
    """Persistent adapter; SQLite details stay behind this application boundary."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"unsupported AgentCorp storage schema version: {version}")
            if version == 0:
                self._connection.execute("PRAGMA user_version = 1")
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS missions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    version TEXT NOT NULL,
                    fixture TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    mission_run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    UNIQUE (mission_run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS workspace_snapshots (
                    id TEXT PRIMARY KEY,
                    snapshot_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    mission_run_id TEXT NOT NULL,
                    state_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    approval_json TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def save_mission(self, mission: MissionRecord) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO missions(id, title, version, fixture) VALUES (?, ?, ?, ?)",
                (str(mission.id), mission.title, mission.version, mission.fixture),
            )

    def get_mission(self, mission_id: UUID) -> MissionRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, title, version, fixture FROM missions WHERE id = ?", (str(mission_id),)
            ).fetchone()
        if row is None:
            return None
        return MissionRecord(row["title"], row["fixture"], id=UUID(row["id"]), version=row["version"])

    def save_workspace_snapshot(self, snapshot: WorkspaceSnapshot) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO workspace_snapshots(id, snapshot_json) VALUES (?, ?)",
                (str(snapshot.id), _json(snapshot)),
            )

    def get_workspace_snapshot(self, snapshot_id: UUID) -> WorkspaceSnapshot | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT snapshot_json FROM workspace_snapshots WHERE id = ?", (str(snapshot_id),)
            ).fetchone()
        return WorkspaceSnapshot.model_validate(json.loads(row["snapshot_json"])) if row else None

    def save_checkpoint(self, checkpoint_id: UUID, state: CheckpointState) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO checkpoints(checkpoint_id, mission_run_id, state_json) VALUES (?, ?, ?)",
                (str(checkpoint_id), str(state.mission_run_id), _json(state)),
            )

    def get_checkpoint(self, checkpoint_id: UUID) -> CheckpointState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT state_json FROM checkpoints WHERE checkpoint_id = ?", (str(checkpoint_id),)
            ).fetchone()
        return CheckpointState.model_validate(json.loads(row["state_json"])) if row else None

    def save_approval(self, approval: PendingApproval) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO approvals(approval_id, run_id, approval_json) VALUES (?, ?, ?)",
                (str(approval.approval_id), str(approval.run_id), _json(approval)),
            )

    def get_approval(self, approval_id: UUID) -> PendingApproval | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT approval_json FROM approvals WHERE approval_id = ?", (str(approval_id),)
            ).fetchone()
        return PendingApproval.model_validate(json.loads(row["approval_json"])) if row else None

    def list_approvals(self, run_id: UUID) -> list[PendingApproval]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT approval_json FROM approvals WHERE run_id = ? ORDER BY approval_id", (str(run_id),)
            ).fetchall()
        return [PendingApproval.model_validate(json.loads(row["approval_json"])) for row in rows]

    def append_events(self, events: list[TraceEvent]) -> None:
        with self._lock, self._connection:
            for event in events:
                self._connection.execute(
                    "INSERT INTO events(id, mission_run_id, sequence, event_json) VALUES (?, ?, ?, ?)",
                    (str(event.id), str(event.mission_run_id), event.sequence, _json(event)),
                )

    def list_events(self, run_id: UUID) -> list[TraceEvent]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_json FROM events WHERE mission_run_id = ? ORDER BY sequence ASC",
                (str(run_id),),
            ).fetchall()
        return [TraceEvent.model_validate(json.loads(row["event_json"])) for row in rows]

    def save_run(self, result: MissionRunResult) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO runs(run_id, mission_id, result_json) VALUES (?, ?, ?)",
                (str(result.mission_run_id), str(result.mission_id), _json(result)),
            )

    def finalize_run(self, result: MissionRunResult, events: list[TraceEvent]) -> None:
        """Commit the completed run and its append-only events together."""
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO runs(run_id, mission_id, result_json) VALUES (?, ?, ?)",
                (str(result.mission_run_id), str(result.mission_id), _json(result)),
            )
            for event in events:
                self._connection.execute(
                    "INSERT INTO events(id, mission_run_id, sequence, event_json) VALUES (?, ?, ?, ?)",
                    (str(event.id), str(event.mission_run_id), event.sequence, _json(event)),
                )

    def get_run(self, run_id: UUID) -> MissionRunResult | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT result_json FROM runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        return MissionRunResult.model_validate(json.loads(row["result_json"])) if row else None
