from uuid import UUID, uuid4
from app.domain.models import ExecutionManifest

class MissionRecord:
    def __init__(self, title: str, fixture: str): self.id=uuid4(); self.title=title; self.fixture=fixture; self.version='1'

class AppStore:
    def __init__(self): self.missions={}; self.runs={}; self.events={}
store=AppStore()
