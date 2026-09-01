class ToolRegistry:
    def __init__(self, tools=None): self.tools = tools or {}
    def register(self,name,tool): self.tools[name]=tool
    def names(self): return tuple(sorted(self.tools))
