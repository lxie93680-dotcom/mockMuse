"""Explicitly authorized local shell execution; cwd is not a sandbox."""
import asyncio
import os
import platform
import signal
from .local_files import checked_path

LIMIT = 65536

async def run(command, cwd='.', timeout=30):
    if not command.strip() or len(command)>16000:
        raise ValueError('命令不能为空且最多16000字符')
    if not 1 <= timeout <= 60:
        raise ValueError('超时必须为1到60秒')
    directory = checked_path(cwd)
    if not directory.is_dir():
        raise ValueError('工作目录必须是目录')
    shell = '/bin/zsh' if platform.system() == 'Darwin' else '/bin/bash'
    # Do not inherit API keys or load the user's shell startup files.
    env = {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
           'HOME': str(directory), 'LANG': 'en_US.UTF-8'}
    args = [shell, '-f', '-c', command] if shell.endswith('zsh') else [shell,'--noprofile','--norc','-c',command]
    process = await asyncio.create_subprocess_exec(*args, cwd=directory, env=env,
                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    async def drain(stream):
        output = bytearray(); truncated=False
        while data := await stream.read(8192):
            room = LIMIT-len(output)
            output.extend(data[:room]); truncated |= len(data)>room
        return output.decode('utf-8',errors='replace'), truncated
    readers = [asyncio.create_task(drain(process.stdout)), asyncio.create_task(drain(process.stderr))]
    timed_out=False
    try:
        await asyncio.wait_for(process.wait(),timeout)
    except asyncio.TimeoutError:
        timed_out=True
    finally:
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        await process.wait()
        try:
            outputs=await asyncio.wait_for(asyncio.gather(*readers),2)
        except asyncio.TimeoutError:
            outputs=[('输出流未关闭',True),('输出流未关闭',True)]
    return {'environment':'local','system':platform.system(),'shell':shell,'cwd':str(directory),
            'exit_code':process.returncode,'timed_out':timed_out,
            'stdout':outputs[0][0],'stderr':outputs[1][0],'truncated':any(o[1] for o in outputs)}
