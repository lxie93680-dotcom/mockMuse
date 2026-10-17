"""Repeatable isolated lifecycle evaluation. --live explicitly uses real models."""
import argparse
import asyncio
import copy
import hashlib
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agents.tool_context import ToolContext
from dotenv import load_dotenv
from muse.agent import run_agent
from muse.filesystem_mcp import Filesystem
from muse.tool_providers import mcp_source_id
from muse.compression import CheckpointStore
from muse.models import get_provider
from muse.tool_access import ToolAccess
from muse.tool_providers import MCPProvider
from muse.tool_registry import ToolRegistry
from muse.tool_store import canonical_json
from .fixtures import EvalMCP

ROOT=Path(__file__).parent
CASES=[json.loads(line) for line in (ROOT/'cases.jsonl').read_text().splitlines() if line.strip()]


def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def require(condition,message):
    if not condition:raise AssertionError(message)


def snapshot(registry,server):
    return {'functions':sorted(server.functions),
            'tools':[{k:r[k] for k in ('tool_id','name','description','parameters','enabled','availability')}
                     for r in registry.store.list_tools()]}


def unpack(raw):
    value=json.loads(raw)
    if isinstance(value,dict) and value.get('type')=='text':
        try:return json.loads(value['text'])
        except (ValueError,TypeError):return value['text']
    return value


def file_observation(path,expected):
    content=path.read_text() if path.exists() else None
    return {'exists':content is not None,'contains_expected':content is not None and expected in content,
            'content_exact':content==expected,'content_repr':repr(content)[:200]}



class Conversation:
    def __init__(self,registry,server,root,provider,trace):
        self.registry=registry;self.server=server;self.root=root;self.provider=provider;self.trace=trace
        self.session='eval-'+uuid.uuid4().hex;self.history=[];self.sequence=0;self.extra_servers=[]

    async def ask(self,prompt):
        self.sequence+=1
        store=CheckpointStore(self.registry.store.path,self.session)
        store.tool_registry=self.registry;store.task_id='eval-task-'+uuid.uuid4().hex
        servers=([self.server] if self.server else [])+self.extra_servers
        with patch.dict(os.environ,{'MUSE_LOCAL_WORKSPACE':str(self.root)}):
            async with asyncio.timeout(120):
                answer,self.history=await run_agent(None,prompt,self.history,lambda *args:None,
                    provider=self.provider,mcp_servers=servers,compression_store=store,
                    browser_mcp=self.server)
        with sqlite3.connect(store.path) as db:
            events=db.execute('SELECT tool_name,event_type FROM tool_events WHERE task_id=? ORDER BY created_at',(store.task_id,)).fetchall()
            calls=[{'tool':name,'arguments':json.loads(detail).get('arguments')} for name,detail in
                   db.execute('SELECT tool_name,detail FROM tool_events WHERE task_id=? AND event_type="start" ORDER BY created_at',(store.task_id,))]
            originals=db.execute('SELECT tool_name,raw_content FROM tool_originals WHERE task_id=?',(store.task_id,)).fetchall()
            requests=db.execute('SELECT count(*) FROM model_requests WHERE task_id=?',(store.task_id,)).fetchone()[0]
        evidence={'operation':'dialogue','prompt':prompt,'answer':answer,'events':events,
                  'outputs':[{'tool':name,'value':unpack(raw)} for name,raw in originals],
                  'model_requests':requests,'calls':calls}
        self.trace.append(evidence)
        require(not any(kind=='error' for _,kind in events),'Dialogue contained a tool failure')
        return evidence


def output_values(evidence,name):
    return [row['value'] for row in evidence['outputs'] if row['tool']==name]


def searched(evidence):
    values=output_values(evidence,'tool_search')
    require(values,'Model did not actually invoke tool_search')
    return [tool for result in values for tool in result.get('tools',[])]


async def refreshed(registry,server):
    server.emit_catalog_event('tools_changed')
    await registry.wait_for_refresh()


def context():
    return ToolContext(context=None,tool_name='tool_call',tool_call_id='direct-eval',tool_arguments='{}')


