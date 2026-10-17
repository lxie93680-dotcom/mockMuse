"""Host-owned task snapshots and a single durable, atomic directory publication."""
import contextvars
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit
from contextlib import contextmanager
from pathlib import Path
from .file_metadata import signature as metadata_signature
from .host import transactional_files_supported

active_workspace = contextvars.ContextVar('muse_staged_workspace', default=None)


def _fingerprint(root,version=2):
    digest=hashlib.sha256()
    def walk(path,relative):
        info=path.lstat()
        mode=stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            value=['link',relative,os.readlink(path)]
        elif stat.S_ISDIR(info.st_mode):
            value=['dir',relative,mode]
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink!=1:raise ValueError('工作区含硬链接，无法安全暂存：'+relative)
            content=hashlib.sha256()
            with path.open('rb') as stream:
                while chunk:=stream.read(1024*1024):content.update(chunk)
            value=['file',relative,mode,content.hexdigest()]
        else:raise ValueError('工作区含不支持的特殊文件：'+relative)
        if version==2:value.extend(metadata_signature(path,info))
        digest.update(json.dumps(value,ensure_ascii=True,separators=(',',':')).encode())
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir(),key=lambda p:p.name):walk(child,relative+'/'+child.name)
    walk(root,'')
    return digest.hexdigest()


def _copy_tree(source,destination):
    if sys.platform=='darwin':
        # ditto preserves xattrs/resource forks and ACLs that this Python build's
        # shutil.copy2 cannot copy. Destination must be a new host-owned tree.
        if Path(destination).exists():raise FileExistsError(destination)
        subprocess.run(['/usr/bin/ditto',str(source),str(destination)],check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=300)
    else:
        shutil.copytree(source,destination,symlinks=True)


def _digest(root,meta):
    return _fingerprint(root,version=meta.get('fingerprint_version',1))


def _atomic_json(path,value):
    temporary=path.with_suffix('.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)
    _sync_dir(path.parent)


def _sync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def _exchange(first,second):
    """No per-file write fallback: unsupported atomic exchange fails closed."""
    libc=ctypes.CDLL(None,use_errno=True)
    if platform.system()=='Darwin':
        function=libc.renamex_np
        function.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_uint]
        result=function(os.fsencode(first),os.fsencode(second),2)  # RENAME_SWAP
    elif platform.system()=='Linux' and hasattr(libc,'renameat2'):
        function=libc.renameat2
        function.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        result=function(-100,os.fsencode(first),-100,os.fsencode(second),2)  # RENAME_EXCHANGE
    else:raise RuntimeError('当前系统不支持原子目录交换，未提交')
    if result:
        number=ctypes.get_errno()
        raise OSError(number,os.strerror(number))


@contextmanager
def _lock(path):
    import fcntl  # POSIX-only implementation; never imported by Windows startup.
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('工作区正在被另一个任务提交或恢复') from None
        yield
    finally:os.close(fd)


class WorkspacePublicationError(RuntimeError):
    """Static, safe user-facing publication outcome; never contains tool output."""


