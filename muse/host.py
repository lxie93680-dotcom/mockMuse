"""Host capabilities and process configuration; no filesystem sandbox fallback."""
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path


def system_name():
    return platform.system()


def transactional_files_supported():
    return system_name() in ('Darwin', 'Linux') or (
        system_name()=='Windows' and windows_version() is not None and windows_version()['major']>=10)


def windows_version():
    if not hasattr(sys,'getwindowsversion'):return None
    version=sys.getwindowsversion()
    return {'major':version.major,'minor':version.minor,'build':version.build}


def local_commands_supported():
    return system_name() in ('Darwin','Linux')


def filesystem_mcp_supported():
    return local_commands_supported()


def capabilities():
    supported = transactional_files_supported()
    limitations=[]
    if not supported:limitations.append('当前系统版本未开放本地文件事务。')
    if system_name()=='Windows':
        limitations.append('文件通过暂存、冲突检查、备份和逐项写回提交，不是整个目录原子交换。仅支持本机固定 NTFS 卷；不自动合并。')
        limitations.append('Windows 命令沙箱尚未实现；Agent 终端和外部 Filesystem MCP 禁用。')
    return {'os': system_name(), 'os_version':windows_version() if system_name()=='Windows' else platform.release(),
            'publication_mode':'journaled_writeback' if system_name()=='Windows' and supported else 'directory_exchange' if supported else 'unavailable',
            'shell': 'PowerShell' if system_name() == 'Windows' else
            ('zsh' if system_name() == 'Darwin' else 'bash'),
            'transactional_files': supported, 'agent_local_commands': local_commands_supported(),
            'filesystem_mcp':filesystem_mcp_supported(),
            'agent_file_writes': supported,
            'limitations':limitations}


def child_environment(home=None):
    # Keep runtime essentials, never provider keys, shell profiles or NODE_OPTIONS.
    keys = {'PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL', 'USER', 'LOGNAME',
            'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'SYSTEMROOT', 'WINDIR',
            'COMSPEC', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PATHEXT',
            'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMDATA', 'HOMEDRIVE', 'HOMEPATH'}
    env = {k: v for k, v in os.environ.items() if k.upper() in keys}
    separator = ';' if system_name() == 'Windows' else ':'
    path = next((v for k, v in env.items() if k.upper() == 'PATH'), os.defpath)
    env = {k: v for k, v in env.items() if k.upper() != 'PATH'}
    env['PATH'] = str(Path(sys.executable).parent) + separator + path
    env.setdefault('LANG', 'en_US.UTF-8')
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    if home is not None:
        env['HOME'] = str(home)
        if system_name() == 'Windows':
            env['USERPROFILE'] = str(home)
    env.setdefault('TMPDIR', tempfile.gettempdir())
    return env


def node_executable():
    explicit = os.getenv('MUSE_NODE_EXECUTABLE')
    if explicit:
        path = Path(explicit).expanduser()
        return str(path.resolve()) if path.is_file() else None
    found = shutil.which('node')
    if found:
        return found
    if system_name() == 'Darwin':
        candidate = Path('/opt/homebrew/opt/node@24/bin/node')
        if candidate.is_file():
            return str(candidate)
    return None


def shell_command(command, gate=False):
    system = system_name()
    if system == 'Windows':
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            raise RuntimeError('未找到 PowerShell；请安装 PowerShell 或将其加入 PATH')
        prefix = "$u=New-Object System.Text.UTF8Encoding($false);[Console]::InputEncoding=$u;[Console]::OutputEncoding=$u;$OutputEncoding=$u;"
        if gate:
            prefix += '[void][Console]::ReadLine();'
        return [shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', prefix + command]
    shell = '/bin/zsh' if system == 'Darwin' else '/bin/bash'
    return [shell, '-f', '-c', command] if system == 'Darwin' else [shell, '--noprofile', '--norc', '-c', command]


def process_options():
    if system_name() == 'Windows':
        return {'creationflags': 0x00000200}  # CREATE_NEW_PROCESS_GROUP
    return {'start_new_session': True}


def browser_executable():
    explicit = os.getenv('MUSE_PLAYWRIGHT_EXECUTABLE')
    if explicit:
        return explicit if Path(explicit).is_file() else None
    system = system_name()
    cache = os.getenv('PLAYWRIGHT_BROWSERS_PATH')
    if cache == '0':
        cache = str(Path(__file__).resolve().parents[1] / 'vendor/playwright/node_modules/playwright-core/.local-browsers')
    root = Path(cache).expanduser() if cache else (
        Path(os.getenv('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'ms-playwright'
        if system == 'Windows' else Path.home() / ('Library/Caches/ms-playwright' if system == 'Darwin' else '.cache/ms-playwright'))
    patterns = ('chromium-*/chrome-win*/chrome.exe',) if system == 'Windows' else (
        ('chromium-*/*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
         'chromium-*/*/Chromium.app/Contents/MacOS/Chromium') if system == 'Darwin' else
        ('chromium-*/chrome-linux*/chrome',))
    matches = [p for pattern in patterns for p in root.glob(pattern) if p.is_file()]
    def revision(path):
        return next((int(p.split('-', 1)[1]) for p in path.parts if p.startswith('chromium-') and p.split('-', 1)[1].isdigit()), 0)
    # Dedicated mode needs a Chromium build supporting --load-extension; do not
    # silently select a regular Chrome installation that ignores that flag.
    return str(max(matches, key=revision)) if matches else None