async def fixture_case(case,root,provider,trace):
    server=EvalMCP(root)
    async with server.session():
        registry=ToolRegistry(root/'catalog.db')
        await registry.register(MCPProvider(server))
        original=copy.deepcopy(server.functions)
        initial=snapshot(registry,server)
        access=ToolAccess(registry)
        talk=Conversation(registry,server,root,provider,trace) if provider else None
        cleanup={}
        checks=[];observations={}
        def check(ok,label):require(ok,label);checks.append(label)
        async def describe_dialogue(prompt,marker):
            if talk:
                evidence=await talk.ask(prompt)
                descriptions=output_values(evidence,'tool_describe')
                check(any(marker in canonical_json(value) for value in descriptions),'agent_described_latest_definition')
        try:
            kind=case.get('scenario',case['id'])
            if kind=='delete_visibility':
                server.install_probe();await refreshed(registry,server)
                row=access.search('eval_temp_probe')[0];tid=row['tool_id'];old=access.describe(tid)
                check(bool(row),'visible_before_removal')
                if talk:
                    first=await talk.ask('仅查询工具目录：使用 tool_search 搜索精确关键词 eval_temp_probe，返回工具结果。不执行工具，不使用浏览器，不进行网页搜索。')
                    check(any(t['tool_id']==tid for t in searched(first)),'dialogue_visible_before_removal')
                server.functions.pop('eval_temp_probe');await refreshed(registry,server)
                check(not access.search('eval_temp_probe'),'hidden_after_removal')
                try:access.describe(tid)
                except PermissionError:checks.append('old_id_rejected')
                else:raise AssertionError('Removed ID still describable')
                count=len(server.effects)
                try:await access.call(context(),tid,{},old['fingerprint'])
                except PermissionError:checks.append('removed_id_call_rejected')
                else:raise AssertionError('Removed ID still callable')
                check(len(server.effects)==count,'removed_tool_no_effect')
                if talk:
                    later=await talk.ask('工具目录刚刚更新。请再次实际调用 tool_search，搜索精确关键词 eval_temp_probe。不执行工具、不使用浏览器，不能引用旧结果冒充当前结果。')
                    check(all(t['tool_id']!=tid for t in searched(later)),'dialogue_hidden_after_removal')
                # Reappearance with the same key must retain identity.
                server.install_probe();await refreshed(registry,server)
                check(access.search('eval_temp_probe')[0]['tool_id']==tid,'same_id_on_restore')
                server.functions.pop('eval_temp_probe');await refreshed(registry,server)

            elif kind=='disable_visibility':
                row=access.search('eval_lookup')[0];tid=row['tool_id'];fp=access.describe(tid)['fingerprint']
                registry.enabled_states();loaded=registry.enabled_cache.loaded_at
                registry.set_enabled(tid,False)
                check(not access.search('eval_lookup'),'disabled_hidden_direct')
                check(not registry.enabled_states()[tid] and registry.enabled_cache.loaded_at==loaded,'cache_write_through_without_ttl_slide')
                try:await access.call(context(),tid,{'query':'must-not-run'},fp)
                except PermissionError:checks.append('old_id_call_denied')
                else:raise AssertionError('Disabled tool executed')
                if talk:
                    evidence=await talk.ask('请实际搜索 eval_lookup 工具，报告目前能否找到。若不可用，不允许改用命令或其他工具模拟它。')
                    check(all(t['tool_id']!=tid for t in searched(evidence)),'disabled_hidden_dialogue')
                check(not server.effects,'disabled_produced_no_effect')
                check(any(r['tool_id']==tid for r in registry.store.list_tools()),'management_keeps_disabled_row')
                registry.set_enabled(tid,True)
                check(bool(access.search('eval_lookup')),'reenable_visible_direct')
                if talk:
                    evidence=await talk.ask('现在请重新实际搜索 eval_lookup，只搜索不要执行。')
                    check(any(t['tool_id']==tid for t in searched(evidence)),'reenable_visible_dialogue')

            elif kind=='natural_write':
                if not talk:return {'status':'invalid','checks':[],'reason':'requires --live for natural-language selection'}
                note=root/'note.txt'
                # Exercise the installed Filesystem tool with its real English schema.
                # Remove the synthetic writer so it cannot make this case artificially easy.
                server.functions.pop('eval_write_note');await refreshed(registry,server)
                fs=Filesystem()
                with patch.dict(os.environ,{'MUSE_FILESYSTEM_MCP':'1','MUSE_LOCAL_WORKSPACE':str(root)}):
                    async with fs.session(lambda *args:None):
                        require(fs.server is not None,'Installed Filesystem MCP required for live file case')
                        await registry.register(MCPProvider(fs.server))
                        talk.extra_servers=[fs.server]
                        try:
                            expected=case.get('text','下周一整理旅行计划')
                            prompt=case.get('prompt','请通过已连接的外部文件能力，把“{text}”保存到 {path}，不要使用终端或内置补丁，也不要仅回复一段文字。')
                            evidence=await talk.ask(prompt.format(text=expected,path=note))
                            observations['file_content']=file_observation(note,expected)
                            check(observations['file_content']['contains_expected'],'actual_fixture_file_contains_expected')
                            searched(evidence)
                            check(any(row['tool']=='mcp:filesystem__write_file' for row in evidence['outputs']),
                                  'installed_filesystem_writer_really_invoked')
                        finally:
                            talk.extra_servers=[]
                            class RetiredSource:
                                source_kind='mcp'
                                source_id=mcp_source_id(fs.server)
                                async def discover_tools(self):return []
                            await registry.register(RetiredSource())

            elif kind=='natural_browser':
                if not talk:return {'status':'invalid','checks':[],'reason':'requires --live for natural-language selection'}
                readonly=case.get('readonly',False)
                if readonly:server.fixture_browser.url='https://eval.invalid/page'
                evidence=await talk.ask(case.get('prompt','帮我打开 https://eval.invalid/page，看看这个评测页面的标题是什么。'))
                searched(evidence)
                if readonly:
                    check(any(e['action']=='read' for e in server.fixture_browser.effects) and not any(e['action']!='read' for e in server.fixture_browser.effects),'browser_readonly_effect')
                else:check(any(e['action']=='open' and e['url']=='https://eval.invalid/page' for e in server.fixture_browser.effects),'browser_open_effect')
                check(any((row['tool'].endswith('__browser_action') or row['tool'].endswith('__read_browser')) and '生命周期评测页面' in canonical_json(row['value']) for row in evidence['outputs']),'actual_browser_title_returned')

            elif kind=='warm_cache_update':
                tid=access.search('eval_lookup')[0]['tool_id'];before=access.describe(tid)
                registry.enabled_states();loaded=registry.enabled_cache.loaded_at
                await describe_dialogue('请搜索评测查询器 eval_lookup 并实际描述其参数，只描述不执行。','LOOKUP_VERSION_ONE')
                server.functions['eval_lookup'].description+=' HOT_CACHE_LATEST_2026'
                server.install_probe();await refreshed(registry,server)
                check(access.describe(tid)['description'].endswith('HOT_CACHE_LATEST_2026'),'description_updated_with_warm_cache')
                check(access.describe(tid)['fingerprint']!=before['fingerprint'],'fingerprint_changed')
                probe=access.search('eval_temp_probe')[0]['tool_id']
                check(probe in registry.enabled_cache.active_ids(),'new_registration_enters_warm_cache')
                check(registry.enabled_cache.loaded_at==loaded,'registration_does_not_slide_ttl')
                check(bool(access.search('HOT_CACHE_LATEST_2026')),'updated_index_searchable')
                await describe_dialogue('工具定义已更新，请重新实际描述刚才的评测查询器，不依据历史推断。','HOT_CACHE_LATEST_2026')

            elif kind=='schema_reload':
                tid=access.search('eval_lookup')[0]['tool_id'];old=access.describe(tid)
                if talk:await talk.ask('请搜索并调用外部评测查询器 eval_lookup，查询 initial，告诉我实际版本。')
                else:await access.call(context(),tid,{'query':'initial'},old['fingerprint'])
                check(any(e.get('version')==1 for e in server.effects),'v1_executed')
                server.install_lookup(2);await refreshed(registry,server)
                current=access.describe(tid)
                check(current['fingerprint']!=old['fingerprint'] and 'revision' in current['parameters']['required'],'new_required_parameter_loaded')
                count=len(server.effects)
                try:await access.call(context(),tid,{'query':'stale'},old['fingerprint'])
                except ValueError:checks.append('stale_fingerprint_rejected')
                else:raise AssertionError('Stale definition accepted')
                check(len(server.effects)==count,'stale_call_no_effect')
                if talk:
                    evidence=await talk.ask('刚才的查询工具已更新。请重新描述它，按当前必填参数查询 updated，返回实际新版结果。')
                    check(any('LOOKUP_VERSION_TWO' in canonical_json(v) for v in output_values(evidence,'tool_describe')),'agent_reloaded_new_schema')
                else:await access.call(context(),tid,{'query':'updated','revision':2},current['fingerprint'])
                check(any(e.get('version')==2 and e.get('revision')==2 for e in server.effects),'v2_executed_with_latest_arguments')
            else:
                from .extended import exercise
                await exercise(case,root,registry,server,access,trace,talk,check)
            return {'status':'pass','checks':checks,'observations':observations}
        finally:
            note=root/'note.txt'
            trace.append({'operation':'fixture_effects','effects':copy.deepcopy(server.effects),
                          'browser_effects':copy.deepcopy(server.fixture_browser.effects),
                          'file_sha256':hashlib.sha256(note.read_bytes()).hexdigest() if note.exists() else None})
            # Restore definitions, enablement and fixture files even after failures.
            try:
                server.functions=original
                await registry.register(MCPProvider(server),force=True)
                for row in initial['tools']:registry.set_enabled(row['tool_id'],row['enabled'])
                note=root/'note.txt'
                if note.exists():note.unlink()
                # New identities are retained as missing tombstones by design.
                current=snapshot(registry,server)
                active=[r for r in current['tools'] if r['availability']!='missing']
                cleanup={'restored':current['functions']==initial['functions'] and active==initial['tools'] and not note.exists(),
                         'new_test_rows_only_missing':all(r['availability']=='missing' for r in current['tools'] if r['tool_id'] not in {v['tool_id'] for v in initial['tools']})}
                trace.append({'operation':'cleanup','result':cleanup})
                require(all(cleanup.values()),'Fixture restoration did not match baseline')
            finally:await registry.close()


