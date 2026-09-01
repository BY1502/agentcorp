from .store import store, MissionRecord
class MissionService:
    def create(self, title, fixture):
        m=MissionRecord(title,fixture); store.missions[m.id]=m; return m
    def get(self, mission_id): return store.missions.get(mission_id)
