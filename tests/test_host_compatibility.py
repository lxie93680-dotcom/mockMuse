"""Portable contract tests plus real host subprocess/HTTP smoke tests.

Windows branches are mocked on macOS; Windows-only tests run in the CI matrix.
No claim is made that file publication/isolation is implemented on Windows.
"""
import asyncio
import importlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from muse import host
from muse.native_tools import NativeRuntime


class HostContracts(unittest.TestCase):
    def test_os_capabilities_and_shells(self):
        for system, shell in [('Darwin', '/bin/zsh'), ('Linux', '/bin/bash'), ('Windows', 'powershell.exe')]:
            with patch('muse.host.system_name', return_value=system), patch('muse.host.windows_version',return_value=None), patch('muse.host.shutil.which', return_value='powershell.exe'):
                info = host.capabilities()
                self.assertEqual(info['os'], system)
                self.assertEqual(info['agent_file_writes'], system != 'Windows')
                args = host.shell_command('echo hello')
                self.assertEqual(args[0], shell)
                self.assertIn('hello', args[-1])
                self.assertEqual('creationflags' in host.process_options(), system == 'Windows')
                if system == 'Windows':
                    self.assertIn('-NoProfile', args)
                    self.assertIn('UTF8Encoding', args[-1])
        with patch('muse.host.system_name', return_value='Windows'), patch('muse.host.shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'PowerShell'):
                host.shell_command('test')

    def test_child_environment_keeps_windows_runtime_but_no_secrets(self):
        env = {'Path': r'C:\Windows\System32', 'SystemRoot': r'C:\Windows',
               'TEMP': r'C:\Temp', 'LOCALAPPDATA': r'C:\User\Local', 'COMSPEC': 'cmd.exe',
               'DEEPSEEK_API_KEY': 'not-forwarded', 'NODE_OPTIONS': '--require unsafe.js',
               'MUSE_ACCESS_TOKEN': 'not-forwarded'}
        with patch.dict(os.environ, env, clear=True), patch('muse.host.system_name', return_value='Windows'):
            result = host.child_environment(Path('workspace'))
        self.assertTrue(result['PATH'].endswith(';'+env['Path']))
        self.assertEqual(result['SystemRoot'], env['SystemRoot'])
        self.assertEqual(result['USERPROFILE'], 'workspace')
        self.assertFalse({'DEEPSEEK_API_KEY', 'NODE_OPTIONS', 'MUSE_ACCESS_TOKEN'} & result.keys())

    def test_chromium_detection_on_three_platforms_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as data:
            root = Path(data)
            variants = {'Windows': 'chrome-win64/chrome.exe', 'Linux': 'chrome-linux64/chrome',
                        'Darwin': 'chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'}
            for system, suffix in variants.items():
                for revision in (9, 10):
                    path = root / ('chromium-'+str(revision)) / suffix
                    path.parent.mkdir(parents=True, exist_ok=True); path.touch()
                with patch.dict(os.environ, {'PLAYWRIGHT_BROWSERS_PATH': data, 'MUSE_PLAYWRIGHT_EXECUTABLE': ''}), patch('muse.host.system_name', return_value=system):
                    self.assertIn('chromium-10', host.browser_executable())
            with patch.dict(os.environ, {'MUSE_PLAYWRIGHT_EXECUTABLE': str(root/'missing.exe')}):
                self.assertIsNone(host.browser_executable())
            explicit = root/'custom browser.exe'; explicit.touch()
            with patch.dict(os.environ, {'MUSE_PLAYWRIGHT_EXECUTABLE': str(explicit)}):
                self.assertEqual(host.browser_executable(), str(explicit))

    def test_node_override_and_no_macos_fallback_on_windows(self):
        with tempfile.TemporaryDirectory() as data:
            executable = Path(data)/'node.exe'; executable.touch()
            with patch.dict(os.environ, {'MUSE_NODE_EXECUTABLE': str(executable)}):
                self.assertEqual(host.node_executable(), str(executable.resolve()))
            with patch.dict(os.environ, {'MUSE_NODE_EXECUTABLE': ''}), patch('muse.host.system_name', return_value='Windows'), patch('muse.host.shutil.which', return_value=None):
                self.assertIsNone(host.node_executable())

    def test_staging_refuses_windows_before_creating_files(self):
        from muse.staged_workspace import StagedWorkspace
        with tempfile.TemporaryDirectory() as data, patch('muse.host.system_name', return_value='Windows'):
            target = Path(data)/'should not exist'
            with self.assertRaisesRegex(RuntimeError, '尚未适配'):
                StagedWorkspace(target).begin()
            self.assertFalse(target.exists())

    def test_windows_agent_tools_and_instructions(self):
        from muse.agent import make_agent
        from muse.models import get_provider
        with patch('muse.host.system_name', return_value='Windows'), patch.dict(os.environ, {'KIMI_API_KEY':'host-test-only'}):
            agent, client = make_agent(None, lambda *args: None, get_provider('kimi'))
            try:
                names = {t.name for t in agent.tools}
                self.assertEqual(names, {'request_human', 'view_image'}|({'read_file','apply_patch'} if host.transactional_files_supported() else set()))
                self.assertIn('PowerShell', agent.instructions)
                self.assertIn('不可用', agent.instructions)
                self.assertNotIn('当前是macOS本地环境', agent.instructions)
            finally:
                asyncio.run(client.close())


class PortableRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='Muse space 中文 ')
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'MUSE_LOCAL_WORKSPACE': str(self.root)})
        self.env.start()
        self.runtime = NativeRuntime()  # Low-level adapter, NOT Agent authorization.

    async def asyncTearDown(self):
        await self.runtime.close(); self.env.stop(); self.temp.cleanup()

    async def test_actual_host_command_unicode_exit_and_stdin(self):
        windows = platform.system() == 'Windows'
        command = "[Console]::Write('你好 Muse');[Console]::Error.Write('error');exit 3" if windows else "printf '你好 Muse'; printf error >&2; exit 3"
        result = await self.runtime.exec_command(command, yield_time_ms=10000)
        self.assertEqual((result['stdout'], result['stderr'], result['exit_code']), ('你好 Muse', 'error', 3))
        command = "$v=[Console]::ReadLine();[Console]::Write('received:'+$v)" if windows else 'read -r v; printf "received:%s" "$v"'
        result = await self.runtime.exec_command(command, yield_time_ms=1)
        result = await self.runtime.write_stdin(result['session_id'], 'Muse\n', yield_time_ms=10000)
        self.assertEqual(result['stdout'], 'received:Muse')

    async def test_actual_host_timeout_and_task_cleanup(self):
        command = 'Start-Sleep -Seconds 30' if platform.system() == 'Windows' else 'sleep 30'
        result = await self.runtime.exec_command(command, yield_time_ms=1, timeout_seconds=2)
        session = self.runtime.sessions[result['session_id']]
        result = await self.runtime.write_stdin(result['session_id'], yield_time_ms=10000)
        self.assertTrue(result['timed_out']); self.assertIsNotNone(session.process.returncode)
        result = await self.runtime.exec_command(command, yield_time_ms=1)
        session = self.runtime.sessions[result['session_id']]
        await self.runtime.close()
        self.assertIsNotNone(session.process.returncode)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows Job Object test')
    async def test_windows_cleanup_kills_descendants(self):
        import ctypes as c
        from ctypes import wintypes as w
        command = "$p=Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList '-NoProfile','-Command','\"Start-Sleep -Seconds 30\"' -PassThru;[Console]::WriteLine($p.Id);Start-Sleep -Seconds 30"
        result = await self.runtime.exec_command(command, yield_time_ms=1000)
        output = result['stdout']
        for _ in range(10):
            if output.strip(): break
            result = await self.runtime.write_stdin(result['session_id'], yield_time_ms=1000)
            output += result['stdout']
        self.assertTrue(output.strip(), result)
        api = c.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; api.OpenProcess.restype = w.HANDLE
        api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]; api.WaitForSingleObject.restype = w.DWORD
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.OpenProcess(0x00100000, False, int(output.strip()))
        self.assertTrue(handle)
        try:
            await self.runtime.close()
            self.assertEqual(api.WaitForSingleObject(handle, 5000), 0)
        finally:
            api.CloseHandle(handle)

    async def test_windows_runner_skips_transaction_and_native_calls_fail_closed(self):
        from muse.agent import run_agent
        with patch('muse.host.system_name', return_value='Windows'),patch('muse.host.windows_version',return_value=None), patch('muse.agent._run_agent_staged', new_callable=AsyncMock, return_value=('browser result', [])) as runner, patch('muse.staged_workspace.StagedWorkspace.begin') as begin:
            answer = await run_agent(None, 'browse', [], lambda *args: None,
                                     mcp_servers=[SimpleNamespace(name='Filesystem'), SimpleNamespace(name='Remote-docs')])
            self.assertEqual(answer[0], 'browser result'); begin.assert_not_called()
            self.assertEqual([s.name for s in runner.call_args.args[6]], ['Remote-docs'])
            runtime = NativeRuntime(require_workspace=True)
            with self.assertRaisesRegex(PermissionError, '尚未适配'):
                await runtime.exec_command('echo forbidden')

    async def test_windows_filesystem_mcp_not_spawned_and_old_catalog_hidden(self):
        from muse.filesystem_mcp import Filesystem
        from muse.tool_access import ToolAccess
        from muse.tool_registry import ToolRegistry
        from muse.tool_providers import ToolDefinition
        registry = ToolRegistry(self.root/'catalog.db')
        provider = SimpleNamespace(source_id='mcp:filesystem', source_kind='mcp',
            discover_tools=AsyncMock(return_value=[ToolDefinition('read_file', 'read_file', 'read file', {'type':'object'})]))
        try:
            row = (await registry.register(provider))['tools'][0]
            with patch('muse.host.system_name', return_value='Windows'), patch.dict(os.environ, {'MUSE_FILESYSTEM_MCP':'1'}), patch('muse.filesystem_mcp.LoggedServer') as spawn:
                fs = Filesystem()
                async with fs.session(lambda *args: None):
                    self.assertFalse(fs.status['ready']); spawn.assert_not_called()
                access = ToolAccess(registry)
                self.assertEqual(access.search('read_file'), [])
                with self.assertRaises(PermissionError): access.describe(row['tool_id'])
        finally:
            await registry.close()


