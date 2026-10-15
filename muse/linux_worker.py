"""Linux-only worker; run inside the dedicated deployment container."""
import asyncio
import os
import platform
import secrets
import signal
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Literal

LIMIT = 256 * 1024
lock = asyncio.Lock()

@asynccontextmanager
async def lifespan(app):
    if platform.system() != 'Linux':
        raise RuntimeError('Linux worker refuses non-Linux hosts')
    if len(os.getenv('MUSE_LINUX_TOKEN', '')) < 32:
        raise RuntimeError('MUSE_LINUX_TOKEN must contain at least 32 characters')
    root().mkdir(parents=True, exist_ok=True)
    yield

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

def root():
    return Path(os.getenv('MUSE_WORKSPACE', '/workspace')).resolve()

def path_for(value):
    value_path = Path(value)
    if value_path.is_absolute():
        raise ValueError('使用工作目录内的相对路径')
    base = root()
    target = (base / value_path).resolve()
    if not target.is_relative_to(base):
        raise ValueError('路径超出工作目录')
    return target

class Operation(BaseModel):
    action: Literal['list', 'read', 'write', 'mkdir', 'move', 'terminal']
    path: str = '.'
    destination: str = ''
    content: str = Field(default='', max_length=LIMIT)
    command: str = Field(default='', max_length=16000)
    timeout: int = Field(default=30, ge=1, le=60)

async def execute_operation(body):
    target = path_for(body.path)
    if body.action == 'list':
        entries = sorted(target.iterdir(), key=lambda p: p.name)
        return {'entries': [{'name': p.name, 'type': 'symlink' if p.is_symlink() else 'directory' if p.is_dir() else 'file'} for p in entries[:1000]], 'truncated': len(entries) > 1000}
    if body.action == 'read':
        with target.open('rb') as file:
            data = file.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('文件过大，最多读取 256 KiB 文本')
        return {'content': data.decode('utf-8')}
    if body.action == 'write':
        if len(body.content.encode()) > LIMIT:
            raise ValueError('文本超过 256 KiB')
        target.write_text(body.content, encoding='utf-8')
        return {'written': len(body.content.encode())}
    if body.action == 'mkdir':
        target.mkdir(parents=True, exist_ok=True)
        return {'created': body.path}
    if body.action == 'move':
        destination = path_for(body.destination)
        if target == root() or destination.exists():
            raise ValueError('不能移动工作目录或覆盖已有目标')
        target.rename(destination)
        return {'moved': body.destination}
    if not body.command.strip():
        raise ValueError('命令不能为空')
    if not target.is_dir():
        raise ValueError('命令工作目录必须是目录')
    # No model/API tokens are inherited by shell children. Container is the boundary.
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp', 'LANG': 'C.UTF-8'}
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = await asyncio.create_subprocess_exec('/bin/bash', '-lc', body.command,
            cwd=target, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), body.timeout)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            # Kill descendants even if their parent already exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        stdout.seek(0); stderr.seek(0)
        out = stdout.read(LIMIT + 1); err = stderr.read(LIMIT + 1)
        return {'exit_code': process.returncode, 'timed_out': timed_out,
                'stdout': out[:LIMIT].decode(errors='replace'), 'stderr': err[:LIMIT].decode(errors='replace'),
                'truncated': len(out) > LIMIT or len(err) > LIMIT}

@app.post('/execute')
async def execute(body: Operation, request: Request):
    expected = os.getenv('MUSE_LINUX_TOKEN', '')
    if not expected or not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + expected):
        raise HTTPException(401, 'Unauthorized')
    if platform.system() != 'Linux':
        raise HTTPException(503, 'Linux required')
    async with lock:
        try:
            return {'system': 'Linux', **await execute_operation(body)}
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from None
