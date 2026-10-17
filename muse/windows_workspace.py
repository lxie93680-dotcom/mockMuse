"""Journaled, original-path Windows publication. NOT a directory atomic swap.

Only controlled in-process file tools may edit the staging tree. Arbitrary
commands/MCP filesystem processes remain unavailable without an OS sandbox.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from contextlib import ExitStack,contextmanager
from pathlib import Path

from .staged_workspace import StagedWorkspace,active_workspace
from .windows_files import WindowsFiles,reparse,valid_relative

MAX_ENTRIES=10000


def durable_json(path,value):
    if path.is_symlink() or path.exists() and reparse(path) or reparse(path.parent):raise RuntimeError('事务元数据路径无效')
    fd,name=tempfile.mkstemp(prefix='journal-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,sort_keys=True)
            stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        if os.name!='nt':
            fd=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
    finally:Path(name).unlink(missing_ok=True)


@contextmanager
def transaction_lock(path):
    if path.exists() and reparse(path):raise RuntimeError('事务锁路径无效')
    fd=os.open(path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        if os.name=='nt':
            import msvcrt
            if os.fstat(fd).st_size==0:os.write(fd,b'0');os.fsync(fd)
            os.lseek(fd,0,0)
            try:msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            except OSError:raise RuntimeError('Windows 工作区正由其他任务使用') from None
        else:
            import fcntl
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Windows 工作区正由其他任务使用') from None
        yield
    finally:os.close(fd)


def digest(entries):
    return hashlib.sha256(json.dumps(entries,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class WindowsStagedWorkspace(StagedWorkspace):
    def __init__(self,target,task_id=None,*,io=None):
        super().__init__(target,task_id)
        self.control=self.target.parent/('.'+self.target.name+'-muse-win-transactions')
        self.directory=self.control/self.id
        self.files=self.directory/'files';self.scratch=self.directory/'scratch'
        self.publication=self.directory/'publication';self.backup=self.directory/'backup'
        self.journal=self.directory/'transaction.json'
        self.io=io if io is not None else WindowsFiles()

    def manifest(self,root,held=None):
        entries={};held=held or {}
        def walk(path,key):
            if key not in held:
                if reparse(path):raise ValueError('Windows 工作区不允许符号链接、junction 或其他重解析点')
                info=path.lstat()
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise ValueError('Windows 工作区含不支持的特殊文件')
            value=held[key].signature if key in held else self.io.describe(path)[0]
            entries[key]=value
            if len(entries)>MAX_ENTRIES:raise ValueError('Windows 工作区超过10000项，未开始提交')
            if value['kind']=='dir':
                names=set()
                for child in sorted(path.iterdir(),key=lambda p:p.name):
                    valid_relative(child.name)
                    if child.name.casefold() in names:raise ValueError('Windows 工作区存在大小写冲突')
                    names.add(child.name.casefold())
                    walk(child,key+'/'+child.name if key else child.name)
        walk(root,'')
        return entries

    def copy_tree(self,source,destination):
        destination.mkdir()
        for child in source.iterdir():
            if reparse(child):raise ValueError('不复制 Windows 重解析点')
            target=destination/child.name
            if child.is_dir():self.copy_tree(child,target)
            else:self.io.copy_file(child,target)
        self.io.copy_metadata(source,destination)

    def _read_journal(self,path):
        try:
            if reparse(path) or reparse(path.parent) or path.stat().st_size>16*1024*1024:raise ValueError()
            meta=json.loads(path.read_text(encoding='utf-8'))
            if (meta.get('backend')!='windows-writeback-v1' or meta.get('target')!=str(self.target) or
                meta.get('commit_id')!=path.parent.name or not re.fullmatch('[0-9a-f]{32}',path.parent.name) or
                meta.get('state') not in {'staged','paused','prepared','applying','committed','aborted','conflict','rolled_back','recovery_blocked'}):
                raise ValueError()
            base=meta['base_entries']
            if not isinstance(base,dict) or '' not in base or len(base)>MAX_ENTRIES or digest(base)!=meta['baseline']:raise ValueError()
            for entries in (base,meta.get('final_entries',{})):
                for key,value in entries.items():
                    if key:valid_relative(key)
                    if value.get('kind') not in ('dir','file'):raise ValueError()
            if meta.get('changed'):
                final=meta['final_entries']
                if '' not in final or len(final)>MAX_ENTRIES or digest(final)!=meta['final']:raise ValueError()
                expected={key for key in set(base)|set(final) if base.get(key)!=final.get(key)}
                paths=[op['path'] for op in meta.get('plan',[])]
                if set(paths)!=expected or len(paths)!=len(expected):raise ValueError()
            for op in meta.get('plan',[]):
                valid_relative(op['path'])
                if op.get('slot')!=hashlib.sha256(op['path'].encode()).hexdigest():raise ValueError()
                if op.get('before')!=base.get(op['path']) or op.get('after')!=meta['final_entries'].get(op['path']):raise ValueError()
                if op.get('status') not in ('pending','started','done','undone'):raise ValueError()
                for field in ('old_id','new_id'):
                    identity=op.get(field)
                    if identity is not None and (not isinstance(identity,list) or not 2<=len(identity)<=3 or
                                                any(type(part)!=int or part<0 for part in identity)):raise ValueError()
            return meta
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            raise RuntimeError('Windows 事务记录无效，需要人工检查，未写回文件') from None

    def save(self):durable_json(self.journal,self.meta)

    def begin(self):
        if self._lease is not None:raise RuntimeError('该暂存对象已经持有任务锁')
        self.target.mkdir(parents=True,exist_ok=True)
        self.io.check_volume(self.target)
        for path in (self.target,self.control,self.directory):
            if path.exists() and reparse(path):raise ValueError('Windows 事务根目录不能是重解析点')
        self.control.mkdir(exist_ok=True);self.directory.mkdir(exist_ok=True)
        lease=transaction_lock(self.directory/'task.lock');lease.__enter__();self._lease=lease
        try:
            with transaction_lock(self.control/'publish.lock'):
                self._recover_pending()
                if self.journal.exists():
                    self.meta=self._read_journal(self.journal)
                    if self.meta['state'] not in ('staged','paused'):raise RuntimeError('该 Windows 文件任务已结束，不能重复执行')
                    if self.manifest(self.target)!=self.meta['base_entries']:raise RuntimeError('暂停期间原目录已修改，未恢复旧暂存区')
                else:
                    if self.backup.exists():raise RuntimeError('缺失日志但存在备份，拒绝覆盖恢复证据')
                    for path in (self.files,self.scratch,self.publication):
                        if path.exists():
                            if reparse(path):raise ValueError('暂存目录不能是重解析点')
                            shutil.rmtree(path)
                    base=self.manifest(self.target)
                    self.copy_tree(self.target,self.files)
                    if self.manifest(self.target)!=base or self.manifest(self.files)!=base:
                        raise RuntimeError('创建 Windows 暂存副本时内容或权限变化，未运行 Agent')
                    self.meta={'backend':'windows-writeback-v1','target':str(self.target),'commit_id':self.id,
                               'baseline':digest(base),'base_entries':base,'state':'staged','created_at':time.time()}
                self.scratch.mkdir(exist_ok=True)
                self.meta['state']='staged';self.save()
            self.token=active_workspace.set(self)
            return self
        except BaseException:self.release();raise

    def _plan(self,final):
        base=self.meta['base_entries'];ops=[]
        if base['']!=final['']:raise ValueError('不支持修改工作区根目录权限或属性，未写回')
        folded={key.casefold():key for key in base}
        if any(key.casefold() in folded and key!=folded[key.casefold()] for key in final):
            raise ValueError('暂不支持仅改变大小写的路径重命名，未写回')
        for key in sorted(set(base)|set(final)):
            if not key or base.get(key)==final.get(key):continue
            before,after=base.get(key),final.get(key)
            if before and after and (before['kind']!='file' or after['kind']!='file'):
                raise ValueError('暂不支持文件与目录互换或修改已有目录元数据，未写回')
            ops.append({'path':key,'before':before,'after':after,'status':'pending',
                        'slot':hashlib.sha256(key.encode()).hexdigest()})
        rank=lambda op:(0 if op['before'] is None and op['after']['kind']=='dir' else
                        2 if op['after'] is None and op['before']['kind']=='dir' else 1)
        return sorted(ops,key=lambda op:(rank(op),-op['path'].count('/') if rank(op)==2 else op['path'].count('/'),op['path']))

    def commit(self):
        self._require_lease()
        with transaction_lock(self.control/'publish.lock'):
            self.meta=self._read_journal(self.journal)
            if self.meta['state']=='committed':return self.meta
            if self.meta['state'] not in ('staged','paused'):raise RuntimeError('Windows 文件任务不可重复提交')
            final=self.manifest(self.files)
            if self.manifest(self.target)!=self.meta['base_entries']:
                self.meta['state']='conflict';self.save();raise RuntimeError('原目录被修改，未写回、不自动合并')
            plan=self._plan(final)
            if not plan:
                self.meta.update(state='committed',changed=False,final=digest(final));self.save();return self.meta
            if self.publication.exists():shutil.rmtree(self.publication)
            self.copy_tree(self.files,self.publication)
            if self.manifest(self.publication)!=final:raise RuntimeError('最终副本校验失败，未写回')
            if self.backup.exists() and any(self.backup.iterdir()):raise RuntimeError('提交前发现备份，拒绝覆盖恢复证据')
            self.backup.mkdir(exist_ok=True)
            objects=self.directory/'objects'
            if objects.exists():
                if reparse(objects):raise ValueError('提交对象目录无效')
                shutil.rmtree(objects)
            objects.mkdir()
            # Each newly added directory is a separate empty object, so moving
            # its namespace cannot accidentally publish its child files early.
            for op in plan:
                if op['after'] and op['after']['kind']=='dir':
                    obj=objects/op['slot'];obj.mkdir();self.io.copy_metadata(self.publication/op['path'],obj)
            self.meta.update(final=digest(final),final_entries=final,plan=plan,changed=True)
            prepared=False
            try:
                with ExitStack() as stack:
                    held={}
                    for key,value in sorted(self.meta['base_entries'].items(),key=lambda x:x[0].count('/')):
                        if value['kind']=='dir':held[key]=stack.enter_context(self.io.guard(self.target/key,directory=True))
                    for op in plan:
                        if op['before']:
                            old=held.get(op['path']) or stack.enter_context(self.io.guard(self.target/op['path'],directory=op['before']['kind']=='dir'))
                            if old.signature!=op['before']:raise RuntimeError('写回前文件已修改或被占用')
                            held[op['path']]=old;op['old_id']=old.identity
                        if op['after']:
                            source=objects/op['slot'] if op['after']['kind']=='dir' else self.publication/op['path']
                            new=stack.enter_context(self.io.guard(source,directory=op['after']['kind']=='dir'))
                            if new.signature!=op['after']:raise RuntimeError('最终副本发生变化')
                            op['new_id']=new.identity;op['_new']=new
                    if self.manifest(self.target,held)!=self.meta['base_entries']:raise RuntimeError('写回前原目录发生变化')
                    runtime={op['path']:op.pop('_new') for op in plan if '_new' in op}
                    self.meta['state']='prepared';self.save();prepared=True
                    self.meta['state']='applying';self.save()
                    try:
                        for op in plan:
                            op['status']='started';self.save()
                            if op['before']:
                                if op['before']['kind']=='dir' and any((self.target/op['path']).iterdir()):
                                    raise RuntimeError('待删除目录出现了外部文件，停止提交')
                                held[op['path']].move(self.backup/op['slot'])
                                if op['before']['kind']=='dir' and any((self.backup/op['slot']).iterdir()):
                                    raise RuntimeError('目录移动期间出现外部文件，恢复原目录')
                            if op['after']:
                                runtime[op['path']].move(self.target/op['path'])
                                held[op['path']]=runtime[op['path']]
                            else:held.pop(op['path'],None)
                            op['status']='done';self.save()
                        if self.manifest(self.target,held)!=final:raise RuntimeError('写回期间原目录发生并发变化')
                        self.meta.update(state='committed',committed_at=time.time());self.save()
                    except Exception:
                        # Release handles before the recovery pass reopens them;
                        # inverse operations compare object identity AND content.
                        raise
            except Exception:
                if prepared:self._rollback(self.journal,self.meta)
                else:
                    for op in plan:op.pop('_new',None)
                    self.meta['state']='conflict';self.save()
                raise
            return self.meta

    def _rollback(self,journal,meta):
        directory=journal.parent;blocked=[]
        (directory/'reverted').mkdir(exist_ok=True)
        for op in reversed(meta['plan']):
            if op['status'] in ('pending','undone'):continue
            target=self.target/op['path'];backup=directory/'backup'/op['slot']
            try:
                with ExitStack() as stack:
                    # Guard every existing ancestor before using a path. Never
                    # follow an editor-created junction/symlink during recovery.
                    parents=[self.target,*[self.target/Path(op['path']).parents[i] for i in range(len(Path(op['path']).parents)-2,-1,-1)]]
                    for parent in dict.fromkeys(parents):
                        if parent.exists():stack.enter_context(self.io.guard(parent,directory=True))
                    if target.exists():
                        current=stack.enter_context(self.io.guard(target,directory=target.is_dir()))
                        if op['before'] and current.signature==op['before'] and current.identity==op.get('old_id') and not backup.exists():
                            op['status']='undone';durable_json(journal,meta);continue
                        if not (op['after'] and current.signature==op['after'] and current.identity==op.get('new_id')):
                            raise RuntimeError('目标已被外部修改')
                        if current.signature['kind']=='dir' and any(target.iterdir()):raise RuntimeError('新目录含外部文件')
                        current.move(directory/'reverted'/op['slot'])
                    elif op['after']:
                        source=directory/'objects'/op['slot'] if op['after']['kind']=='dir' else directory/'publication'/op['path']
                        reverted=directory/'reverted'/op['slot']
                        if reverted.exists():
                            moved=stack.enter_context(self.io.guard(reverted,directory=op['after']['kind']=='dir'))
                            if moved.signature!=op['after'] or moved.identity!=op.get('new_id'):
                                raise RuntimeError('恢复中间副本被修改')
                        elif op['status']=='done' or not source.exists():
                            raise RuntimeError('已发布的文件被外部删除，停止恢复')
                    if op['before']:
                        if not backup.exists():raise RuntimeError('备份缺失')
                        old=stack.enter_context(self.io.guard(backup,directory=op['before']['kind']=='dir'))
                        if old.signature!=op['before'] or old.identity!=op.get('old_id'):raise RuntimeError('备份发生变化')
                        old.move(target)
                    op['status']='undone';durable_json(journal,meta)
            except (OSError,RuntimeError,ValueError):blocked.append(op['path'])
        meta.update(state='recovery_blocked' if blocked else 'rolled_back',conflicts=blocked)
        durable_json(journal,meta)
        if journal==self.journal:self.meta=meta
        if blocked:raise RuntimeError('Windows 写回恢复遇到外部修改，保留用户文件、备份和暂存，需要人工处理')

    def _recover_pending(self):
        for journal in sorted(self.control.glob('*/transaction.json')):
            meta=self._read_journal(journal)
            if meta['state'] not in ('prepared','applying','recovery_blocked'):continue
            with ExitStack() as stack:
                if journal!=self.journal:stack.enter_context(transaction_lock(journal.parent/'task.lock'))
                self._rollback(journal,meta)

    def abandon(self,paused=False):
        self._require_lease()
        if self.meta:
            self.meta=self._read_journal(self.journal)
            if self.meta['state']=='staged':
                self.meta['state']='paused' if paused else 'aborted';self.save()

    def cleanup_committed(self):
        self._require_lease()
        if self._read_journal(self.journal)['state']!='committed':raise RuntimeError('未提交，不能清理')
        # Keep the original backups and the journal for recovery/auditing.
        for path in (self.files,self.scratch,self.publication,self.directory/'objects'):
            if path.exists():
                if reparse(path):raise ValueError('拒绝清理重解析点')
                shutil.rmtree(path)

    def sandbox_command(self,command):
        raise RuntimeError('Windows 尚无命令写入沙箱，禁止通过终端或外部 Filesystem MCP 绕过暂存工具')


def recover_windows_workspace(target):
    transaction=WindowsStagedWorkspace(target)
    if not transaction.control.exists():return {'status':'ready','backend':'windows-writeback-v1'}
    if reparse(transaction.control):raise RuntimeError('Windows 事务控制目录无效')
    transaction.io.check_volume(transaction.target)
    with transaction_lock(transaction.control/'publish.lock'):transaction._recover_pending()
    return {'status':'ready','backend':'windows-writeback-v1'}
