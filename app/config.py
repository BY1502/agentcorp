from pathlib import Path
from pydantic import BaseModel, Field

class Settings(BaseModel):
    skills_dir: Path = Field(default=Path("skills"))
    workspaces_dir: Path = Field(default=Path("workspaces"))

settings = Settings()
