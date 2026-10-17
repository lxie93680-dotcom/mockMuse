"""Journal protocol fault injection runs on every host; Win32 tests need Windows.

The emulated IO deliberately does not claim to implement Windows sharing locks.
"""
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from muse.windows_workspace import WindowsStagedWorkspace,digest,transaction_lock
from muse.windows_files import valid_relative
from muse.native_tools import NativeRuntime,apply_patch_text


class EmulatedFiles:
    """Test-only rename model; no production fallback uses this class."""
    hook=None
    def check_volume(self,path):pass
    def describe(self,path):
        info=path.lstat()
        if path.is_symlink() or not path.is_dir() and info.st_nlink!=1:raise ValueError('link')
        result={'kind':'dir' if path.is_dir() else 'file','mode':stat.S_IMODE(info.st_mode)}
        if not path.is_dir():result.update(size=info.st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        return result,[info.st_dev,info.st_ino]
    def copy_metadata(self,source,destination):shutil.copystat(source,destination)
    def copy_file(self,source,destination):shutil.copy2(source,destination)
    @contextmanager
    def guard(self,path,directory=False,read_only=False):
        owner=self
        class Handle:
            def __init__(self):self.signature,self.identity=owner.describe(path);self.path=path
            def move(self,destination):
                if owner.hook:owner.hook('before',self.path,destination)
                if destination.exists():raise FileExistsError(destination)
                self.path.rename(destination);old=self.path;self.path=destination
                if owner.hook:owner.hook('after',old,destination)
        yield Handle()


class WritebackProtocol(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='Muse Windows 中文 ')
        self.parent=Path(self.temp.name).resolve();self.target=self.parent/'project';self.target.mkdir()
        (self.target/'a.txt').write_text('original a',encoding='utf-8')
        (self.target/'b.txt').write_text('original b',encoding='utf-8')
        self.io=EmulatedFiles();self.tx=None
    def tearDown(self):
        if self.tx:self.tx.release()
        self.temp.cleanup()
    def begin(self,task='task'):
        self.tx=WindowsStagedWorkspace(self.target,task,io=self.io).begin();return self.tx
    def edit(self):
        tx=self.begin();(tx.files/'a.txt').write_text('agent a',encoding='utf-8');return tx
    def restart(self):
        self.tx.release();self.tx=None;self.io.hook=None
        return self.begin('next-task')

    def test_stage_then_writeback_same_path_and_keep_original_backup(self):
        tx=self.edit()
        self.assertEqual((self.target/'a.txt').read_text(),'original a')
        result=tx.commit();self.assertEqual(result['state'],'committed')
        self.assertEqual((self.target/'a.txt').read_text(),'agent a')
        slot=result['plan'][0]['slot'];self.assertEqual((tx.backup/slot).read_text(),'original a')
        self.assertEqual(tx.commit()['state'],'committed')
        tx.cleanup_committed();self.assertTrue((tx.backup/slot).exists());self.assertTrue(tx.journal.exists())

    def test_preexisting_user_change_conflicts_without_merge_or_overwrite(self):
        tx=self.edit();(self.target/'a.txt').write_text('user edit')
        with self.assertRaisesRegex(RuntimeError,'不自动合并'):tx.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'user edit')
        self.assertEqual(tx.meta['state'],'conflict');self.assertTrue((tx.files/'a.txt').exists())

    def test_new_editor_file_in_rename_gap_is_never_replaced_or_rolled_back(self):
        tx=self.edit()
        def hook(phase,source,destination):
            if phase=='after' and source==self.target/'a.txt':
                (self.target/'a.txt').write_text('editor wins');self.io.hook=None
        self.io.hook=hook
        with self.assertRaisesRegex(RuntimeError,'人工处理'):tx.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'editor wins')
        self.assertEqual(tx.meta['state'],'recovery_blocked')
        self.assertEqual(next(tx.backup.iterdir()).read_text(),'original a')
        with self.assertRaisesRegex(RuntimeError,'人工处理'):self.restart()
        self.assertEqual((self.target/'a.txt').read_text(),'editor wins')

    def test_io_failure_after_first_file_restores_prior_changes(self):
        tx=self.edit();(tx.files/'b.txt').write_text('agent b')
        def hook(phase,source,destination):
            if phase=='before' and source==self.target/'b.txt':
                self.io.hook=None;raise OSError('injected disk failure')
        self.io.hook=hook
        with self.assertRaises(OSError):tx.commit()
        self.assertEqual(tx.meta['state'],'rolled_back')
        self.assertEqual((self.target/'a.txt').read_text(),'original a')
        self.assertEqual((self.target/'b.txt').read_text(),'original b')

    def test_crash_between_backup_and_new_file_recovers_at_next_task(self):
        tx=self.edit()
        def hook(phase,source,destination):
            if phase=='after' and source==self.target/'a.txt':raise KeyboardInterrupt()
        self.io.hook=hook
        with self.assertRaises(KeyboardInterrupt):tx.commit()
        self.assertFalse((self.target/'a.txt').exists())
        journal=tx.journal;self.restart()
        self.assertEqual((self.target/'a.txt').read_text(),'original a')
        self.assertEqual(json.loads(journal.read_text())['state'],'rolled_back')

    def test_crash_after_new_file_moved_before_receipt_recovers(self):
        tx=self.edit()
        def hook(phase,source,destination):
            if phase=='after' and destination==self.target/'a.txt':raise KeyboardInterrupt()
        self.io.hook=hook
        with self.assertRaises(KeyboardInterrupt):tx.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'agent a')
        journal=tx.journal;self.restart()
        self.assertEqual((self.target/'a.txt').read_text(),'original a')
        self.assertEqual(json.loads(journal.read_text())['state'],'rolled_back')

    def test_edit_after_crash_blocks_recovery_instead_of_losing_user_edit(self):
        tx=self.edit()
        def hook(phase,source,destination):
            if phase=='after' and destination==self.target/'a.txt':raise KeyboardInterrupt()
        self.io.hook=hook
        with self.assertRaises(KeyboardInterrupt):tx.commit()
        (self.target/'a.txt').write_text('user after crash')
        with self.assertRaisesRegex(RuntimeError,'人工处理'):self.restart()
        self.assertEqual((self.target/'a.txt').read_text(),'user after crash')

    def test_identical_content_with_different_file_identity_is_not_removed(self):
        tx=self.edit()
        def hook(phase,source,destination):
            if phase=='after' and destination==self.target/'a.txt':raise KeyboardInterrupt()
        self.io.hook=hook
        with self.assertRaises(KeyboardInterrupt):tx.commit()
        replacement=self.target/'replacement';replacement.write_text('agent a');replacement.replace(self.target/'a.txt')
        with self.assertRaisesRegex(RuntimeError,'人工处理'):self.restart()
        self.assertEqual((self.target/'a.txt').read_text(),'agent a')

    def test_user_deletion_after_crash_is_not_recreated_by_recovery(self):
        tx=self.edit()
        def hook(phase,source,destination):
            if phase=='after' and destination==self.target/'a.txt':raise KeyboardInterrupt()
        self.io.hook=hook
        with self.assertRaises(KeyboardInterrupt):tx.commit()
        (self.target/'a.txt').unlink()
        with self.assertRaisesRegex(RuntimeError,'人工处理'):self.restart()
        self.assertFalse((self.target/'a.txt').exists())

    def test_external_file_added_during_directory_move_is_restored(self):
        (self.target/'empty').mkdir();tx=self.edit();(tx.files/'empty').rmdir()
        def hook(phase,source,destination):
            if phase=='after' and source==self.target/'empty':
                (destination/'user.txt').write_text('user content');self.io.hook=None
        self.io.hook=hook
        with self.assertRaisesRegex(RuntimeError,'恢复原目录'):tx.commit()
        self.assertEqual((self.target/'empty/user.txt').read_text(),'user content')
        self.assertEqual((self.target/'a.txt').read_text(),'original a')

    def test_partial_recovery_itself_can_be_retried_without_overwrite(self):
        tx=self.edit()
        def crash(phase,source,destination):
            if phase=='after' and destination==self.target/'a.txt':raise KeyboardInterrupt()
        self.io.hook=crash
        with self.assertRaises(KeyboardInterrupt):tx.commit()
        tx.release();self.tx=None
        def recovery_crash(phase,source,destination):
            if phase=='after' and destination.parent.name=='reverted':raise KeyboardInterrupt()
        self.io.hook=recovery_crash
        with self.assertRaises(KeyboardInterrupt):self.begin('next-task')
        self.io.hook=None;self.begin('next-again')
        self.assertEqual((self.target/'a.txt').read_text(),'original a')

    def test_added_files_directories_deleted_files_and_empty_directories(self):
        (self.target/'empty').mkdir();tx=self.begin()
        (tx.files/'a.txt').unlink();(tx.files/'empty').rmdir()
        (tx.files/'新目录/sub').mkdir(parents=True);(tx.files/'新目录/sub/new.txt').write_text('new')
        tx.commit()
        self.assertFalse((self.target/'a.txt').exists());self.assertFalse((self.target/'empty').exists())
        self.assertEqual((self.target/'新目录/sub/new.txt').read_text(),'new')

    def test_directory_delete_with_external_addition_is_rolled_back_without_losing_addition(self):
        (self.target/'empty').mkdir();tx=self.edit();(tx.files/'empty').rmdir()
        def hook(phase,source,destination):
            if phase=='before' and source==self.target/'empty':
                (source/'user.txt').write_text('external');self.io.hook=None;raise OSError('busy')
        self.io.hook=hook
        with self.assertRaises(OSError):tx.commit()
        self.assertEqual((self.target/'empty/user.txt').read_text(),'external')
        self.assertEqual((self.target/'a.txt').read_text(),'original a')

    def test_pause_resume_cancel_and_noop_do_not_publish(self):
        tx=self.edit();tx.abandon(paused=True);tx.release()
        self.tx=WindowsStagedWorkspace(self.target,'task',io=self.io).begin()
        self.assertEqual((self.tx.files/'a.txt').read_text(),'agent a')
        self.tx.abandon();self.tx.release();self.tx=None
        self.assertEqual((self.target/'a.txt').read_text(),'original a')
        tx=self.begin('noop');result=tx.commit();self.assertFalse(result['changed'])

    def test_invalid_journal_and_traversal_stop_before_recovery(self):
        tx=self.edit()
        meta=json.loads(tx.journal.read_text());meta['target']=str(self.parent/'other')
        tx.journal.write_text(json.dumps(meta))
        tx.release();self.tx=None
        with self.assertRaisesRegex(RuntimeError,'事务记录无效'):self.begin('next')
        self.assertEqual((self.target/'a.txt').read_text(),'original a')
        for value in ('../a','a:stream','CON.txt','a/..','a.','a ','a\\b','/abs','LPT1'):
            with self.assertRaises(ValueError):valid_relative(value)

    def test_symlink_and_hardlink_are_rejected(self):
        if os.name=='nt':self.skipTest('Win32 reparse test covers Windows')
        (self.target/'link').symlink_to(self.parent)
        with self.assertRaises(ValueError):self.begin()
        (self.target/'link').unlink();os.link(self.target/'a.txt',self.target/'alias')
        with self.assertRaises(ValueError):self.begin('hardlink')

    def test_existing_directory_metadata_and_type_replacements_refused(self):
        (self.target/'dir').mkdir();tx=self.begin()
        (tx.files/'a.txt').unlink();(tx.files/'a.txt').mkdir()
        with self.assertRaises(ValueError):tx.commit()
        self.assertTrue((self.target/'a.txt').is_file())

    def test_transaction_lock_rejects_second_owner(self):
        tx=self.begin()
        with self.assertRaises(RuntimeError):
            with transaction_lock(tx.directory/'task.lock'):pass

    def test_rollback_will_not_replace_external_directory_with_user_files(self):
        tx=self.edit()
        def hook(phase,source,destination):
            if phase=='after' and source==self.target/'a.txt':
                (self.target/'a.txt').mkdir();(self.target/'a.txt/user.txt').write_text('user');self.io.hook=None
        self.io.hook=hook
        with self.assertRaisesRegex(RuntimeError,'人工处理'):tx.commit()
        self.assertEqual((self.target/'a.txt/user.txt').read_text(),'user')


class WindowsAgentIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_real_agent_tool_setup_reads_and_patches_stage_then_publishes(self):
        from agents.tool_context import ToolContext
        from muse.agent import run_agent
        from muse.models import Provider
        from muse.host import capabilities
        with tempfile.TemporaryDirectory() as data:
            target=Path(data).resolve()/'project';target.mkdir();(target/'a.txt').write_text('old\n')
            async def fake_run(agent,**kwargs):
                tools={tool.name:tool for tool in agent.tools}
                self.assertTrue({'read_file','apply_patch','tool_search','skill_read'}<=tools.keys())
                self.assertTrue({'exec_command','write_stdin'}.isdisjoint(tools))
                ctx=ToolContext(context=None,tool_name='read_file',tool_call_id='read',tool_arguments='{}')
                content=json.loads(await tools['read_file'].on_invoke_tool(ctx,json.dumps({'path':'a.txt'})))
                self.assertEqual(content['content'],'old\n')
                ctx=ToolContext(context=None,tool_name='apply_patch',tool_call_id='patch',tool_arguments='{}')
                await tools['apply_patch'].on_invoke_tool(ctx,json.dumps({'patch':'*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch'}))
                self.assertEqual((target/'a.txt').read_text(),'old\n')
                return SimpleNamespace(final_output='done',to_input_list=lambda:[])
            with patch.dict(os.environ,{'MUSE_LOCAL_WORKSPACE':str(target),'MUSE_FILESYSTEM_MCP':'0'}),patch('muse.host.system_name',return_value='Windows'),patch('muse.host.windows_version',return_value={'major':10,'minor':0,'build':22631}),patch('muse.windows_workspace.WindowsFiles',EmulatedFiles),patch('muse.agent.Runner.run',side_effect=fake_run):
                self.assertEqual(capabilities()['publication_mode'],'journaled_writeback')
                answer,_=await run_agent(None,'edit',[],lambda *args:None,provider=Provider('custom','test','test','https://example.invalid','test',{},'CUSTOM_API_KEY'))
                self.assertEqual(answer,'done');self.assertEqual((target/'a.txt').read_text(),'new\n')

    async def test_windows_version_selection_and_terminal_denial(self):
        from muse.host import capabilities
        from muse.staged_workspace import create_staged_workspace
        with tempfile.TemporaryDirectory() as data,patch('muse.host.system_name',return_value='Windows'):
            for version,expected in [({'major':6,'minor':3,'build':9600},False),({'major':10,'minor':0,'build':19045},True),({'major':10,'minor':0,'build':22631},True)]:
                with patch('muse.host.windows_version',return_value=version),patch('muse.windows_workspace.WindowsFiles',EmulatedFiles):
                    self.assertEqual(capabilities()['agent_file_writes'],expected)
                    self.assertFalse(capabilities()['agent_local_commands'])
                    if expected:self.assertIsInstance(create_staged_workspace(Path(data)/'target'),WindowsStagedWorkspace)
                    else:
                        with self.assertRaises(RuntimeError):create_staged_workspace(Path(data)/'target')


