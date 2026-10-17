import importlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from muse.tool_providers import ToolDefinition


class ToolManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,{'MUSE_DATA_DIR':self.temp.name,'MUSE_BROWSER_BACKEND':'extension',
            'MUSE_FILESYSTEM_MCP':'0','MUSE_TOOL_SOURCES_CONFIG':'','MUSE_ACCESS_TOKEN':'test-management'})
        self.env.start();self.addCleanup(self.env.stop)
        import muse.app as module
        self.module=importlib.reload(module)
        self.client=TestClient(self.module.app);self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)

    def login(self):
        self.client.post('/api/login',json={'token':'test-management'})

    def test_page_auth_and_metadata_scope(self):
        self.assertEqual(self.client.get('/tools').status_code,200)
        self.assertIn('href="/tools"',self.client.get('/').text)
        self.assertEqual(self.client.get('/api/tools').status_code,401)
        self.assertEqual(self.client.get('/api/tools/absent/detail').status_code,401)
        self.login()
        data=self.client.get('/api/tools').json()
        self.assertEqual(len(data['tools']),6)
        self.assertTrue(all(row['source_kind']=='mcp' for row in data['tools']))
        self.assertTrue(all('parameters' not in row for row in data['tools']))
        self.assertNotIn('headers',str(data))
        self.assertEqual(self.client.get('/api/tools/absent/detail').status_code,404)
        detail=self.client.get('/api/tools/'+data['tools'][0]['tool_id']+'/detail').json()
        self.assertIn('parameters',detail);self.assertTrue(detail['fingerprint'])

    def test_management_shows_disabled_and_missing_while_agent_search_hides_them(self):
        self.login()
        provider=SimpleNamespace(source_id='plugin:fixture',source_kind='plugin',
            discover_tools=AsyncMock(return_value=[ToolDefinition('read','read','fixture',{'type':'object'})]))
        result=self.client.portal.call(self.module.tool_registry.register,provider)
        tid=result['tools'][0]['tool_id']
        loaded_at=self.module.tool_registry.enabled_cache.loaded_at
        self.client.get('/api/tools')
        loaded_at=self.module.tool_registry.enabled_cache.loaded_at
        response=self.client.post('/api/tools/'+tid+'/enabled',json={'enabled':False})
        self.assertEqual(response.status_code,200)
        row=next(row for row in self.client.get('/api/tools').json()['tools'] if row['tool_id']==tid)
        self.assertFalse(row['enabled'])
        self.assertEqual(self.module.tool_registry.enabled_cache.loaded_at,loaded_at)
        self.assertEqual(self.module.tool_registry.search('fixture'),[])
        self.assertFalse(self.client.get('/api/tools/'+tid+'/detail').json()['enabled'])
        provider.discover_tools.return_value=[]
        self.client.post('/api/tools/refresh',json={'source_id':'plugin:fixture'})
        row=next(row for row in self.client.get('/api/tools').json()['tools'] if row['tool_id']==tid)
        self.assertEqual(row['availability'],'missing')
        self.assertEqual(self.client.get('/api/tools/'+tid+'/detail').json()['availability'],'missing')

    def test_list_reads_enable_cache_not_stale_db_flag(self):
        self.login()
        row=self.client.get('/api/tools').json()['tools'][0];tid=row['tool_id']
        self.module.tool_registry.store.set_enabled(tid,False)
        current=next(row for row in self.client.get('/api/tools').json()['tools'] if row['tool_id']==tid)
        self.assertTrue(current['enabled'])
        self.assertTrue(self.client.get('/api/tools/'+tid+'/detail').json()['enabled'])
        self.client.post('/api/tools/'+tid+'/enabled',json={'enabled':False})
        current=next(row for row in self.client.get('/api/tools').json()['tools'] if row['tool_id']==tid)
        self.assertFalse(current['enabled'])

    def test_write_origin_and_unavailable_registry(self):
        self.login()
        tid=self.client.get('/api/tools').json()['tools'][0]['tool_id']
        self.assertEqual(self.client.post('/api/tools/'+tid+'/enabled',json={'enabled':False},
            headers={'origin':'https://other.example'}).status_code,403)
        with patch.object(self.module,'tool_registry',None):
            self.assertEqual(self.client.get('/api/tools').status_code,503)
            self.assertEqual(self.client.get('/api/tools/'+tid+'/detail').status_code,503)
