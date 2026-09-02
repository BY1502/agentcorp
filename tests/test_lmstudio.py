import json
import pytest
from urllib.error import URLError, HTTPError
from app.models.lmstudio import LMStudioProvider, ProviderError
from app.domain.contracts import ModelRequest

class Response:
    def __init__(self,data): self.data=data
    def read(self): return json.dumps(self.data).encode()

def test_lmstudio_provider_parses_message(monkeypatch):
    monkeypatch.setattr('app.models.lmstudio.urlopen',lambda *args,**kwargs: Response({'output':[{'type':'message','content':'hello'}],'stats':{'input_tokens':1}}))
    r=LMStudioProvider('qwen/test').complete(ModelRequest(messages=[{'role':'user','content':'hi'}]))
    assert r.kind=='final' and r.output['content']=='hello'

def test_lmstudio_provider_parses_tool_call(monkeypatch):
    content='<tool_call>\n<function=read_file><parameter=path>app/auth.py</parameter></function>\n</tool_call>'
    monkeypatch.setattr('app.models.lmstudio.urlopen',lambda *args,**kwargs: Response({'output':[{'type':'message','content':content}]}))
    r=LMStudioProvider('qwen/test').complete(ModelRequest(messages=[]))
    assert r.kind=='tool' and r.tool_call.name=='read_file' and r.tool_call.arguments['path']=='app/auth.py'

@pytest.mark.parametrize('exc,category',[(URLError('x'),'connection_error'),(TimeoutError(),'timeout_error')])
def test_lmstudio_provider_network_errors(monkeypatch,exc,category):
    monkeypatch.setattr('app.models.lmstudio.urlopen',lambda *a,**k: (_ for _ in ()).throw(exc))
    with pytest.raises(ProviderError,match='LM Studio'): LMStudioProvider('x').complete(ModelRequest(messages=[]))

def test_lmstudio_provider_rejects_bad_json(monkeypatch):
    monkeypatch.setattr('app.models.lmstudio.urlopen',lambda *a,**k: Response(bad:=None))
    # use a minimal response object with invalid bytes
    class Bad:
        def read(self): return b'{bad'
    monkeypatch.setattr('app.models.lmstudio.urlopen',lambda *a,**k: Bad())
    with pytest.raises(ProviderError) as error: LMStudioProvider('x').complete(ModelRequest(messages=[]))
    assert error.value.category=='response_parse_error'
