import importlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient

class Conversations(unittest.TestCase):
    def test_isolation_switch_and_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'MUSE_BROWSER_BACKEND':'extension','MUSE_DATA_DIR':directory,'MUSE_ACCESS_TOKEN':'test','MUSE_FILESYSTEM_MCP':'0'}):
            import muse.app as module
            module=importlib.reload(module)
            with TestClient(module.app) as c:
                self.assertEqual(c.post('/api/conversations').status_code,401)
                c.post('/api/login',json={'token':'test'})
                first=c.get('/api/state').json()['conversation_id']
                module.state['browser_workspace']=True
                module.state['messages']=[{'role':'user','text':'old task'}]
                module.history=[{'role':'user','content':'old context'}];module.save()
                new=c.post('/api/conversations').json()
                self.assertNotEqual(first,new['conversation_id']);self.assertEqual(new['messages'],[]);self.assertEqual(module.history,[])
                self.assertFalse(new['browser_workspace']);self.assertIsNone(new['frame']);self.assertEqual(new['url'],'')
                second=new['conversation_id']
                module.state['messages']=[{'role':'user','text':'new task'}]
                module.history=[{'role':'user','content':'new context'}];module.save()
                module.job=SimpleNamespace(done=lambda:False)
                self.assertEqual(c.post('/api/conversations').status_code,409)
                self.assertEqual(c.post('/api/conversations/'+first+'/activate').status_code,409)
                module.job=None
                switched=c.post('/api/conversations/'+first+'/activate').json()
                self.assertTrue(switched['browser_workspace']);self.assertIsNone(switched['frame']);self.assertEqual(switched['url'],'')
                self.assertEqual(switched['messages'][0]['text'],'old task');self.assertEqual(module.history[0]['content'],'old context')
                self.assertEqual(c.post('/api/conversations/missing/activate').status_code,404)
                c.post('/api/conversations/'+second+'/activate')
            module=importlib.reload(module)
            with TestClient(module.app) as c:
                c.post('/api/login',json={'token':'test'})
                state=c.get('/api/state').json()
                self.assertEqual(state['conversation_id'],second)
                self.assertEqual(state['messages'][0]['text'],'new task')
                self.assertEqual(module.history[0]['content'],'new context')
                self.assertEqual(len(state['conversations']),2)
