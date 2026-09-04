from fastapi import FastAPI
from .config import settings
from fastapi import HTTPException
from uuid import UUID
from .api.schemas import EventResponse, MissionCreate, MissionResponse, RunCreate, RunResponse
from .models.registry import DisabledModelError, UnknownModelError
from .services.mission import MissionService
from .services.run import RunService
from .services.store import store

app = FastAPI(title="AgentCorp", version="0.1.0")

@app.get("/health")
def health() -> dict[str, str]: return {"status": "ok"}

missions=MissionService(); runs=RunService()
@app.post('/missions', response_model=MissionResponse)
def create_mission(request: MissionCreate):
    m=missions.create(request.title,request.fixture); return MissionResponse(id=m.id,title=m.title,version=m.version,fixture=m.fixture)
@app.get('/missions/{mission_id}', response_model=MissionResponse)
def get_mission(mission_id: UUID):
    m=missions.get(mission_id)
    if not m: raise HTTPException(404,'mission not found')
    return MissionResponse(id=m.id,title=m.title,version=m.version,fixture=m.fixture)
def run_response(r):
    return RunResponse(run_id=r.mission_run_id,mission_id=r.mission_id,status=r.status,retry_count=r.retry_count,changed_files=r.changed_files,tool_call_count=r.tool_call_count,event_count=r.event_count,workspace_ref=r.workspace_reference,model_snapshot=r.execution_manifest.model_snapshot)
@app.post('/missions/{mission_id}/runs', response_model=RunResponse)
def start_run(mission_id: UUID, request: RunCreate | None = None):
    m=missions.get(mission_id)
    if not m: raise HTTPException(404,'mission not found')
    try:
        result = runs.start(m, request.model_id if request else None)
    except UnknownModelError as error:
        raise HTTPException(404, str(error)) from error
    except DisabledModelError as error:
        raise HTTPException(409, str(error)) from error
    return run_response(result)
@app.get('/runs/{run_id}', response_model=RunResponse)
def get_run(run_id: UUID):
    r=runs.get(run_id)
    if not r: raise HTTPException(404,'run not found')
    return run_response(r)
@app.get('/runs/{run_id}/events', response_model=list[EventResponse])
def get_events(run_id: UUID):
    if not runs.get(run_id): raise HTTPException(404,'run not found')
    return [EventResponse(sequence=e.sequence,event_type=e.event_type,mission_run_id=e.mission_run_id,agent_run_id=e.agent_run_id,timestamp=e.timestamp,payload=e.payload) for e in sorted(runs.events_for(run_id),key=lambda x:x.sequence)]
