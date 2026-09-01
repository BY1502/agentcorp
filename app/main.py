from fastapi import FastAPI
from .config import settings

app = FastAPI(title="AgentCorp", version="0.1.0")

@app.get("/health")
def health() -> dict[str, str]: return {"status": "ok"}
