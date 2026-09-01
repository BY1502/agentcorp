from app.domain.contracts import ModelRequest, ModelResponse

class FakeModelProvider:
    def __init__(self, responses: list[dict]): self.responses = iter(responses)
    def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(output=next(self.responses))
