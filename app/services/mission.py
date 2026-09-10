from .store import MissionRecord, store


class MissionService:
    def __init__(self, repository=None):
        self.repository = repository or store

    def create(self, title, fixture):
        mission = MissionRecord(title, fixture)
        self.repository.save_mission(mission)
        return mission

    def get(self, mission_id):
        return self.repository.get_mission(mission_id)
