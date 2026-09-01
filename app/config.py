from pathlib import Path
from pydantic import BaseModel, Field
import os

class Settings(BaseModel):
    skills_dir: Path = Field(default=Path("skills"))
    workspaces_dir: Path = Field(default=Path("workspaces"))
    lmstudio_base_url: str = "http://127.0.0.1:1234"
    lmstudio_model: str = "qwen/qwen3.8-27b"
    lmstudio_timeout: float = 120

settings = Settings(lmstudio_base_url=os.getenv("AGENTCORP_LMSTUDIO_BASE_URL", "http://127.0.0.1:1234"), lmstudio_model=os.getenv("AGENTCORP_LMSTUDIO_MODEL", "qwen/qwen3.8-27b"))