class PortableService(unittest.TestCase):
    def test_real_uvicorn_startup_and_cli_over_http(self):
        import socket
        import time
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        with tempfile.TemporaryDirectory(prefix='Muse server 中文 ') as data:
            env = {**os.environ, 'MUSE_DATA_DIR':data, 'MUSE_BROWSER_BACKEND':'extension',
                   'MUSE_FILESYSTEM_MCP':'0', 'MUSE_TOOL_SOURCES_CONFIG':'',
                   'MUSE_ACCESS_TOKEN':'host-test-only', 'PYTHONUTF8':'1'}
            with tempfile.TemporaryFile() as log:
                process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'muse.app:app',
                    '--host', '127.0.0.1', '--port', str(port), '--loop', 'asyncio'],
                    env=env, stdout=log, stderr=log)
                try:
                    from muse.cli import Client
                    client = Client('http://127.0.0.1:'+str(port), timeout=1)
                    deadline = time.monotonic()+20
                    while True:
                        try:
                            state = client.request('state'); break
                        except Exception:
                            if process.poll() is not None or time.monotonic() > deadline:
                                self.fail('Isolated uvicorn failed to become ready')
                            time.sleep(.1)
                    self.assertEqual(state['environment']['os'], platform.system())
                    result = subprocess.run([sys.executable, '-m', 'muse.cli', '--server',
                        client.base, 'status'], env=env, capture_output=True, text=True, encoding='utf-8', timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['environment']['os'], platform.system())
                finally:
                    if process.poll() is None: process.terminate()
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=10)

    def test_real_asgi_startup_state_and_cli_with_windows_capabilities(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'MUSE_DATA_DIR': data,
            'MUSE_BROWSER_BACKEND':'extension', 'MUSE_FILESYSTEM_MCP':'0', 'MUSE_TOOL_SOURCES_CONFIG':''}):
            import muse.app as app
            app = importlib.reload(app)
            with patch('muse.host.system_name', return_value='Windows'):
                with TestClient(app.app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 1234)) as client:
                    response = client.get('/api/state')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['environment']['agent_file_writes'],host.transactional_files_supported())
                    self.assertEqual(client.get('/').status_code, 200)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native smoke test')
    def test_windows_clean_import_and_powershell_script_parse(self):
        result = subprocess.run([sys.executable, '-c', 'import muse.app; import muse.cli; print("ok")'],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        script = Path(__file__).resolve().parents[1]/'scripts/muse-windows.ps1'
        command = "$errors=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$null,[ref]$errors);if($errors){$errors;exit 1}"
        # Pass the path as an environment value; do not interpolate shell code.
        command = command.replace('$args[0]', '$env:MUSE_TEST_SCRIPT')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
                                env={**os.environ,'MUSE_TEST_SCRIPT': str(script)}, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)


