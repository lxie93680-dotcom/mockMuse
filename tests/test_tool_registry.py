import asyncio
import json
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from muse.tool_providers import MCPProvider, ManifestProvider, HTTPToolsProvider, ToolDefinition, openapi_definitions
from muse.tool_registry import ToolRegistry
from muse.tool_store import ToolStore


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'muse.db'
        self.store=ToolStore(self.path)
        self.registry=ToolRegistry(self.store)

    async def test_mcp_discovery_identity_and_disabled_state(self):
        tool=SimpleNamespace(name='read',description='Read content',input_schema={'type':'object'})
        server=SimpleNamespace(name='Filesystem',list_tools=AsyncMock(return_value=[tool]))
        provider=MCPProvider(server)
        first=await self.registry.register(provider)
        tid=first['tools'][0]['tool_id'];self.store.set_enabled(tid,False)
        second=await self.registry.register(provider)
        self.assertEqual(second['changed'],0)
        self.assertEqual(second['tools'][0]['tool_id'],tid)
        tool.description='Updated lookup'
        changed=await self.registry.register(provider)
        self.assertEqual(changed['changed'],1)
        self.assertFalse(self.store.get(tid)['enabled'])
        self.assertEqual(self.store.search('Updated'),[])
        self.assertEqual(len(self.store.search('Updated',enabled_only=False)),1)

    async def test_manifest_stable_key_rename_and_batch_validation(self):
        path=Path(self.temp.name)/'tools.json'
        document={'tools':[{'key':'stable','name':'lookup','description':'查询工具','inputSchema':{'type':'object','properties':{'query':{'type':'string'}}}}]}
        path.write_text(json.dumps(document))
        provider=ManifestProvider('plugin:lookup',path)
        first=await self.registry.register(provider)
        document['tools'][0]['name']='search'
        path.write_text(json.dumps(document))
        changed=await self.registry.register(provider)
        self.assertEqual(first['tools'][0]['tool_id'],changed['tools'][0]['tool_id'])
        self.assertEqual(self.store.list_tools()[0]['name'],'search')
        self.assertEqual(len(self.store.search('search')),1)

    async def test_invalid_snapshot_preserves_all_prior_data(self):
        provider=SimpleNamespace(source_id='plugin:test',source_kind='plugin',discover_tools=AsyncMock(return_value=[ToolDefinition('key','lookup','old description',{})]))
        first=await self.registry.register(provider)
        tid=first['tools'][0]['tool_id']
        previous=self.registry.providers['plugin:test']
        invalid=SimpleNamespace(source_id='plugin:test',source_kind='plugin',discover_tools=AsyncMock(return_value=[ToolDefinition('key','lookup','new description',{}),ToolDefinition('other','other','invalid',[])]))
        with self.assertRaises(ValueError):await self.registry.register(invalid)
        self.assertIs(self.registry.providers['plugin:test'],previous)
        self.assertEqual(self.store.get(tid)['description'],'old description')
        self.assertEqual(self.store.get(tid)['fingerprint'],first['tools'][0]['fingerprint'])
        self.assertEqual(self.store.search('new description'),[])
        with closing(self.store.connect()) as db:
            for table in ('tool_catalog','tool_fingerprints','tool_catalog_fts'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],1)

    async def test_sources_are_independent_and_credentials_not_logged(self):
        logs=[];self.registry.record=lambda *args:logs.append(args)
        bad=SimpleNamespace(source_id='http:bad',source_kind='http',discover_tools=AsyncMock(side_effect=ConnectionError('Bearer private-token-value')))
        good=SimpleNamespace(source_id='plugin:good',source_kind='plugin',discover_tools=AsyncMock(return_value=[ToolDefinition('lookup','lookup','test',{})]))
        result=await self.registry.register_all([bad,good])
        self.assertEqual(result['sources'],{'plugin:good':1})
        self.assertEqual(result['errors'],['http:bad'])
        self.assertNotIn('private-token',str(logs))
        self.assertNotIn('private-token',str(self.store.list_tools()))

    async def test_duplicate_sources_keys_names_and_builtin_rejected(self):
        provider=SimpleNamespace(source_id='plugin:test',source_kind='plugin',discover_tools=AsyncMock(return_value=[]))
        with self.assertRaises(ValueError):await self.registry.register_all([provider,provider])
        for definitions in ([ToolDefinition('same','a','',{}),ToolDefinition('same','b','',{})],
                            [ToolDefinition('a','same','',{}),ToolDefinition('b','same','',{})]):
            provider.discover_tools.return_value=definitions
            with self.assertRaises(ValueError):await self.registry.register(provider)
        provider.source_id='muse:builtin';provider.source_kind='builtin'
        with self.assertRaises(ValueError):await self.registry.register(provider)
        self.assertEqual(self.store.list_tools(),[])

    async def test_same_source_discovery_is_serial_and_cancel_does_not_commit(self):
        active=0;maximum=0
        async def discover():
            nonlocal active,maximum
            active+=1;maximum=max(maximum,active)
            await asyncio.sleep(.01)
            active-=1
            return [ToolDefinition('a','a','',{})]
        provider=SimpleNamespace(source_id='plugin:test',source_kind='plugin',discover_tools=discover)
        await asyncio.gather(self.registry.register(provider),self.registry.register(provider))
        self.assertEqual(maximum,1)
        started=asyncio.Event()
        async def blocked():
            started.set();await asyncio.Event().wait()
        provider.discover_tools=blocked
        task=asyncio.create_task(self.registry.register(provider))
        await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(self.store.list_tools()),1)

    async def test_http_tools_and_openapi_metadata(self):
        payload={'tools':[{'name':'remote_lookup','description':'remote','parameters':{'type':'object'}}]}
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                seen.append(self.headers.get('Authorization'))
                if self.path=='/redirect':
                    self.send_response(302);self.send_header('Location','/schema');self.end_headers();return
                data=json.dumps(payload).encode()
                self.send_response(200);self.end_headers();self.wfile.write(data)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            provider=HTTPToolsProvider('http:test',f'http://127.0.0.1:{server.server_port}/schema',{'Authorization':'Bearer synthetic-test-secret'})
            result=await self.registry.register(provider)
            self.assertEqual(result['registered'],1)
            self.assertEqual(seen,['Bearer synthetic-test-secret'])
            self.assertNotIn('synthetic-test-secret',str(self.store.list_tools()))
            payload.clear();payload.update({'openapi':'3.1.0','paths':{'/items/{id}':{
                'parameters':[{'name':'id','in':'path','required':True,'schema':{'type':'string'}}],
                'post':{'operationId':'update_item','summary':'Update item','requestBody':{'required':True,'content':{'application/json':{'schema':{'$ref':'#/components/schemas/Body'}}}},'responses':{}}}},
                'components':{'schemas':{'Body':{'type':'object','properties':{'label':{'type':'string'}}}}}})
            result=await self.registry.register(provider)
            row=next(row for row in self.store.list_tools() if row['name']=='update_item')
            self.assertEqual(row['tool_key'],'POST:/items/{id}')
            self.assertEqual(row['parameters']['required'],['path','body'])
            self.assertEqual(row['parameters']['properties']['body']['properties']['label'],{'type':'string'})
            redirect=HTTPToolsProvider('http:redirect',f'http://127.0.0.1:{server.server_port}/redirect')
            with self.assertRaises(ValueError):await self.registry.register(redirect)
        finally:
            await asyncio.to_thread(server.shutdown);server.server_close();worker.join()

    async def test_openapi_rejects_unsupported_inputs_and_ignores_response_refs(self):
        valid={'openapi':'3.1.0','paths':{'/items':{'get':{'operationId':'list_items','responses':{'200':{'schema':{'$ref':'#/components/schemas/Recursive'}}}}}}}
        self.assertEqual(openapi_definitions(valid)[0].name,'list_items')
        for schema in ({'$ref':'https://example.com/schema'},{'$ref':'#/components/schemas/Recursive'}):
            document={'openapi':'3.1.0','paths':{'/items':{'post':{'requestBody':{'content':{'application/json':{'schema':schema}}}}}},'components':{'schemas':{'Recursive':{'$ref':'#/components/schemas/Recursive'}}}}
            with self.assertRaises(ValueError):openapi_definitions(document)
        with self.assertRaises(ValueError):HTTPToolsProvider('http:test','file:///etc/passwd')
        with self.assertRaises(ValueError):HTTPToolsProvider('http:test','https://user:password@example.com/schema')
