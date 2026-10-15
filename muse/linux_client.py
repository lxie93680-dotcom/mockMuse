"""Remote Linux tools. Never execute commands or access files on the Agent host."""
import json
import os
import asyncio
import urllib.request
import urllib.error
from urllib.parse import urlparse


async def linux_request(action, **params):
    endpoint = os.getenv('MUSE_LINUX_URL', '').rstrip('/')
    token = os.getenv('MUSE_LINUX_TOKEN', '')
    if not endpoint or not token:
        raise RuntimeError('Linux 工具未配置：请设置 MUSE_LINUX_URL 和 MUSE_LINUX_TOKEN；不会使用本地环境')
    parsed = urlparse(endpoint)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError('Linux 服务地址无效')
    if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise RuntimeError('远程 Linux 服务必须使用 HTTPS 或 SSH 转发到 localhost')
    def request():
        req = urllib.request.Request(endpoint + '/execute', data=json.dumps({'action': action, **params}).encode(),
                                     headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
        # Do not follow redirects carrying the worker credential.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=70) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'Linux 服务拒绝请求（HTTP {error.code}）') from None
        except (OSError, ValueError):
            raise RuntimeError('Linux 服务不可达或响应无效；不会回退本地执行') from None
        if result.get('system') != 'Linux':
            raise RuntimeError('执行端不是 Linux，拒绝结果')
        return result
    return await asyncio.to_thread(request)
