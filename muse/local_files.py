"""Local text-file operations, restricted to a dedicated Agent workspace."""
import os
from pathlib import Path

LIMIT = 256 * 1024
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / 'data' / 'workspace'

def workspace():
    base = Path(os.getenv('MUSE_LOCAL_WORKSPACE', str(DEFAULT_ROOT))).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base

def checked_path(value):
    base = workspace()
    relative = Path(value)
    if relative.is_absolute():
        allowed = [Path(p).expanduser().absolute() for p in os.getenv('MUSE_LOCAL_ALLOWED_FILES', '').split(os.pathsep) if p]
        if relative not in allowed:
            raise ValueError('该绝对路径未获授权')
        if any(p.is_symlink() for p in (relative, *relative.parents)):
            raise ValueError('不允许通过符号链接操作文件')
        return relative
    if '..' in relative.parts:
        raise ValueError('请使用本地工作目录内的相对路径')
    current = base
    for part in relative.parts:
        if part.startswith('.'):
            raise ValueError('不允许操作隐藏文件或凭据文件')
        current = current / part
        if current.is_symlink():
            raise ValueError('不允许通过符号链接操作文件')
    if not current.resolve().is_relative_to(base):
        raise ValueError('路径超出本地工作目录')
    return current

def operate(action, path='.', content='', destination='', overwrite=False, query=''):
    target = checked_path(path)
    result = {'environment': 'local', 'workspace': str(workspace())}
    if action == 'list':
        entries = sorted((p for p in target.iterdir() if not p.name.startswith('.')), key=lambda p:p.name)
        result.update(entries=[{'name':p.name,'type':'symlink' if p.is_symlink() else 'directory' if p.is_dir() else 'file'} for p in entries[:1000]],truncated=len(entries)>1000)
    elif action == 'read':
        with target.open('rb') as file:
            data = file.read(LIMIT+1)
        if len(data)>LIMIT:
            raise ValueError('只支持最多256 KiB的文本文件')
        result['content'] = data.decode('utf-8')
    elif action == 'write':
        if len(content.encode('utf-8'))>LIMIT:
            raise ValueError('文本超过256 KiB')
        with target.open('w' if overwrite else 'x', encoding='utf-8') as file:
            file.write(content)
        result['written'] = len(content.encode('utf-8'))
    elif action == 'edit':
        if not query or len(content.encode('utf-8'))>LIMIT:
            raise ValueError('需要非空query，替换文本最多256 KiB')
        old = operate('read',path)['content']
        if old.count(query)!=1:
            raise ValueError('待替换文本必须恰好出现一次')
        updated=old.replace(query,content,1)
        if len(updated.encode('utf-8'))>LIMIT: raise ValueError('修改后文件过大')
        target.write_text(updated,encoding='utf-8');result['edited']=True
    elif action == 'search':
        if not target.is_dir() or not query: raise ValueError('搜索需要目录和非空query')
        matches=[];visited=0;truncated=False
        for folder,dirs,files in os.walk(target,followlinks=False):
            dirs[:]=[d for d in dirs if not d.startswith('.') and not (Path(folder)/d).is_symlink()]
            for name in files:
                file=Path(folder)/name
                if name.startswith('.') or file.is_symlink(): continue
                visited+=1
                if visited>1000: truncated=True;break
                relative=str(file.relative_to(workspace()))
                if query in name: matches.append({'path':relative,'match':'filename'})
                try:
                    if file.stat().st_size<=LIMIT:
                        for number,line in enumerate(file.read_text(encoding='utf-8').splitlines(),1):
                            if query in line: matches.append({'path':relative,'line':number,'text':line[:300]})
                            if len(matches)>=100: break
                except (UnicodeError,OSError): pass
                if len(matches)>=100: truncated=True;break
            if truncated: break
        result.update(matches=matches,truncated=truncated)
    elif action == 'mkdir':
        target.mkdir(parents=True, exist_ok=True)
        result['created'] = path
    elif action == 'move':
        dest = checked_path(destination)
        if not destination or target == workspace() or dest.exists():
            raise ValueError('不能移动工作目录或覆盖已有目标')
        target.rename(dest)
        result['moved'] = destination
    else:
        raise ValueError('action仅支持list/read/write/mkdir/move/search/edit')
    return result
