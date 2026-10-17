"""Compatibility entrypoints backed by the source-neutral external tool registry."""
from .tool_providers import MCPProvider, mcp_source_id
from .tool_registry import ToolRegistry


def source_id(server):
    return mcp_source_id(server)


async def save_mcp_tools(store, server):
    registry=ToolRegistry(store)
    try:
        result=await registry.register(MCPProvider(server))
        return result['tools']
    finally:
        await registry.close()


async def register_catalog(path, servers, record, registry=None, providers=()):
    owned=registry is None
    registry=registry or ToolRegistry(path,record)
    try:
        return await registry.register_all([MCPProvider(server) for server in servers]+list(providers))
    finally:
        if owned:await registry.close()


async def bootstrap_catalog(path, browser_server, filesystem_server, record, registry=None):
    """Register external definitions only; builtins remain entirely in code."""
    return await register_catalog(path,[s for s in (filesystem_server,browser_server) if s],record,registry=registry)