@unittest.skipUnless(os.getenv('MUSE_RUN_BROWSER_SMOKE') == '1', 'Opt-in real Chromium/MCP smoke test')
class RealBrowserCompatibility(unittest.IsolatedAsyncioTestCase):
    async def test_real_node_mcp_navigation_input_screenshot_and_trace(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from muse.playwright_browser import PlaywrightBrowser
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                data = b'<html><title>Muse fixture</title><label>Name<input aria-label="Name"></label><p>Host compatibility</p></html>'
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='Muse browser 中文 ') as data, patch.dict(os.environ, {
                'MUSE_DATA_DIR':data, 'MUSE_PORT':str(server.server_port),
                'MUSE_PLAYWRIGHT_CONNECTION':'dedicated', 'MUSE_PLAYWRIGHT_HEADLESS':'1',
                'MUSE_PLAYWRIGHT_CDP_ENDPOINT':'', 'MUSE_PLAYWRIGHT_EXECUTABLE':''}):
                browser = PlaywrightBrowser()
                async with browser.session(lambda *args: None):
                    self.assertTrue(browser.status['ready'], browser.status)
                    page = await browser.perform('open', url='http://127.0.0.1:'+str(server.server_port))
                    self.assertEqual(page['title'], 'Muse fixture')
                    textbox = next(e for e in page['elements'] if e['role'] == 'textbox')
                    updated = await browser.perform('fill', ref=textbox['ref'], text='Muse 你好')
                    self.assertIn('Muse 你好', updated['snapshot'])
                    result = await browser.tool('browser_take_screenshot', {'type':'jpeg','filename':'compatibility.jpg'})
                    self.assertTrue(result)
                    from PIL import Image
                    with Image.open(Path(data)/'playwright-output/compatibility.jpg') as picture:
                        self.assertGreater(picture.width, 0); self.assertGreater(picture.height, 0)
                    traces = list((Path(data)/'playwright-output').glob('mcp-trace-*.jsonl'))
                    self.assertTrue(any('instrumentation_ready' in p.read_text(encoding='utf-8') for p in traces))
        finally:
            await asyncio.to_thread(server.shutdown); server.server_close(); thread.join()
