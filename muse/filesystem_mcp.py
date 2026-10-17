"""Filesystem MCP process, owned by the task snapshot lifecycle."""
import os
import inspect
from pathlib import Path
from contextlib import asynccontextmanager
from agents.mcp import MCPServerStdio
from .workspace import workspace
from .staged_workspace import active_workspace
from .tool_failures import ToolBusinessError
from .loop_guard import active_guard
from .catalog_events import CatalogEvents
from .host import node_executable, child_environment, filesystem_mcp_supported

ROOT = Path(__file__).resolve().parents[1]

class LoggedServer(CatalogEvents, MCPServerStdio):
    def __init__(self, record, **kwargs):
        self.init_catalog_events()
        previous_handler=kwargs.pop('message_handler',None)
        async def message_handler(message):
            await self.handle_catalog_message(message)
            if previous_handler:
                result=previous_handler(message)
                if inspect.isawaitable(result):await result
        super().__init__(message_handler=message_handler,**kwargs)
        self.record = record

    async def connect(self):
        await super().connect()
        self.emit_catalog_event('connected')
    async def call_tool(self, tool_name, arguments, meta=None):
        self.record(self.name + ' · ' + tool_name)
        try:
            result = await super().call_tool(tool_name, arguments, meta)
        except Exception:
            self.record('MCP 调用失败', tool_name)
            raise
        self.record('MCP 工具返回错误' if getattr(result,'is_error',getattr(result,'isError',False)) else 'MCP 工具返回', tool_name)
        if getattr(result,'is_error',getattr(result,'isError',False)) and active_guard.get() is not None:
            raise ToolBusinessError('MCP tool returned an error')
        return result

class Filesystem:
    def __init__(self):
        self.server=None
        self.status={'enabled':False,'ready':False,'tools':[],'error':''}
    @asynccontextmanager
    async def session(self,record):
        enabled=os.getenv('MUSE_FILESYSTEM_MCP','0')=='1'
        self.status={'enabled':enabled,'ready':False,'tools':[],'error':''}
        if not enabled:
            yield;return
        if not filesystem_mcp_supported():
            self.status['error']='当前系统未提供外部进程文件写入沙箱，Filesystem MCP 不可用；可用受控内置文件工具'
            record('MCP 未启用',self.status['error']);yield;return
        entry=ROOT/'vendor/filesystem/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js'
        node=node_executable()
        if not entry.is_file() or not node or not Path(node).is_file():
            self.status['error']='Filesystem MCP 未安装或缺少Node.js'
            record('MCP 启动失败',self.status['error']);yield;return
        transaction=active_workspace.get()
        command=[node,str(entry),str(workspace())]
        if transaction is not None:command=transaction.sandbox_command(command)
        env=child_environment(transaction.scratch if transaction else ROOT/'data/mcp-home')
        if transaction is not None:
            env['TMPDIR']=env['TEMP']=env['TMP']=str(transaction.scratch)
        server=LoggedServer(record,name='Filesystem',params={'command':command[0],'args':command[1:],
             'cwd':str(workspace()),'env':env},
             cache_tools_list=True,client_session_timeout_seconds=60)
        server._muse_connection_available=True
        if transaction is not None:server._muse_file_workspace=transaction.id
        try: await server.connect()
        except Exception:
            self.status['error']='Filesystem MCP 启动失败，请查看服务日志'
            record('MCP 启动失败',self.status['error']);yield;return
        try:
            tools=await server.list_tools();self.server=server
            self.status.update(ready=True,tools=[{'name':t.name,'description':t.description} for t in tools])
            record('Filesystem MCP 已连接',f'tools/list 获取 {len(tools)} 个工具')
            yield
        finally:
            server._muse_connection_available=False
            self.server=None;self.status['ready']=False;await server.cleanup()
