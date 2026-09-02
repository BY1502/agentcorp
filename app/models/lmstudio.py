import json, re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from app.domain.contracts import ModelRequest, ModelResponse, ToolCall

class ProviderError(RuntimeError):
    def __init__(self, category, message, **metadata): self.category=category; self.metadata=metadata; super().__init__(message)

class LMStudioProvider:
    def __init__(self, model, base_url="http://127.0.0.1:1234", timeout=120): self.model=model; self.base_url=base_url.rstrip('/'); self.timeout=timeout
    def complete(self, request):
        system=next((m.get('content','') for m in request.messages if m.get('role')=='system'),''); user='\n\n'.join(m.get('content','') for m in request.messages if m.get('role')!='system')
        req=Request(self.base_url+'/api/v1/chat',data=json.dumps({'model':self.model,'system_prompt':system,'input':user}).encode(),headers={'Content-Type':'application/json'})
        try: raw=urlopen(req,timeout=self.timeout).read()
        except HTTPError as e: raise ProviderError('http_error',f'LM Studio returned HTTP {e.code}',status_code=e.code) from e
        except TimeoutError as e: raise ProviderError('timeout_error','LM Studio request timed out') from e
        except (URLError,OSError) as e: raise ProviderError('connection_error','Unable to connect to LM Studio') from e
        if not raw: raise ProviderError('response_parse_error','LM Studio returned an empty body')
        try: data=json.loads(raw)
        except (TypeError,ValueError) as e: raise ProviderError('response_parse_error','LM Studio returned invalid JSON') from e
        if isinstance(data,dict) and isinstance(data.get('choices'),list):
            if not data['choices']: raise ProviderError('invalid_response','Response choices are empty')
            msg=data['choices'][0].get('message')
            if not isinstance(msg,dict): raise ProviderError('invalid_response','Response message is missing')
            return self._message(msg,data.get('usage',{}))
        output=data.get('output') if isinstance(data,dict) else None
        if not isinstance(output,list) or not output: raise ProviderError('invalid_response','Response output is missing or empty')
        msg=next((x for x in output if isinstance(x,dict) and x.get('type')=='message'),None)
        if msg is None: raise ProviderError('invalid_response','Response message is missing')
        return self._message(msg,data.get('stats',{}))
    def _message(self,msg,usage):
        calls=msg.get('tool_calls')
        if calls is not None:
            if not isinstance(calls,list) or not calls: raise ProviderError('tool_call_parse_error','Tool calls are malformed')
            fn=calls[0].get('function') if isinstance(calls[0],dict) else None
            if not isinstance(fn,dict) or not fn.get('name') or 'arguments' not in fn: raise ProviderError('tool_call_parse_error','Tool call function is incomplete')
            try: args=json.loads(fn['arguments']) if isinstance(fn['arguments'],str) else fn['arguments']
            except (TypeError,ValueError) as e: raise ProviderError('tool_call_parse_error','Tool call arguments are invalid JSON') from e
            if not isinstance(args,dict): raise ProviderError('tool_call_parse_error','Tool call arguments must be an object')
            return ModelResponse(kind='tool',tool_call=ToolCall(name=fn['name'],arguments=args),usage=usage)
        if not msg.get('content'): raise ProviderError('invalid_response','Response has neither content nor tool calls')
        match=re.search(r'<tool_call>\s*<function=([^>]+)>(.*?)</function>',msg['content'],re.S)
        if match:
            pairs=re.findall(r'<parameter=([^>]+)>\s*(.*?)\s*</parameter>',match.group(2),re.S)
            return ModelResponse(kind='tool',tool_call=ToolCall(name=match.group(1),arguments=dict(pairs)),usage=usage)
        return ModelResponse(kind='final',output={'content':msg['content']},usage=usage)
