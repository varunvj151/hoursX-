import asyncio
from typing import Any
import contextlib

from pydantic import BaseModel, create_model
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

from hoursx.tools.base import Tool, ToolContext, ToolOutcome, ToolSpec

class MCPTool:
    """Wrapper that adapts an MCP tool into an HoursX Tool."""
    
    def __init__(self, spec: ToolSpec, session: ClientSession, mcp_name: str):
        self.spec = spec
        self._session = session
        self._mcp_name = mcp_name

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolOutcome:
        try:
            # Drop unset optional fields
            args_dict = args.model_dump(exclude_unset=True)
            result = await self._session.call_tool(self._mcp_name, arguments=args_dict)
            summary = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
            return ToolOutcome.success(summary=summary or "Tool executed successfully.", data={"mcp_result": True})
        except Exception as e:
            return ToolOutcome.failure(summary=f"MCP tool error: {e}")

class MCPAdapter:
    """Manages the lifecycle of MCP servers and discovers tools."""
    
    def __init__(self, servers_config: dict[str, dict[str, Any]]):
        self.config = servers_config
        self._exits: list[Any] = []
        self._sessions: list[Any] = []

    async def start(self) -> list[Tool]:
        tools: list[Tool] = []
        for name, cfg in self.config.items():
            cmd = cfg.get("command")
            args = cfg.get("args", [])
            env = cfg.get("env", None)
            
            if not cmd:
                continue

            params = StdioServerParameters(command=cmd, args=args, env=env)
            
            ctx = stdio_client(params)
            read, write = await ctx.__aenter__()
            self._exits.append(ctx)
            
            session_ctx = ClientSession(read, write)
            session = await session_ctx.__aenter__()
            self._sessions.append(session_ctx)
            
            await session.initialize()
            
            tool_list = await session.list_tools()
            for t in tool_list.tools:
                fields = {}
                props = t.inputSchema.get("properties", {}) if t.inputSchema else {}
                required = t.inputSchema.get("required", []) if t.inputSchema else []
                
                for prop_name, prop_val in props.items():
                    typ = Any
                    val_type = prop_val.get("type")
                    if val_type == "string": typ = str
                    elif val_type == "integer": typ = int
                    elif val_type == "boolean": typ = bool
                    elif val_type == "number": typ = float
                    
                    if prop_name in required:
                        fields[prop_name] = (typ, ...)
                    else:
                        fields[prop_name] = (typ, None)
                
                params_model = create_model(f"MCP_{name}_{t.name}", **fields)
                
                spec = ToolSpec(
                    name=f"mcp.{name}.{t.name}",
                    description=t.description or f"MCP tool {t.name} from {name}",
                    params_model=params_model,
                )
                
                tools.append(MCPTool(spec, session, t.name))
        return tools

    async def stop(self):
        for s in reversed(self._sessions):
            with contextlib.suppress(Exception):
                await s.__aexit__(None, None, None)
        for e in reversed(self._exits):
            with contextlib.suppress(Exception):
                await e.__aexit__(None, None, None)
