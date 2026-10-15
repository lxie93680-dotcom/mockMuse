"""Project-local Desktop Commander MCP process, owned by Muse's lifespan."""
import json
import os
import shutil
from pathlib import Path
from contextlib import asynccontextmanager
from agents.mcp import MCPServerStdio
from .local_files import workspace
from .loop_guard import active_guard

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'vendor/desktop-commander/node_modules/@wonderwhy-er/desktop-commander/dist/index.js'
# Configuration and unrelated process control are not exposed to the model.
TOOLS = {'read_file','write_file','write_pdf','start_process','read_process_output',
         'interact_with_process','force_terminate','list_sessions'}

class LoggedServer(MCPServerStdio):
    def __init__(self, record, **kwargs):
        super().__init__(**kwargs)
        self.record = record
    async def call_tool(self, tool_name, arguments, meta=None):
        self.record(self.name + ' · ' + tool_name)
        try:
            result = await super().call_tool(tool_name, arguments, meta)
        except Exception:
            self.record('MCP 调用失败', tool_name)
            raise
        self.record('MCP 工具返回错误' if getattr(result,'is_error',getattr(result,'isError',False)) else 'MCP 工具返回', tool_name)
        if getattr(result,'is_error',getattr(result,'isError',False)) and active_guard.get() is not None:
            raise RuntimeError('MCP tool returned an error')
        return result

class Commander:
    def __init__(self):
        self.server = None
        self.status = {'enabled':False,'ready':False,'tools':[],'error':''}

    @asynccontextmanager
    async def session(self, record):
        enabled = os.getenv('MUSE_DESKTOP_COMMANDER','0') == '1'
        self.status = {'enabled':enabled,'ready':False,'tools':[],'error':''}
        if not enabled:
            yield
            return
        node = shutil.which('node') or '/opt/homebrew/opt/node@24/bin/node'
        if not ENTRY.is_file() or not Path(node).is_file():
            self.status['error']='缺少 Node.js 或 Desktop Commander；请按 README 安装'
            record('MCP 启动失败',self.status['error'])
            yield
            return
        home=Path(os.getenv('MUSE_MCP_HOME', str(ROOT/'data/mcp-home'))).resolve()
        config_dir=home/'.claude-server-commander';config_dir.mkdir(parents=True,exist_ok=True)
        config_file=config_dir/'config.json'
        config=json.loads(config_file.read_text()) if config_file.exists() else {}
        config.update({'allowedDirectories':[str(workspace())], 'defaultShell':'/bin/zsh',
                       'telemetryEnabled':False,'fileReadLineLimit':1000,'fileWriteLineLimit':1000,
                       'pendingWelcomeOnboarding':False,'welcomeOnboardingEligible':False})
        config.setdefault('blockedCommands',['sudo','su','rm','shutdown','reboot','mkfs','dd','diskutil'])
        config_file.write_text(json.dumps(config,ensure_ascii=False,indent=2));config_file.chmod(0o600)
        env={'HOME':str(home),'PATH':os.pathsep.join([str(Path(node).parent),'/opt/homebrew/bin','/usr/local/bin','/usr/bin','/bin','/usr/sbin','/sbin']),
             'SHELL':'/bin/zsh','LANG':'en_US.UTF-8','DO_NOT_TRACK':'1'}
        server=LoggedServer(record,name='Desktop Commander',params={'command':node,'args':[str(ENTRY),'--no-onboarding'],
                         'cwd':str(workspace()),'env':env},cache_tools_list=True,
                         client_session_timeout_seconds=90,tool_filter={'allowed_tool_names':sorted(TOOLS)})
        # Enter/exit the MCP cancel scope in the SAME lifespan task.
        try:
            await server.connect()
        except Exception:
            self.status['error']='Desktop Commander 启动失败，查看服务日志；不会静默切换工具'
            record('MCP 启动失败',self.status['error'])
            yield
            return
        try:
            tools=await server.list_tools()
            self.server=server
            self.status.update(ready=True,tools=[{'name':t.name,'description':t.description} for t in tools])
            record('Desktop Commander 已连接',f'通过 tools/list 获取 {len(tools)} 个工具')
            yield
        finally:
            self.server=None;self.status['ready']=False
            await server.cleanup()

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
        entry=ROOT/'vendor/filesystem/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js'
        node=shutil.which('node') or '/opt/homebrew/opt/node@24/bin/node'
        if not entry.is_file() or not Path(node).is_file():
            self.status['error']='Filesystem MCP 未安装或缺少Node.js'
            record('MCP 启动失败',self.status['error']);yield;return
        server=LoggedServer(record,name='Filesystem',params={'command':node,'args':[str(entry),str(workspace())],
             'cwd':str(workspace()),'env':{'HOME':str(ROOT/'data/mcp-home'),'PATH':str(Path(node).parent)+':/usr/bin:/bin'}},
             cache_tools_list=True,client_session_timeout_seconds=60)
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
            self.server=None;self.status['ready']=False;await server.cleanup()
