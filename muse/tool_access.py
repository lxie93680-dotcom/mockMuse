"""Task-scoped, permission-checked deferred access to external tools."""
import inspect
import json
from typing import Any

from agents import function_tool
from agents.tool_context import ToolContext
from agents.mcp.util import MCPUtil
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import NoSuchResource

from .human_handoff import HumanInterventionRequired
from .tool_providers import MCPProvider
from .tool_store import definition_fingerprint
from .tool_failures import ToolDefinitionError, ToolCallFailure, classify, causes
from .loop_guard import LoopStopped
from .host import filesystem_mcp_supported


def _no_remote(uri):
    raise NoSuchResource(ref=uri)


class ToolAccess:
    def __init__(self, registry, agent=None, allowed_ids=None, denied_names=()):
        self.registry=registry
        self.agent=agent
        self.allowed_ids=None if allowed_ids is None else frozenset(allowed_ids)
        self.denied_names=frozenset(denied_names)

    def permits(self, row):
        return ((filesystem_mcp_supported() or row['source_id']!='mcp:filesystem')
                and (self.allowed_ids is None or row['tool_id'] in self.allowed_ids)
                and row['name'] not in self.denied_names
                and row['tool_key'] not in self.denied_names)

    def resolve(self, tool_id):
        row=self.registry.store.get(tool_id)
        if row is None or not self.permits(row):
            raise PermissionError('工具不存在或不在本次授权范围')
        self.registry.require_enabled(row['source_id'],row['tool_key'])
        return row

    def compact(self, row):
        provider=self.registry.providers.get(row['source_id'])
        return {'tool_id':row['tool_id'],'name':row['name'],'source_id':row['source_id'],
                'server_id':row['server_id'],'source_kind':row['source_kind'],'description':row['description'][:800],
                'executable':isinstance(provider,MCPProvider) and getattr(provider.server,'_muse_connection_available',True)}

    def search(self, query, limit=10, server_id=None):
        # Scope AND enablement are applied before LIMIT, never after truncation.
        ids=self.registry.enabled_cache.active_ids()
        if self.allowed_ids is not None or self.denied_names or not filesystem_mcp_supported():
            ids=[tid for tid in ids if self.permits(self.registry.store.get(tid))]
        rows=self.registry.store.search(query,limit,enabled_only=False,tool_ids=ids,server_id=server_id)
        return [self.compact(row) for row in rows]

    def describe(self, tool_id):
        row=self.resolve(tool_id)
        return {**self.compact(row),'parameters':row['parameters'],'fingerprint':row['fingerprint']}

    def child(self, denied_names, allowed_ids=None):
        ids=self.allowed_ids
        if allowed_ids is not None:
            ids=frozenset(allowed_ids) if ids is None else ids.intersection(allowed_ids)
        return ToolAccess(self.registry,self.agent,ids,self.denied_names.union(denied_names))

    def catalog(self):
        return [self.compact(row) for row in self.registry.store.list_tools()
                if self.permits(row) and self.registry.enabled_cache.allows(row['source_id'],row['tool_key'])]

    async def call(self, ctx, tool_id, arguments, fingerprint):
        # Scope and enablement failures cannot initiate network discovery.
        row=self.resolve(tool_id)
        ctx._muse_call_stage='validation'
        try:
            return await self._call(ctx,tool_id,arguments,fingerprint)
        except Exception as error:
            if any(isinstance(cause,LoopStopped) for cause in causes(error)):raise
            category=classify(error)
            stage=ctx._muse_call_stage
            discovery='skipped'
            if category in ('connection','timeout','definition'):
                discovery=await self.registry.rediscover_on_failure(row['source_id'])
            if category in ('connection','timeout','definition') or stage=='execute':
                raise ToolCallFailure(category,row,stage,discovery) from None
            raise

    async def _call(self, ctx, tool_id, arguments, fingerprint):
        row=self.resolve(tool_id)
        if fingerprint!=row['fingerprint']:
            raise ValueError('工具定义已变更或未描述；先调用tool_describe获取当前fingerprint')
        if not isinstance(arguments,dict):raise ValueError('arguments必须是对象')
        schema=row['parameters']
        try:
            validator=validator_for(schema)
            validator.check_schema(schema)
            valid=validator(schema,registry=Registry(retrieve=_no_remote)).is_valid(arguments)
        except Exception:
            raise ValueError('工具参数定义无法安全验证；未执行工具') from None
        if not valid:raise ValueError('参数不符合tool_describe返回的JSON Schema；未执行工具')
        provider=self.registry.providers.get(row['source_id'])
        if not isinstance(provider,MCPProvider):
            raise RuntimeError('工具来源尚无已连接的执行器；当前仅支持目录发现')
        if provider.server.name=='Filesystem':
            from .staged_workspace import active_workspace
            transaction=active_workspace.get()
            readonly={'read_file','read_text_file','read_media_file','read_multiple_files',
                      'list_directory','list_directory_with_sizes','directory_tree','search_files',
                      'get_file_info','list_allowed_directories'}
            if transaction is None:
                if row['tool_key'] not in readonly:
                    raise PermissionError('文件写入必须在任务暂存区内执行')
            elif getattr(provider.server,'_muse_file_workspace',None)!=transaction.id:
                raise PermissionError('文件工具未绑定当前任务暂存区，拒绝访问正式目录')

        # Keep SDK filtering, annotations and approval policies on the live tool;
        # rebuilding it from the catalog alone would discard policy metadata.
        ctx._muse_call_stage='discovery'
        live=await provider.server.list_tools(run_context=ctx,agent=self.agent)
        definition=next((tool for tool in live if tool.name==row['name']),None)
        if definition is None:
            if getattr(provider.server,'tool_filter',None) is not None:
                raise PermissionError('工具被来源策略排除')
            raise ToolDefinitionError('工具已从来源移除')
        live_schema=getattr(definition,'input_schema',None)
        if live_schema is None:live_schema=definition.inputSchema
        if definition_fingerprint(definition.name,definition.description or '',live_schema)!=fingerprint:
            raise ToolDefinitionError('来源工具定义与目录不一致；请刷新来源后重新描述')
        target=MCPUtil.to_function_tool(definition,provider.server,False,
                                       agent=self.agent,failure_error_function=None)
        if target.tool_input_guardrails or target.tool_output_guardrails:
            raise RuntimeError('此工具需要专用guardrails执行路径；未执行工具')
        ctx._muse_call_stage='policy'
        approval=target.needs_approval
        if callable(approval):
            approval=approval(ctx,arguments,ctx.tool_call_id)
            if inspect.isawaitable(approval):approval=await approval
        if approval:raise HumanInterventionRequired('此工具调用需要人工批准')
        # Approval callbacks can yield; recheck immediately before the effect.
        latest=self.resolve(tool_id)
        if latest['fingerprint']!=fingerprint or self.registry.providers.get(row['source_id']) is not provider:
            raise ValueError('工具定义或连接已变更；请重新描述后调用')
        ctx._muse_result_tool_name=row['source_id']+'__'+row['name']
        from .browser_diagnostics import trace_context
        trace=trace_context.get()
        token=trace_context.set((trace[0],ctx._muse_result_tool_name,ctx.tool_call_id) if trace else None)
        try:
            ctx._muse_call_stage='execute'
            return await target.on_invoke_tool(ctx,json.dumps(arguments,ensure_ascii=False))
        finally:
            trace_context.reset(token)

    def tools(self):
        access=self
        @function_tool(strict_mode=False)
        async def tool_search(query: str, limit: int = 10, server_id: str | None = None) -> str:
            """按关键词搜索当前已启用的外部工具，返回精简候选，不执行工具。先搜索再描述。"""
            return json.dumps({'tools':access.search(query,limit,server_id)},ensure_ascii=False)

        @function_tool
        async def tool_describe(tool_id: str) -> str:
            """获取已启用工具的完整参数JSON Schema和指纹。调用前必须先描述，不猜参数。"""
            return json.dumps(access.describe(tool_id),ensure_ascii=False)

        @function_tool(strict_mode=False)
        async def tool_call(ctx: ToolContext, tool_id: str, arguments: dict[str,Any], fingerprint: str) -> Any:
            """调用已描述的外部工具。传入tool_describe的tool_id、fingerprint及符合其Schema的arguments。"""
            return await access.call(ctx,tool_id,arguments,fingerprint)

        tools=[tool_search,tool_describe,tool_call]
        for tool in tools:tool._muse_tool_access=access
        return tools
