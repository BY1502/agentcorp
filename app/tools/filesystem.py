import hashlib, subprocess, sys, time, os
from pathlib import Path
from .registry import ToolRegistry
from app.domain.contracts import ToolCall, ToolResult

class WorkspaceTools:
    def __init__(self, root: Path): self.root = root.resolve()
    def path(self, value: str) -> Path:
        p = Path(value)
        if p.is_absolute(): raise ValueError("absolute paths are not allowed")
        result = (self.root / p).resolve()
        try: result.relative_to(self.root)
        except ValueError: raise ValueError("path escapes workspace")
        return result
    def list_files(self, path=".") -> ToolResult:
        try: return ToolResult(success=True, output="\n".join(sorted(str(p.relative_to(self.root)) for p in self.path(path).rglob("*") if p.is_file())))
        except Exception as e: return ToolResult(success=False, error=str(e))
    def read_file(self, path) -> ToolResult:
        try: return ToolResult(success=True, output=self.path(path).read_text(encoding="utf-8"))
        except Exception as e: return ToolResult(success=False, error=str(e))
    def search_code(self, query, path=".") -> ToolResult:
        try:
            base=self.path(path); hits=[]
            for f in sorted(base.rglob("*")):
                if f.is_file():
                    try:
                        for n,line in enumerate(f.read_text(encoding="utf-8").splitlines(),1):
                            if query in line: hits.append(f"{f.relative_to(self.root)}:{n}:{line}")
                    except UnicodeDecodeError: pass
            return ToolResult(success=True, output="\n".join(hits[:100]))
        except Exception as e: return ToolResult(success=False,error=str(e))
    def edit_file(self, path, old_text, new_text, allow_multiple=False) -> ToolResult:
        try:
            f=self.path(path); before=f.read_text(encoding="utf-8"); count=before.count(old_text)
            if count == 0: return ToolResult(success=False,error="old_text not found")
            if count > 1 and not allow_multiple: return ToolResult(success=False,error="ambiguous edit")
            f.write_text(before.replace(old_text,new_text),encoding="utf-8")
            return ToolResult(success=True,output=f"edited {path}",metadata={"before":hashlib.sha256(before.encode()).hexdigest(),"after":hashlib.sha256(f.read_bytes()).hexdigest()})
        except Exception as e: return ToolResult(success=False,error=str(e))
    def run_test(self, path="tests", timeout=30) -> ToolResult:
        started=time.monotonic()
        try:
            env=os.environ.copy(); env["PYTHONPATH"]=str(self.root)
            p=subprocess.run([sys.executable,"-m","pytest",path,"-q","-c","/dev/null"],cwd=self.root,env=env,text=True,capture_output=True,timeout=timeout)
            return ToolResult(success=p.returncode==0,output=p.stdout,error=p.stderr,metadata={"exit_code":p.returncode,"duration_ms":round((time.monotonic()-started)*1000)})
        except Exception as e: return ToolResult(success=False,error=str(e),metadata={"duration_ms":round((time.monotonic()-started)*1000)})

    def execute(self, call: ToolCall) -> ToolResult:
        try: return getattr(self, call.name)(**call.arguments)
        except Exception as e: return ToolResult(success=False,error=str(e))