class StagedWorkspace:
    def __init__(self,target,task_id=None):
        self.target=Path(target).resolve()
        identity=str(task_id or uuid.uuid4().hex)
        self.id=hashlib.sha256(identity.encode()).hexdigest()[:32]
        self.control=self.target.parent/('.'+self.target.name+'-muse-transactions')
        self.directory=self.control/self.id
        self.files=self.directory/'files'
        self.scratch=self.directory/'scratch'
        self.publication=self.directory/'publication'
        self.journal=self.directory/'transaction.json'
        self.meta=None
        self._lease=None
        self.token=None

    def begin(self):
        if platform.system() not in ('Darwin','Linux') or not transactional_files_supported():
            raise RuntimeError('当前系统尚未适配文件事务与写入隔离，未启动本地文件任务')
        if self._lease is not None:raise RuntimeError('该暂存对象已经持有任务锁')
        self.target.mkdir(parents=True,exist_ok=True)
        if self.control.is_symlink():raise ValueError('事务目录不能是符号链接')
        self.control.mkdir(mode=0o700,exist_ok=True)
        if self.control.stat().st_dev!=self.target.stat().st_dev:
            raise RuntimeError('正式工作区与暂存区必须位于同一文件系统，未启动文件修改')
        self.directory.mkdir(mode=0o700,exist_ok=True)
        if self.directory.is_symlink():raise ValueError('任务目录不能是符号链接')
        lease=_lock(self.directory/'task.lock')
        lease.__enter__()
        self._lease=lease
        try:
            with _lock(self.control/'publish.lock'):
                self._recover_pending()
                if self.journal.exists():
                    self.meta=self._read_journal(self.journal)
                    if self.meta['state'] not in ('paused','staged'):
                        raise RuntimeError('该任务已经结束，不能再次执行或提交')
                    if _digest(self.target,self.meta)!=self.meta['baseline']:
                        raise RuntimeError('暂停期间正式工作区已变化，未恢复旧暂存任务')
                else:
                    # No receipt means initialization did not finish. Never merge a
                    # partial copy into a fresh baseline or remove an unexplained backup.
                    if self.publication.exists() or self.publication.is_symlink():
                        raise RuntimeError('事务记录缺失但存在提交副本，需要人工处理')
                    for leftover in (self.files,self.scratch):
                        self._safe_directory(leftover,required=False)
                        if leftover.exists():shutil.rmtree(leftover)
                    baseline=_fingerprint(self.target)
                    _copy_tree(self.target,self.files)
                    if _fingerprint(self.target)!=baseline or _fingerprint(self.files)!=baseline:
                        raise RuntimeError('创建暂存副本期间正式工作区发生变化，未启动 Agent')
                    self.meta={'commit_id':self.id,'target':str(self.target),'baseline':baseline,
                               'state':'staged','fingerprint_version':2,'created_at':time.time()}
                self._safe_directory(self.files)
                self._safe_directory(self.scratch,required=False)
                self.scratch.mkdir(exist_ok=True,mode=0o700)
                self.meta['state']='staged'
                _atomic_json(self.journal,self.meta)
            self.token=active_workspace.set(self)
            return self
        except BaseException:
            self.release()
            raise

    def _require_lease(self):
        if self._lease is None:raise RuntimeError('当前对象未持有任务锁，不能修改或提交事务')

    @staticmethod
    def _safe_directory(path,required=True):
        if path.is_symlink() or path.exists() and not path.is_dir():
            raise RuntimeError('事务记录引用的目录无效，拒绝继续')
        if required and not path.is_dir():raise RuntimeError('事务记录引用的目录缺失，拒绝继续')

    def _read_journal(self,path):
        try:
            if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size>65536:
                raise ValueError('invalid journal path')
            meta=json.loads(path.read_text())
            if (not isinstance(meta,dict) or meta.get('target')!=str(self.target)
                    or meta.get('commit_id')!=path.parent.name
                    or not re.fullmatch(r'[0-9a-f]{32}',str(meta.get('commit_id','')))
                    or not re.fullmatch(r'[0-9a-f]{64}',str(meta.get('baseline','')))
                    or meta.get('fingerprint_version',1) not in (1,2)
                    or meta.get('state') not in {'staged','paused','prepared','committed','aborted','conflict'}):
                raise ValueError('invalid journal identity')
            if meta['state'] in ('prepared','committed') and not re.fullmatch(r'[0-9a-f]{64}',str(meta.get('final',''))):
                raise ValueError('invalid final fingerprint')
            return meta
        except (OSError,ValueError,TypeError):
            raise RuntimeError('事务记录无效或身份不匹配，需要人工处理') from None

    def _recover_pending(self):
        # Resolve any crash between exchange and its durable receipt before a new snapshot.
        for journal in self.control.glob('*/transaction.json'):
            meta=self._read_journal(journal)
            if meta.get('state')!='prepared':continue
            files=journal.parent/'publication'
            self._safe_directory(files)
            target_hash=_digest(self.target,meta)
            staged_hash=_digest(files,meta)
            if target_hash==meta['final'] and staged_hash==meta['baseline']:
                meta['state']='committed'
            elif target_hash==meta['baseline'] and staged_hash==meta['final']:
                # The exchange never occurred; keep changes available for explicit recovery.
                meta['state']='paused'
            else:raise RuntimeError('发现未完成的文件提交且内容存在冲突，需要人工处理')
            _atomic_json(journal,meta)

    def _check_links(self,base=None):
        base=base or self.files
        self._safe_directory(base)
        if base.lstat().st_mode & (stat.S_ISUID|stat.S_ISGID):raise ValueError('不提交 setuid/setgid 目录')
        for root,dirs,files in os.walk(base,followlinks=False):
            for name in dirs+files:
                path=Path(root)/name
                relative=path.relative_to(base)
                info=path.lstat()
                if info.st_mode & (stat.S_ISUID|stat.S_ISGID):raise ValueError('不提交 setuid/setgid 文件')
                if path.is_symlink():
                    original=self.target/relative
                    if not original.is_symlink() or os.readlink(path)!=os.readlink(original):
                        raise ValueError('不允许提交新增或修改的符号链接')

    def _flush_files(self):
        for root,dirs,files in os.walk(self.publication,followlinks=False):
            for name in files:
                path=Path(root)/name
                if path.is_symlink():continue
                fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
                try:os.fsync(fd)
                finally:os.close(fd)
            _sync_dir(root)

    def commit(self):
        if self.meta is None:raise RuntimeError('任务尚未启动')
        with _lock(self.control/'publish.lock'):
            persisted=self._read_journal(self.journal)
            if persisted['state']=='committed':return persisted
            self._require_lease()
            self.meta=persisted
            if persisted['state']=='prepared':
                self._recover_pending()
                persisted=self._read_journal(self.journal)
                if persisted['state']=='committed':self.meta=persisted;return persisted
            if persisted['state'] not in ('staged','paused'):raise RuntimeError('任务不可提交')
            self._check_links()
            final=_digest(self.files,self.meta)
            if _digest(self.target,self.meta)!=self.meta['baseline']:
                self.meta['state']='conflict';_atomic_json(self.journal,self.meta)
                raise RuntimeError('正式工作区已被其他任务修改，暂存变更未写回')
            if final==self.meta['baseline']:
                self.meta.update(state='committed',changed=False,final=final)
                _atomic_json(self.journal,self.meta)
                return self.meta
            # Freeze a host-only publication copy. Even a surviving child holding an
            # open staging FD cannot mutate the published inodes after the swap.
            if self.publication.is_symlink():raise ValueError('提交目录不能是符号链接')
            if self.publication.exists():shutil.rmtree(self.publication)
            _copy_tree(self.files,self.publication)
            self._check_links(self.publication)
            if _digest(self.publication,self.meta)!=final:
                raise RuntimeError('暂存区在生成最终版本期间发生变化，未提交')
            self._flush_files()
            if _digest(self.target,self.meta)!=self.meta['baseline']:
                self.meta['state']='conflict';_atomic_json(self.journal,self.meta)
                raise RuntimeError('正式工作区已被其他任务修改，暂存变更未写回')
            self.meta.update(state='prepared',changed=True,final=final)
            _atomic_json(self.journal,self.meta)
            _exchange(self.publication,self.target)
            _sync_dir(self.target.parent);_sync_dir(self.directory)
            self.meta.update(state='committed',committed_at=time.time())
            _atomic_json(self.journal,self.meta)
            return self.meta

    def cleanup_committed(self):
        self._require_lease()
        if self._read_journal(self.journal)['state']!='committed':
            raise RuntimeError('未完成提交，不能清理暂存证据')
        for path in (self.files,self.scratch,self.publication):
            if path.is_symlink():raise ValueError('暂存根目录不能是符号链接')
            if path.exists():shutil.rmtree(path)

    def abandon(self,paused=False):
        self._require_lease()
        if self.meta:self.meta=self._read_journal(self.journal)
        if self.meta and self.meta['state']=='staged':
            self.meta['state']='paused' if paused else 'aborted'
            _atomic_json(self.journal,self.meta)

    def release(self):
        if self.token is not None:
            active_workspace.reset(self.token);self.token=None
        if self._lease is not None:
            self._lease.__exit__(None,None,None);self._lease=None

    def sandbox_command(self,command):
        system=platform.system()
        if system=='Darwin' and Path('/usr/bin/sandbox-exec').exists():
            # Descendants inherit Seatbelt. Deny hardlinks as an inode-based escape.
            profile='(version 1)(allow default)(deny file-write*)(deny file-link)(deny network*)(deny mach-lookup)(deny appleevent-send)'
            # Preserve the existing CLI -> trusted Muse API path, not general network access.
            endpoint=urlsplit(os.getenv('MUSE_URL') or 'http://127.0.0.1:'+os.getenv('MUSE_PORT','8000'))
            if endpoint.hostname in ('127.0.0.1','localhost','::1') and endpoint.scheme in ('http','https'):
                port=endpoint.port or (443 if endpoint.scheme=='https' else 80)
                profile+='(allow network-outbound (remote ip "localhost:'+str(port)+'"))'
            for path in (self.files,self.scratch):
                profile+='(allow file-write* (subpath '+json.dumps(str(path))+'))'
            profile+='(allow file-write* (literal "/dev/null") (literal "/dev/tty"))'
            return ['/usr/bin/sandbox-exec','-p',profile,*command]
        binary=shutil.which('bwrap') if system=='Linux' else None
        if binary:
            return [binary,'--die-with-parent','--unshare-pid','--unshare-net','--unshare-ipc','--ro-bind','/','/',
                    '--bind',str(self.files),str(self.files),'--bind',str(self.scratch),str(self.scratch),
                    '--dev','/dev','--proc','/proc','--',*command]
        raise RuntimeError('当前宿主机缺少写入隔离能力，已拒绝执行命令；不会退回直接写正式目标')


def create_staged_workspace(target,task_id=None):
    from .host import system_name,transactional_files_supported
    if system_name()=='Windows':
        if not transactional_files_supported():raise RuntimeError('Windows 文件事务需要 Windows 10 或更高版本')
        from .windows_workspace import WindowsStagedWorkspace
        return WindowsStagedWorkspace(target,task_id)
    return StagedWorkspace(target,task_id)
