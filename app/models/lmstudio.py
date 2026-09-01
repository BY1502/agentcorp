import json
import re
from urllib.request import Request, urlopen
from app.domain.contracts import ModelRequest, ModelResponse

class LMStudioProvider:
    """Adapter for LM Studio's native /api/v1/chat endpoint."""
    def __init__(self, model: str, base_url: str = "http://127.0.0.1:1234", timeout: float = 120):
        self.model, self.base_url, self.timeout = model, base_url.rstrip('/'), timeout
    def complete(self, request: ModelRequest) -> ModelResponse:
        system = next((m["content"] for m in request.messages if m.get("role")=="system"), "")
        user = "\n\n".join(m.get("content", "") for m in request.messages if m.get("role") != "system")
        body=json.dumps({"model":self.model,"system_prompt":system,"input":user}).encode()
        response=urlopen(Request(self.base_url+"/api/v1/chat",data=body,headers={"Content-Type":"application/json"}),timeout=self.timeout)
        data=json.loads(response.read())
        message=next((x.get("content","") for x in data.get("output",[]) if x.get("type")=="message"), "")
        match=re.search(r'<tool_call>\s*<function=([^>]+)>(.*?)</function>\s*</tool_call>',message,re.S)
        if match:
            args={k:json.loads(v) if v[:1] in '[{' else v for k,v in re.findall(r'<parameter=([^>]+)>\s*(.*?)\s*</parameter>',match.group(2),re.S)}
            from app.domain.contracts import ToolCall
            return ModelResponse(kind="tool",tool_call=ToolCall(name=match.group(1),arguments=args),usage=data.get("stats",{}))
        return ModelResponse(kind="final",output={"content":message},usage=data.get("stats",{}))