@unittest.skipUnless(sys.platform=='win32','Requires native Windows/NTFS')
class NativeWindowsWriteback(unittest.IsolatedAsyncioTestCase):
    async def test_actual_windows_handles_snapshot_patch_and_commit(self):
        from muse.staged_workspace import create_staged_workspace
        from muse.host import capabilities
        with tempfile.TemporaryDirectory(prefix='Muse Win 中文 ') as data:
            target=Path(data)/'project';target.mkdir();(target/'a.txt').write_text('old\n',encoding='utf-8')
            tx=create_staged_workspace(target).begin();runtime=NativeRuntime(require_workspace=True)
            try:
                self.assertEqual(capabilities()['publication_mode'],'journaled_writeback')
                apply_patch_text('*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch')
                with self.assertRaises(PermissionError):await runtime.exec_command('Write-Output unsafe')
                tx.commit();self.assertEqual((target/'a.txt').read_text(),'new\n')
            finally:await runtime.close();tx.release()

    async def test_actual_exclusive_handle_prevents_editor_write_and_replacement(self):
        from muse.windows_files import WindowsFiles
        io=WindowsFiles()
        with tempfile.TemporaryDirectory() as data:
            root=Path(data);target=root/'a.txt';target.write_text('old')
            other=root/'other.txt';other.write_text('other')
            with io.guard(target) as held:
                with self.assertRaises(OSError):target.write_text('user')
                with self.assertRaises(OSError):other.replace(target)
                held.move(root/'backup.txt')
                target.write_text('user-created-during-gap')
                with io.guard(other) as new:
                    with self.assertRaises(OSError):new.move(target)
            self.assertEqual(target.read_text(),'user-created-during-gap')

    async def test_actual_occupied_file_aborts_before_modification(self):
        from muse.windows_files import WindowsFiles
        with tempfile.TemporaryDirectory() as data:
            target=Path(data)/'project';target.mkdir();(target/'a.txt').write_text('old')
            tx=WindowsStagedWorkspace(target).begin()
            try:
                (tx.files/'a.txt').write_text('new')
                with WindowsFiles().guard(target/'a.txt'):
                    with self.assertRaises(OSError):tx.commit()
                self.assertEqual((target/'a.txt').read_text(),'old')
            finally:tx.release()

    async def test_actual_windows_crash_recovery_preserves_original_path(self):
        with tempfile.TemporaryDirectory() as data:
            target=Path(data)/'project';target.mkdir();(target/'a.txt').write_text('old')
            tx=WindowsStagedWorkspace(target,'crash').begin()
            (tx.files/'a.txt').write_text('new')
            rename=tx.io.rename
            def crash(held,destination):
                rename(held,destination)
                if destination==tx.target/'a.txt':raise KeyboardInterrupt()
            try:
                with patch.object(tx.io,'rename',side_effect=crash):
                    with self.assertRaises(KeyboardInterrupt):tx.commit()
            finally:tx.release()
            recovered=WindowsStagedWorkspace(target,'next').begin()
            try:
                self.assertEqual((target/'a.txt').read_text(),'old')
                self.assertEqual(json.loads(tx.journal.read_text())['state'],'rolled_back')
            finally:recovered.abandon();recovered.release()

    async def test_actual_ntfs_named_stream_is_rejected(self):
        with tempfile.TemporaryDirectory() as data:
            target=Path(data)/'project';target.mkdir();(target/'a.txt').write_text('old')
            Path(str(target/'a.txt')+':extra').write_text('must not silently disappear')
            with self.assertRaisesRegex(ValueError,'命名数据流'):WindowsStagedWorkspace(target).begin()
