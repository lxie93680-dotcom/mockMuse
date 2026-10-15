"""Server-only provider configuration; public metadata never includes credentials."""
import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    extra_body: dict
    key_variable: str

    def public(self):
        return {'id': self.id, 'name': self.name, 'model': self.model,
                'ready': bool(self.api_key.strip())}


def get_provider(name=None):
    name = name or os.getenv('LLM_PROVIDER', 'deepseek')
    presets = {
        'kimi': ('Kimi', 'KIMI', 'kimi-k2.6', 'https://api.moonshot.cn/v1'),
        'deepseek': ('DeepSeek', 'DEEPSEEK', 'deepseek-flash', 'https://api.deepseek.com'),
        'custom': ('自定义兼容接口', 'CUSTOM', '', ''),
    }
    if name not in presets:
        raise ValueError('模型供应商必须是 kimi、deepseek 或 custom')
    label, prefix, default_model, default_url = presets[name]
    model = os.getenv(f'{prefix}_MODEL', default_model)
    # Kimi K2 supports this parameter; other model families use their own defaults.
    defaults = {'thinking': {'type': 'disabled'}} if name == 'deepseek' or (
        name == 'kimi' and model in ('kimi-k2.5', 'kimi-k2.6')) else {}
    extra = json.loads(os.getenv(f'{prefix}_EXTRA_BODY', json.dumps(defaults)))
    if not isinstance(extra, dict):
        raise ValueError(f'{prefix}_EXTRA_BODY 必须是 JSON 对象')
    key = os.getenv(f'{prefix}_API_KEY', '')
    if name == 'kimi' and not key:
        key = os.getenv('MOONSHOT_API_KEY', '')
    return Provider(name, label, model, os.getenv(f'{prefix}_BASE_URL', default_url),
                    key, extra, f'{prefix}_API_KEY')


def public_providers():
    return [get_provider(name).public() for name in ('kimi', 'deepseek', 'custom')]