def api(port,path,body=None):
    client=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
    try:
        client.request('POST' if body is not None else 'GET','/api/'+path,
                       body=json.dumps(body) if body is not None else None,headers={'Content-Type':'application/json'})
        response=client.getresponse();value=json.loads(response.read())
        require(response.status==200,'Temporary service returned HTTP '+str(response.status))
        return value
    finally:client.close()


@contextmanager
def service(root,config,trace):
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    env={key:value for key,value in os.environ.items() if key in ('PATH','HOME','TMPDIR','LANG','LC_ALL','USER')}
    env.update(MUSE_DATA_DIR=str(root/'service-data'),MUSE_TOOL_SOURCES_CONFIG=str(config),
               MUSE_ACCESS_TOKEN='isolated-eval-only',MUSE_BROWSER_BACKEND='extension',
               MUSE_FILESYSTEM_MCP='0',PYTHON_DOTENV_DISABLED='1')
    with (root/'service.log').open('ab') as log:
        process=subprocess.Popen([sys.executable,'-m','uvicorn','muse.app:app','--host','127.0.0.1','--port',str(port),'--loop','asyncio'],
                                 cwd=ROOT.parents[1],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
        try:
            ready=False
            for _ in range(100):
                if process.poll() is not None:raise RuntimeError('Temporary service exited before ready')
                try:api(port,'tools');ready=True;break
                except (OSError,ValueError):time.sleep(.05)
            require(ready,'Temporary service startup timed out')
            trace.append({'operation':'process_start','pid':process.pid,'port':port})
            yield port
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            trace.append({'operation':'process_stop','pid':process.pid,'stopped':process.poll() is not None})


async def restart_case(root,provider,trace,case=None):
    variant=(case or {}).get('variant','update')
    payload={'tools':[{'key':'lookup','name':'eval_restart_lookup','description':'RESTART_VERSION_ONE',
                       'parameters':{'type':'object','properties':{'query':{'type':'string'}}}}]}
    manifest=root/'tools.json';manifest.write_text(json.dumps(payload))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            data=json.dumps(payload).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    config=root/'sources.json'
    config.write_text(json.dumps({'sources':[{'id':'plugin:restart-eval','kind':'plugin','path':'tools.json'},
        {'id':'http:restart-eval','kind':'http','url':f'http://127.0.0.1:{http.server_port}/tools'}]}))
    checks=[];cleanup=False
    try:
        # In a worker thread, so the parent event loop can continue responding to MCP.
        def process_cycles():
            with service(root,config,trace) as port:
                before={r['source_id']:r for r in api(port,'tools')['tools'] if r['name']=='eval_restart_lookup'}
                require(len(before)==2,'Plugin/HTTP initial definitions absent')
                old={s:api(port,'tools/'+r['tool_id']+'/detail') for s,r in before.items()}
                for row in before.values():api(port,'tools/'+row['tool_id']+'/enabled',{'enabled':False})
            # Change only after process exit: file watching cannot satisfy the restart oracle.
            if variant=='empty':
                payload['tools']=[]
            elif variant!='unchanged':
                payload['tools'][0]['description']='RESTART_VERSION_TWO'
                payload['tools'][0]['parameters']['required']=['query']
                if variant=='rename':payload['tools'][0]['name']='eval_restart_lookup_renamed'
                payload['tools'].append({'name':'eval_after_restart','description':'Restart new capability','parameters':{}})
            manifest.write_text(json.dumps(payload))
            with service(root,config,trace) as port:
                rows=api(port,'tools')['tools'];states=api(port,'tools/enabled')['states']
                for source,initial in before.items():
                    current=api(port,'tools/'+initial['tool_id']+'/detail')
                    require(not current['enabled'] and not states[current['tool_id']],'Restart lost disabled state')
                    if variant=='empty':
                        require(current['availability']=='missing','Empty snapshot did not remove old identity')
                    elif variant=='unchanged':
                        require(current['fingerprint']==old[source]['fingerprint'] and current['description']=='RESTART_VERSION_ONE',
                                'Unchanged restart altered definition')
                    else:
                        require(current['description']=='RESTART_VERSION_TWO' and 'query' in current['parameters']['required'],'Restart did not load latest definition')
                        require(current['fingerprint']!=old[source]['fingerprint'],'Restart did not update fingerprint')
                        require(any(r['source_id']==source and r['name']=='eval_after_restart' for r in rows),'New tool absent after restart')
                        if variant=='rename':require(current['name']=='eval_restart_lookup_renamed','Stable-key rename not loaded')
            return old
        await asyncio.to_thread(process_cycles)
        checks.extend(['real_process_restart','plugin_and_http_snapshot_'+variant,'stable_ids','disabled_persisted'])
        registry=ToolRegistry(root/'service-data'/'muse.db')
        try:
            access=ToolAccess(registry)
            marker='RESTART_VERSION_ONE' if variant=='unchanged' else 'RESTART_VERSION_TWO'
            if variant=='empty':
                require(not registry.store.search('eval_restart_lookup',enabled_only=False),'Missing restart tool retained in index')
            else:
                require(len(registry.store.search(marker,enabled_only=False))==2,'Restart index does not match both sources')
                if variant!='unchanged':
                    require(bool(access.search('eval_after_restart')),'Restart new tool not searchable')
                    require(not registry.store.search('RESTART_VERSION_ONE',enabled_only=False),'Stale restart index retained')
            if provider:
                talk=Conversation(registry,None,root,provider,trace)
                if variant=='empty':
                    evidence=await talk.ask('仅查询工具目录，用 tool_search 搜索精确关键词 eval_restart_lookup，不执行工具，不使用浏览器。')
                    require(not searched(evidence),'Dialogue found missing restarted tool')
                else:
                    tid=next(r['tool_id'] for r in registry.store.list_tools('plugin:restart-eval') if r['tool_key']=='lookup')
                    registry.set_enabled(tid,True)
                    evidence=await talk.ask('仅查询工具目录：搜索 eval_restart_lookup，实际描述插件来源 plugin:restart-eval 的工具，只描述不要执行，报告当前版本和必填参数。')
                    require(any(v.get('source_id')=='plugin:restart-eval' and marker in canonical_json(v)
                                and (variant=='unchanged' or 'query' in v.get('parameters',{}).get('required',[]))
                                for v in output_values(evidence,'tool_describe')),
                            'Dialogue did not describe restarted definition')
                checks.append('dialogue_verified_restarted_snapshot')
        finally:await registry.close()
        # Original sources are restored and a third fresh startup must confirm restoration.
        payload['tools']=[{'key':'lookup','name':'eval_restart_lookup','description':'RESTART_VERSION_ONE',
                           'parameters':{'type':'object','properties':{'query':{'type':'string'}}}}]
        manifest.write_text(json.dumps(payload))
        def restore_check():
            with service(root,config,trace) as port:
                rows=api(port,'tools')['tools']
                for row in rows:
                    if row['source_id'] in ('plugin:restart-eval','http:restart-eval'):
                        if row['name']=='eval_restart_lookup':
                            require(row['description']=='RESTART_VERSION_ONE','Baseline description not restored')
                            api(port,'tools/'+row['tool_id']+'/enabled',{'enabled':True})
                        elif row['name']=='eval_after_restart':
                            require(row['availability']=='missing','Added tool not removed')
                require(all(r['enabled'] for r in api(port,'tools')['tools'] if r['name']=='eval_restart_lookup'),'Baseline enable flags not restored')
        await asyncio.to_thread(restore_check)
        cleanup=True
        return {'status':'pass','checks':checks}
    finally:
        # This is an isolated fixture; teardown removes its whole namespace after errors too.
        http.shutdown();http.server_close();thread.join(timeout=5)
        trace.append({'operation':'cleanup','result':{'restored':cleanup,'fixture_namespace_removed_by_temporary_directory':True,
                                                     'http_server_stopped':not thread.is_alive()}})
        require(not thread.is_alive(),'HTTP fixture thread leaked')


async def evaluate(output,live=False,provider_name=None,selected=None,categories=None,repeats=1):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    provider=get_provider(provider_name) if live else None
    if provider:require(bool(provider.api_key),'Selected model is not configured')
    cases=[c for c in CASES if (selected is None or c['id'] in selected) and (categories is None or c['category'] in categories)]
    require(1<=repeats<=10,'repeats must be 1 to 10')
    schedule=[(case,repeat) for repeat in range(repeats) for case in cases]
    require(bool(cases),'No cases selected')
    run={'status':'running','mode':'live_model_and_direct' if live else 'direct_only',
         'provider':provider.public() if provider else None,'categories':len({c['category'] for c in cases}),'case_ids':[c['id'] for c in cases],
         'repeats':repeats,'scheduled_cases':len(schedule),
         'product_state_modified':False,'fixture_browser':'synthetic adapter over real in-memory MCP',
         'file_case':'installed Filesystem MCP with original schemas and temporary allowed directory',
         'catalog_sha256':hashlib.sha256((ROOT/'cases.jsonl').read_bytes()).hexdigest(),
         'source_sha256':{str(p.relative_to(ROOT.parents[1])):hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in [Path(__file__),ROOT/'fixtures.py',ROOT/'extended.py',ROOT.parents[1]/'muse/tool_access.py',
                                   ROOT.parents[1]/'muse/tool_registry.py',ROOT.parents[1]/'muse/tool_store.py',
                                   ROOT.parents[1]/'muse/tool_enable_cache.py',ROOT.parents[1]/'muse/agent.py',
                                   ROOT.parents[1]/'muse/tool_providers.py',ROOT.parents[1]/'muse/browser_mcp.py',
                                   ROOT.parents[1]/'uv.lock']}}
    dump(output/'run.json',run)
    rows=[]
    try:
        for case,repeat in schedule:
            started=time.monotonic();trace=[];removed=False;fixture_path=None
            row={'case_id':case['id'],'title':case['title'],'category':case['category'],
                 'backend':run['mode'],'trace':trace,'repeat':repeat}
            try:
                with tempfile.TemporaryDirectory(prefix='muse-tool-eval-') as directory:
                    root=Path(directory).resolve()
                    try:
                        if case.get('scenario',case['id'])=='restart_reload':result=await restart_case(root,provider,trace,case)
                        else:result=await fixture_case(case,root,provider,trace)
                        row.update(result)
                    finally:fixture_path=root
                removed=not fixture_path.exists()
                require(removed,'Temporary fixture directory leaked')
            except Exception as error:
                row['status']='fail' if isinstance(error,AssertionError) else 'error'
                row['failure' if isinstance(error,AssertionError) else 'error']={
                    'type':type(error).__name__,'message':str(error)[:400] if isinstance(error,AssertionError) else 'Evaluation execution failed; inspect bounded trace'}
            removed=fixture_path is not None and not fixture_path.exists()
            row['temporary_namespace_removed']=removed
            row['duration_ms']=round((time.monotonic()-started)*1000)
            row['tool_calls']=sum(sum(event=='start' for _,event in step.get('events',[])) for step in trace)
            row['model_requests']=sum(step.get('model_requests',0) for step in trace)
            row['dialogue_tested']=any(step.get('operation')=='dialogue' for step in trace)
            rows.append(row)
            with (output/'results.jsonl').open('a') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
            print(case['id'],row['status'],str(row['duration_ms'])+'ms',flush=True)
            dump(output/'summary.json',summary(rows,run))
    finally:
        run['status']='completed' if len(rows)==len(schedule) else 'interrupted'
        dump(output/'run.json',run);dump(output/'summary.json',summary(rows,run))
    return rows


def summary(rows,run):
    return {'mode':run['mode'],'total':len(rows),'counts':{s:sum(r['status']==s for r in rows) for s in ('pass','fail','error','invalid')},
            'model_requests':sum(r['model_requests'] for r in rows),'dialogue_cases':sum(r['dialogue_tested'] for r in rows),
            'all_temporary_namespaces_removed':all(r['temporary_namespace_removed'] for r in rows),
            'category_counts':{category:{'total':sum(r['category']==category for r in rows),
                'pass':sum(r['category']==category and r['status']=='pass' for r in rows),
                'not_run':sum(r['category']==category and r['status']=='invalid' for r in rows)}
                for category in sorted({r['category'] for r in rows},key=int)},
            'natural_language_quality':'measured' if run['mode']=='live_model_and_direct' else 'not_run',
            'file_content_exact':{'pass':sum(r.get('observations',{}).get('file_content',{}).get('content_exact') is True for r in rows),
                                  'fail':sum(r.get('observations',{}).get('file_content',{}).get('content_exact') is False for r in rows)},
            'browser_fixture':'synthetic, not a real Chrome end-to-end measurement','product_state_modified':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');parser.add_argument('--provider')
    parser.add_argument('--output',default=str(ROOT/'results'/time.strftime('%Y%m%d-%H%M%S')))
    parser.add_argument('--cases',nargs='+',choices=[c['id'] for c in CASES])
    parser.add_argument('--category',nargs='+',choices=sorted({c['category'] for c in CASES},key=int))
    parser.add_argument('--repeats',type=int,default=1)
    args=parser.parse_args();load_dotenv()
    rows=asyncio.run(evaluate(args.output,args.live,args.provider,args.cases,args.category,args.repeats))
    if any(r['status'] in ('fail','error') or args.live and r['status']=='invalid' for r in rows):raise SystemExit(1)


if __name__=='__main__':main()
