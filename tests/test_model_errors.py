import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from muse.model_errors import model_failure
from muse.loop_guard import GuardedModel,LoopGuard,LoopStopped

class ProviderError(Exception):
    def __init__(self,status,body=''):
        super().__init__(body);self.status_code=status

class ModelErrors(unittest.IsolatedAsyncioTestCase):
    async def test_balance_failure_stops_once_and_never_exposes_secret(self):
        error=ProviderError(402,'private-key')
        model=SimpleNamespace(get_response=AsyncMock(side_effect=error))
        guard=LoopGuard(lambda *a:None)
        with self.assertRaisesRegex(LoopStopped,'模型无法使用：账户余额') as caught:await GuardedModel(model,guard).get_response()
        self.assertNotIn('private-key',str(caught.exception))
        self.assertEqual(model.get_response.await_count,1)
        fresh=LoopGuard(lambda *a:None)
        self.assertEqual(fresh.failures,{'tool':0,'model':0})
        self.assertIsNone(fresh.stopped)

    async def test_transient_failure_keeps_retry_budget_with_clear_reason(self):
        model=SimpleNamespace(get_response=AsyncMock(side_effect=ProviderError(429)))
        guard=LoopGuard(lambda *a:None)
        with self.assertRaisesRegex(LoopStopped,'限流'):await GuardedModel(model,guard).get_response()
        self.assertEqual(model.get_response.await_count,3)

    def test_auth_and_bad_request_are_not_balance_errors(self):
        self.assertIn('API key',model_failure(ProviderError(401))[0])
        self.assertIn('请求参数',model_failure(ProviderError(400))[0])
