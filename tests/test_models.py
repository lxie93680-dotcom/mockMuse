import os
import unittest
from unittest.mock import patch
from muse.models import get_provider, public_providers


class ModelConfiguration(unittest.TestCase):
    def test_kimi_default_and_private_credentials(self):
        with patch.dict(os.environ, {'LLM_PROVIDER': 'kimi', 'KIMI_API_KEY': 'private-key'}, clear=True):
            provider = get_provider()
            self.assertEqual(provider.model, 'kimi-k2.6')
            self.assertEqual(provider.base_url, 'https://api.moonshot.cn/v1')
            self.assertEqual(provider.extra_body, {'thinking': {'type': 'disabled'}})
            self.assertNotIn('private-key', repr(provider))
            self.assertNotIn('private-key', str(public_providers()))

    def test_custom_and_model_specific_parameters(self):
        with patch.dict(os.environ, {'CUSTOM_MODEL': 'some-model', 'CUSTOM_BASE_URL':'https://example.com/v1',
                                     'CUSTOM_API_KEY':'private-key'}, clear=True):
            self.assertEqual(get_provider('custom').extra_body, {})
        with patch.dict(os.environ, {'KIMI_MODEL':'kimi-k2.7-code'}, clear=True):
            self.assertEqual(get_provider('kimi').extra_body, {})
        with patch.dict(os.environ, {'KIMI_MODEL':'kimi-k3'}, clear=True):
            self.assertEqual(get_provider('kimi').extra_body, {})
        with patch.dict(os.environ, {'CUSTOM_EXTRA_BODY':'{"reasoning_effort":"low"}'}, clear=True):
            self.assertEqual(get_provider('custom').extra_body, {'reasoning_effort':'low'})
        with patch.dict(os.environ, {'CUSTOM_EXTRA_BODY':'[]'}, clear=True):
            with self.assertRaises(ValueError):
                get_provider('custom')
