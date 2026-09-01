from app.domain.contracts import ModelRequest, ModelResponse, ToolCall

class FakeModelProvider:
    def __init__(self, responses: list[dict]): self.responses = iter(responses)
    def complete(self, request: ModelRequest) -> ModelResponse:
        item = next(self.responses)
        if item.get("kind") == "tool": return ModelResponse(kind="tool", tool_call=ToolCall(name=item["name"], arguments=item.get("arguments", {})))
        return ModelResponse(kind="final", output=item.get("output", item))
