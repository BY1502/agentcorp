class ToolRegistry:
    def __init__(self, tools=None): self.tools = tools or {}
    def register(self,name,tool): self.tools[name]=tool
    def names(self): return tuple(sorted(self.tools))
    def specifications(self, names=None):
        names = names or self.names()
        return [{"name": n, "description": "registered mission tool", "parameters": {"type": "object"}} for n in names if n in self.tools]
