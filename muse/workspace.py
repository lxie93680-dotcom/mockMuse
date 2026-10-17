"""Shared workspace and document path authorization."""
import os
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / 'data' / 'workspace'

def target_workspace():
    base = Path(os.getenv('MUSE_LOCAL_WORKSPACE', str(DEFAULT_ROOT))).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base

def workspace():
    from .staged_workspace import active_workspace
    transaction=active_workspace.get()
    return transaction.files if transaction is not None else target_workspace()

def checked_path(value):
    base = workspace()
    relative = Path(value)
    if relative.is_absolute():
        from .staged_workspace import active_workspace
        if active_workspace.get() is not None:
            raise ValueError('任务只能访问暂存区中的文件路径；正式目标不可直接写入')
        allowed = [Path(p).expanduser().absolute() for p in os.getenv('MUSE_LOCAL_ALLOWED_FILES', '').split(os.pathsep) if p]
        if relative not in allowed:
            raise ValueError('该绝对路径未获授权')
        if any(p.is_symlink() for p in (relative, *relative.parents)):
            raise ValueError('不允许通过符号链接操作文件')
        return relative
    if '..' in relative.parts:
        raise ValueError('请使用本地工作目录内的相对路径')
    from .host import system_name
    if system_name()=='Windows' and relative.parts:
        from .windows_files import valid_relative
        valid_relative(relative.as_posix())
    current = base
    for part in relative.parts:
        if part.startswith('.'):
            raise ValueError('不允许操作隐藏文件或凭据文件')
        current = current / part
        if current.is_symlink():
            raise ValueError('不允许通过符号链接操作文件')
        if system_name()=='Windows' and current.exists() and getattr(current.lstat(),'st_file_attributes',0)&0x400:
            raise ValueError('不允许通过 junction 或其他重解析点操作文件')
    if not current.resolve().is_relative_to(base):
        raise ValueError('路径超出本地工作目录')
    return current
