import hashlib
from pathlib import Path
from app.domain.models import SkillProfile, SkillVersion

class FilesystemSkillLoader:
    def __init__(self, root: Path): self.root = root
    def load(self, name: str, version: str | None = None) -> SkillVersion:
        path = (self.root / name).resolve()
        if self.root.resolve() not in path.parents: raise ValueError("skill path escapes root")
        content = path.read_text()
        return SkillVersion(name=name, version=version or "1.0.0", content=content, checksum=hashlib.sha256(content.encode()).hexdigest())
    def snapshot(self, names: list[str]) -> tuple[SkillVersion, ...]: return tuple(self.load(n) for n in names)

class DeterministicPromptCompiler:
    def __init__(self, loader: FilesystemSkillLoader): self.loader = loader
    def compile(self, context: dict, profile: SkillProfile):
        skills = self.loader.snapshot(list(profile.skills))
        instructions = "\n\n".join(s.content for s in skills)
        if context.get("expected_output"): instructions += "\n\nReturn only valid JSON matching the required schema: " + context["expected_output"]
        return {"messages": [{"role": "system", "content": instructions}, {"role": "user", "content": str(context)}], "skill_checksums": tuple(s.checksum for s in skills)}
