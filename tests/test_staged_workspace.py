import asyncio
import json
import os
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from muse.staged_workspace import StagedWorkspace, active_workspace, _fingerprint
from muse.workspace import workspace
from muse.native_tools import NativeRuntime, apply_patch_text


class StagingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.parent=Path(self.temp.name).resolve()
        self.target=self.parent/'project';self.target.mkdir()
        (self.target/'a.txt').write_text('original\n')
        self.env=patch.dict(os.environ,{'MUSE_LOCAL_WORKSPACE':str(self.target),'MUSE_FILESYSTEM_MCP':'0','KIMI_API_KEY':'staging-test-only'})
        self.env.start()
        self.transaction=None
        self.runtime=NativeRuntime()
    async def asyncTearDown(self):
        await self.runtime.close()
        if self.transaction:self.transaction.release()
        self.env.stop();self.temp.cleanup()
    def begin(self,id=None):
        self.transaction=StagedWorkspace(self.target,id).begin()
        return self.transaction

    async def test_patches_only_stage_then_single_atomic_publish_and_duplicate_receipt(self):
        tx=self.begin('one')
        apply_patch_text('*** Begin Patch\n*** Update File: '+str(self.target/'a.txt')+'\n@@\n-original\n+first\n*** End Patch')
        apply_patch_text('*** Begin Patch\n*** Update File: a.txt\n@@\n-first\n+final\n*** Add File: b.txt\n+new\n*** End Patch')
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')
        self.assertFalse((self.target/'b.txt').exists())
        from muse.staged_workspace import _exchange
        with patch('muse.staged_workspace._exchange',wraps=_exchange) as exchange:
            receipt=tx.commit();again=tx.commit()
            self.assertEqual(exchange.call_count,1)
            self.assertEqual(receipt['commit_id'],again['commit_id'])
        self.assertEqual((self.target/'a.txt').read_text(),'final\n')
        self.assertEqual((self.target/'b.txt').read_text(),'new\n')
        self.assertEqual((tx.publication/'a.txt').read_text(),'original\n')

    async def test_real_terminal_cannot_write_target_or_outside_or_hardlink_escape(self):
        tx=self.begin()
        formal=shlex.quote(str(self.target/'a.txt'))
        outside=shlex.quote(str(self.parent/'escape.txt'))
        result=await self.runtime.exec_command('printf staged > a.txt')
        self.assertEqual(result['exit_code'],0)
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')
        for command in ['printf bad > '+formal,'printf bad > '+outside,
                        'ln '+formal+' hardlink && printf bad > hardlink',
                        'ln -s '+formal+' shortcut && printf bad > shortcut']:
            result=await self.runtime.exec_command(command)
            self.assertNotEqual(result['exit_code'],0,command)
            self.assertEqual((self.target/'a.txt').read_text(),'original\n')
        self.assertFalse((self.parent/'escape.txt').exists())

    async def test_conflict_refuses_to_overwrite_external_changes(self):
        tx=self.begin()
        (tx.files/'a.txt').write_text('agent')
        (self.target/'a.txt').write_text('external')
        with self.assertRaisesRegex(RuntimeError,'其他任务修改'):tx.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'external')
        self.assertEqual(json.loads(tx.journal.read_text())['state'],'conflict')

    async def test_aborted_task_never_publishes_and_pause_resumes_same_stage(self):
        tx=self.begin('paused')
        (tx.files/'a.txt').write_text('pending')
        tx.abandon(paused=True);tx.release()
        self.transaction=StagedWorkspace(self.target,'paused').begin()
        self.assertEqual((workspace()/'a.txt').read_text(),'pending')
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')
        self.transaction.abandon()
        with self.assertRaises(RuntimeError):self.transaction.commit()

    async def test_crash_after_exchange_recovers_without_second_exchange(self):
        tx=self.begin('crash')
        (tx.files/'a.txt').write_text('final')
        from muse.staged_workspace import _atomic_json
        def fail_receipt(path,meta):
            if meta.get('state')=='committed':raise OSError('simulated crash after exchange')
            _atomic_json(path,meta)
        with patch('muse.staged_workspace._atomic_json',side_effect=fail_receipt),self.assertRaises(OSError):tx.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'final')
        tx.release()
        self.transaction=StagedWorkspace(self.target,'next').begin()
        self.assertEqual(json.loads(tx.journal.read_text())['state'],'committed')
        self.assertEqual((workspace()/'a.txt').read_text(),'final')

    async def test_reject_new_symlinks_and_atomic_exchange_failure_no_partial_write(self):
        tx=self.begin()
        (tx.files/'newlink').symlink_to('/tmp')
        with self.assertRaises(ValueError):tx.commit()
        (tx.files/'newlink').unlink()
        (tx.files/'a.txt').write_text('new')
        (tx.files/'b.txt').write_text('second')
        with patch('muse.staged_workspace._exchange',side_effect=OSError('unsupported')),self.assertRaises(OSError):tx.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')
        self.assertFalse((self.target/'b.txt').exists())

    async def test_runner_success_publishes_once_failure_discards(self):
        from muse.agent import run_agent
        from muse.models import get_provider
        async def fake_run(agent,**kwargs):
            apply_patch_text('*** Begin Patch\n*** Update File: a.txt\n@@\n-original\n+finished\n*** End Patch')
            self.assertEqual((self.target/'a.txt').read_text(),'original\n')
            return SimpleNamespace(final_output='done',to_input_list=lambda:[])
        with patch('muse.agent.Runner.run',side_effect=fake_run):
            answer,_=await run_agent(None,'edit',[],lambda *args:None,provider=get_provider('kimi'))
        self.assertEqual(answer,'done')
        self.assertEqual((self.target/'a.txt').read_text(),'finished\n')
        async def failed_run(agent,**kwargs):
            (workspace()/'a.txt').write_text('must not publish')
            raise RuntimeError('runner failed')
        with patch('muse.agent.Runner.run',side_effect=failed_run),self.assertRaisesRegex(RuntimeError,'runner failed'):
            await run_agent(None,'edit',[],lambda *args:None,provider=get_provider('kimi'))
        self.assertEqual((self.target/'a.txt').read_text(),'finished\n')
        self.assertIsNone(active_workspace.get())

    async def test_open_staging_handle_cannot_change_published_inode(self):
        tx=self.begin()
        with (tx.files/'a.txt').open('r+') as held:
            held.seek(0);held.write('final\n');held.truncate();held.flush()
            tx.commit()
            held.seek(0);held.write('late writer\n');held.truncate();held.flush()
        self.assertEqual((self.target/'a.txt').read_text(),'final\n')
        self.assertEqual((tx.files/'a.txt').read_text(),'late writer\n')

    async def test_no_changes_does_not_replace_target(self):
        tx=self.begin()
        with patch('muse.staged_workspace._exchange') as exchange:
            result=tx.commit()
        exchange.assert_not_called()
        self.assertFalse(result['changed'])

    async def test_simultaneous_snapshots_conflict_instead_of_lost_update(self):
        first=self.begin('first')
        (first.files/'a.txt').write_text('first')
        second=StagedWorkspace(self.target,'second').begin()
        try:
            (second.files/'a.txt').write_text('second')
            first.commit()
            with self.assertRaisesRegex(RuntimeError,'其他任务修改'):second.commit()
            self.assertEqual((self.target/'a.txt').read_text(),'first')
        finally:second.release()

    async def test_terminal_cannot_modify_journal_or_use_network(self):
        tx=self.begin()
        result=await self.runtime.exec_command('printf bad > '+shlex.quote(str(tx.journal)))
        self.assertNotEqual(result['exit_code'],0)
        self.assertEqual(json.loads(tx.journal.read_text())['state'],'staged')
        import sys
        async def handle(reader,writer):
            writer.close();await writer.wait_closed()
        server=await asyncio.start_server(handle,'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        try:
            command=shlex.quote(sys.executable)+' -c '+shlex.quote('import socket; socket.create_connection(("127.0.0.1", '+str(port)+'), timeout=1).close()')
            result=await self.runtime.exec_command(command)
            self.assertNotEqual(result['exit_code'],0)
            with patch.dict(os.environ,{'MUSE_URL':'http://127.0.0.1:'+str(port)}):
                result=await self.runtime.exec_command(command)
            self.assertEqual(result['exit_code'],0,result['stderr'])
        finally:
            server.close();await server.wait_closed()

    async def test_real_filesystem_mcp_only_writes_stage(self):
        from muse.filesystem_mcp import Filesystem, ROOT
        entry=ROOT/'vendor/filesystem/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js'
        if not entry.exists():self.skipTest('Filesystem MCP not installed')
        tx=self.begin()
        with patch.dict(os.environ,{'MUSE_FILESYSTEM_MCP':'1'}):
            fs=Filesystem()
            async with fs.session(lambda *args:None):
                self.assertTrue(fs.status['ready'])
                await fs.server.call_tool('write_file',{'path':str(tx.files/'a.txt'),'content':'MCP staged'})
                self.assertEqual((self.target/'a.txt').read_text(),'original\n')
                result=await fs.server.call_tool('write_file',{'path':str(self.target/'a.txt'),'content':'bypass'})
                self.assertTrue(getattr(result,'is_error',getattr(result,'isError',False)))
            tx.commit()
            self.assertEqual((self.target/'a.txt').read_text(),'MCP staged')

    async def test_cancellation_keeps_formal_unchanged_and_kills_commands(self):
        from muse.agent import run_agent
        from muse.models import get_provider
        entered=asyncio.Event();captured={}
        async def waiting(agent,**kwargs):
            (workspace()/'a.txt').write_text('unfinished')
            captured['journal']=active_workspace.get().journal
            runtime=agent._muse_native_runtime
            result=await runtime.exec_command('sleep 30',yield_time_ms=1)
            captured['process']=runtime.sessions[result['session_id']].process
            entered.set()
            await asyncio.Event().wait()
        with patch('muse.agent.Runner.run',side_effect=waiting):
            task=asyncio.create_task(run_agent(None,'edit',[],lambda *args:None,provider=get_provider('kimi')))
            await asyncio.wait_for(entered.wait(),3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')
        self.assertIsNotNone(captured['process'].returncode)
        self.assertEqual(json.loads(captured['journal'].read_text())['state'],'aborted')

    async def test_existing_muse_cli_can_only_reach_configured_host_api(self):
        import sys
        async def respond(reader,writer):
            await reader.readuntil(b'\r\n\r\n')
            body=b'{"servers":[]}'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
            await writer.drain();writer.close();await writer.wait_closed()
        server=await asyncio.start_server(respond,'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        tx=self.begin()
        try:
            with patch.dict(os.environ,{'MUSE_URL':'http://127.0.0.1:'+str(port)}):
                result=await self.runtime.exec_command(shlex.quote(sys.executable)+' -m muse.cli mcp list')
                while result['session_id']:
                    result=await self.runtime.write_stdin(result['session_id'],yield_time_ms=1000)
            self.assertEqual(result['exit_code'],0,result['stderr'])
            self.assertEqual(json.loads(result['stdout']),{'servers':[]})
        finally:server.close();await server.wait_closed()

    async def test_partial_snapshot_failure_can_retry_same_task(self):
        from muse.staged_workspace import _copy_tree
        original=_copy_tree
        def fail_copy(source,destination,**kwargs):
            Path(destination).mkdir()
            (Path(destination)/'partial.txt').write_text('incomplete')
            raise OSError('fixture disk failure')
        failed=StagedWorkspace(self.target,'copy-retry')
        with patch('muse.staged_workspace._copy_tree',side_effect=fail_copy),self.assertRaises(OSError):failed.begin()
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')
        self.transaction=StagedWorkspace(self.target,'copy-retry').begin()
        self.assertFalse((workspace()/'partial.txt').exists())
        self.assertEqual((workspace()/'a.txt').read_text(),'original\n')

    async def test_crashed_staged_task_can_resume_without_recopying(self):
        tx=self.begin('crashed-before-commit')
        (tx.files/'a.txt').write_text('recover me')
        tx.release()  # Simulated process exit: no abandon/paused receipt.
        self.transaction=StagedWorkspace(self.target,'crashed-before-commit').begin()
        self.assertEqual((workspace()/'a.txt').read_text(),'recover me')
        self.transaction.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'recover me')

    async def test_released_host_handle_cannot_commit_or_abort_resumed_task(self):
        tx=self.begin('lease')
        (tx.files/'a.txt').write_text('pending')
        tx.abandon(paused=True);tx.release()
        self.transaction=StagedWorkspace(self.target,'lease').begin()
        with self.assertRaises(RuntimeError):tx.commit()
        tx.meta['state']='staged'
        with self.assertRaises(RuntimeError):tx.abandon()
        self.assertEqual(json.loads(tx.journal.read_text())['state'],'staged')
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')

    async def test_wrong_target_or_commit_identity_in_journal_is_rejected(self):
        tx=self.begin('identity')
        tx.abandon(paused=True);tx.release()
        meta=json.loads(tx.journal.read_text());meta['target']=str(self.parent/'other-target')
        tx.journal.write_text(json.dumps(meta))
        with self.assertRaisesRegex(RuntimeError,'事务记录'):
            StagedWorkspace(self.target,'identity').begin()
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')

    async def test_agent_tools_without_harness_fail_closed(self):
        from muse.agent import make_agent
        from muse.models import get_provider
        from agents.tool_context import ToolContext
        agent,client=make_agent(None,lambda *args:None,get_provider('kimi'))
        ctx=ToolContext(context=None,tool_name='exec_command',tool_call_id='without-harness',tool_arguments='{}')
        tools={t.name:t for t in agent.tools}
        try:
            # Tool errors may be converted to text by the SDK; observe actual effects.
            await tools['exec_command'].on_invoke_tool(ctx,json.dumps({'command':'printf bypass > bypass.txt'}))
            await tools['apply_patch'].on_invoke_tool(ctx,json.dumps({'patch':'*** Begin Patch\n*** Add File: bypass-patch.txt\n+bad\n*** End Patch'}))
            self.assertFalse((self.target/'bypass.txt').exists())
            self.assertFalse((self.target/'bypass-patch.txt').exists())
        finally:await agent._muse_native_runtime.close();await client.close()

    async def test_closed_agent_patch_tool_cannot_modify_stage(self):
        from muse.agent import make_agent
        from muse.models import get_provider
        from agents.tool_context import ToolContext
        tx=self.begin()
        agent,client=make_agent(None,lambda *args:None,get_provider('kimi'))
        try:
            tools={t.name:t for t in agent.tools}
            await agent._muse_native_runtime.close()
            ctx=ToolContext(context=None,tool_name='apply_patch',tool_call_id='closed-patch',tool_arguments='{}')
            await tools['apply_patch'].on_invoke_tool(ctx,json.dumps({'patch':'*** Begin Patch\n*** Add File: late.txt\n+bad\n*** End Patch'}))
            self.assertFalse((tx.files/'late.txt').exists())
        finally:await client.close()

    async def test_add_delete_rename_binary_empty_directory_and_executable_mode(self):
        tx=self.begin()
        (tx.files/'a.txt').rename(tx.files/'renamed.txt')
        (tx.files/'empty').mkdir()
        (tx.files/'binary.dat').write_bytes(bytes(range(256)))
        executable=tx.files/'tool.sh';executable.write_text('#!/bin/sh\nprintf hello\n');executable.chmod(0o755)
        self.assertTrue((self.target/'a.txt').exists())
        self.assertFalse((self.target/'binary.dat').exists())
        tx.commit()
        self.assertFalse((self.target/'a.txt').exists())
        self.assertEqual((self.target/'renamed.txt').read_text(),'original\n')
        self.assertTrue((self.target/'empty').is_dir())
        self.assertEqual((self.target/'binary.dat').read_bytes(),bytes(range(256)))
        self.assertEqual((self.target/'tool.sh').stat().st_mode&0o777,0o755)

    async def test_paused_task_rejects_changed_target_without_destroying_stage(self):
        tx=self.begin('pause-conflict')
        (tx.files/'a.txt').write_text('pending')
        tx.abandon(paused=True);tx.release()
        (self.target/'a.txt').write_text('external')
        with self.assertRaisesRegex(RuntimeError,'工作区已变化'):
            StagedWorkspace(self.target,'pause-conflict').begin()
        self.assertEqual((tx.files/'a.txt').read_text(),'pending')
        self.assertEqual((self.target/'a.txt').read_text(),'external')

    async def test_task_lock_and_repeated_begin_do_not_release_owner(self):
        tx=self.begin('locked')
        with self.assertRaises(RuntimeError):tx.begin()
        with self.assertRaises(RuntimeError):StagedWorkspace(self.target,'locked').begin()
        self.assertIs(active_workspace.get(),tx)
        (tx.files/'a.txt').write_text('owner')
        tx.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'owner')

    async def test_disk_full_final_copy_and_fsync_leave_target_untouched(self):
        tx=self.begin()
        (tx.files/'a.txt').write_text('final')
        from muse.staged_workspace import _copy_tree
        original=_copy_tree
        def disk_full(source,destination,**kwargs):
            Path(destination).mkdir();(Path(destination)/'partial').write_text('partial')
            raise OSError(28,'fixture disk full')
        with patch('muse.staged_workspace._copy_tree',side_effect=disk_full),self.assertRaises(OSError):tx.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')
        with patch.object(tx,'_flush_files',side_effect=OSError('fixture fsync')),self.assertRaises(OSError):tx.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')
        self.assertEqual(json.loads(tx.journal.read_text())['state'],'staged')
        tx.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'final')

    async def test_final_snapshot_content_mismatch_is_not_published(self):
        tx=self.begin();(tx.files/'a.txt').write_text('final')
        from muse.staged_workspace import _copy_tree
        original=_copy_tree
        def changed_copy(source,destination,**kwargs):
            result=original(source,destination,**kwargs)
            (Path(destination)/'a.txt').write_text('raced')
            return result
        with patch('muse.staged_workspace._copy_tree',side_effect=changed_copy),self.assertRaisesRegex(RuntimeError,'最终版本期间发生变化'):tx.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')

    async def test_external_change_during_flush_is_detected_before_exchange(self):
        tx=self.begin();(tx.files/'a.txt').write_text('final')
        def outside_writer():(self.target/'a.txt').write_text('outside during flush')
        with patch.object(tx,'_flush_files',side_effect=outside_writer),patch('muse.staged_workspace._exchange') as exchange:
            with self.assertRaisesRegex(RuntimeError,'其他任务修改'):tx.commit()
            exchange.assert_not_called()
        self.assertEqual((self.target/'a.txt').read_text(),'outside during flush')

    async def test_prepared_before_exchange_resumes_only_on_explicit_task_reentry(self):
        tx=self.begin('before-exchange');(tx.files/'a.txt').write_text('final')
        with patch('muse.staged_workspace._exchange',side_effect=OSError('fixture crash')),self.assertRaises(OSError):tx.commit()
        tx.release()
        other=StagedWorkspace(self.target,'other').begin()
        try:
            self.assertEqual(json.loads(tx.journal.read_text())['state'],'paused')
            self.assertEqual((self.target/'a.txt').read_text(),'original\n')
            other.abandon()
        finally:other.release()
        self.transaction=StagedWorkspace(self.target,'before-exchange').begin()
        self.transaction.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'final')

    async def test_ambiguous_prepared_recovery_blocks_without_overwrite(self):
        tx=self.begin('ambiguous');(tx.files/'a.txt').write_text('final')
        with patch('muse.staged_workspace._exchange',side_effect=OSError('fixture crash')),self.assertRaises(OSError):tx.commit()
        tx.release();(self.target/'a.txt').write_text('third version')
        with self.assertRaisesRegex(RuntimeError,'冲突'):StagedWorkspace(self.target,'new').begin()
        self.assertEqual((self.target/'a.txt').read_text(),'third version')
        self.assertEqual(json.loads(tx.journal.read_text())['state'],'prepared')

    async def test_committed_receipt_survives_cleanup_and_cannot_reexecute_task(self):
        tx=self.begin('once');(tx.files/'a.txt').write_text('final')
        tx.commit();tx.cleanup_committed();tx.release()
        with patch('muse.staged_workspace._exchange') as exchange:
            self.assertEqual(tx.commit()['state'],'committed')
            with self.assertRaisesRegex(RuntimeError,'已经结束'):StagedWorkspace(self.target,'once').begin()
            exchange.assert_not_called()
        self.assertFalse(tx.files.exists())
        self.assertEqual((self.target/'a.txt').read_text(),'final')

    async def test_invalid_journal_and_symlink_journal_fail_closed(self):
        tx=self.begin('bad-journal');tx.abandon(paused=True);tx.release()
        tx.journal.write_text('{broken')
        with self.assertRaisesRegex(RuntimeError,'事务记录'):StagedWorkspace(self.target,'new').begin()
        tx.journal.unlink();outside=self.parent/'outside.json';outside.write_text('{}')
        tx.journal.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError,'事务记录'):StagedWorkspace(self.target,'new').begin()
        self.assertEqual(outside.read_text(),'{}')
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')

    async def test_no_sandbox_backend_never_starts_unsandboxed_command(self):
        self.begin()
        with patch('muse.staged_workspace.platform.system',return_value='Linux'),patch('muse.staged_workspace.shutil.which',return_value=None),patch('muse.native_tools.asyncio.create_subprocess_exec') as start:
            with self.assertRaisesRegex(RuntimeError,'缺少写入隔离能力'):
                await self.runtime.exec_command('printf bypass > a.txt')
            start.assert_not_called()
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')

    async def test_external_file_allowlist_cannot_expand_staged_write_scope(self):
        self.begin();outside=self.parent/'allowed.txt';outside.write_text('outside')
        with patch.dict(os.environ,{'MUSE_LOCAL_ALLOWED_FILES':str(outside)}),self.assertRaises(ValueError):
            apply_patch_text('*** Begin Patch\n*** Update File: '+str(outside)+'\n@@\n-outside\n+bad\n*** End Patch')
        self.assertEqual(outside.read_text(),'outside')

    async def test_agent_runtime_cannot_cross_task_context(self):
        first=self.begin('owner');runtime=NativeRuntime(require_workspace=True)
        second=StagedWorkspace(self.target,'other').begin()
        try:
            with self.assertRaises(PermissionError):await runtime.exec_command('printf wrong > a.txt')
            self.assertEqual((first.files/'a.txt').read_text(),'original\n')
            self.assertEqual((second.files/'a.txt').read_text(),'original\n')
        finally:second.abandon();second.release()
        try:
            result=await runtime.exec_command('printf owner > a.txt')
            self.assertEqual(result['exit_code'],0)
            self.assertEqual((first.files/'a.txt').read_text(),'owner')
        finally:await runtime.close()

    async def test_source_hardlink_and_fifo_are_rejected_before_agent(self):
        os.link(self.target/'a.txt',self.target/'hardlink')
        with self.assertRaisesRegex(ValueError,'硬链接'):StagedWorkspace(self.target,'unsupported').begin()
        (self.target/'hardlink').unlink();os.mkfifo(self.target/'pipe')
        with self.assertRaisesRegex(ValueError,'特殊文件'):StagedWorkspace(self.target,'unsupported').begin()
        (self.target/'pipe').unlink()
        self.transaction=StagedWorkspace(self.target,'unsupported').begin()
        self.assertEqual((workspace()/'a.txt').read_text(),'original\n')

    def set_attribute(self,path,key,value):
        import sys,subprocess
        if sys.platform=='darwin':
            subprocess.run(['/usr/bin/xattr','-w',key,value,str(path)],check=True,capture_output=True)
        elif hasattr(os,'setxattr'):os.setxattr(path,key,value.encode())
        else:self.skipTest('xattrs unavailable')

    def get_attribute(self,path,key):
        import sys,subprocess
        if sys.platform=='darwin':return subprocess.check_output(['/usr/bin/xattr','-p',key,str(path)]).decode().rstrip('\n')
        return os.getxattr(path,key).decode()

    async def test_metadata_only_extended_attribute_change_is_published(self):
        key='user.muse-staging-fixture'
        self.set_attribute(self.target/'a.txt',key,'baseline')
        tx=self.begin();self.set_attribute(tx.files/'a.txt',key,'final')
        result=tx.commit()
        self.assertTrue(result['changed'])
        self.assertEqual(self.get_attribute(self.target/'a.txt',key),'final')
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')

    async def test_external_metadata_only_change_is_conflict(self):
        key='user.muse-staging-fixture'
        self.set_attribute(self.target/'a.txt',key,'baseline')
        tx=self.begin();(tx.files/'a.txt').write_text('final')
        self.set_attribute(self.target/'a.txt',key,'outside')
        with self.assertRaisesRegex(RuntimeError,'其他任务修改'):tx.commit()
        self.assertEqual(self.get_attribute(self.target/'a.txt',key),'outside')
        self.assertEqual((self.target/'a.txt').read_text(),'original\n')

    async def test_root_setgid_mode_is_rejected(self):
        tx=self.begin();tx.files.chmod(0o2755)
        try:
            with self.assertRaises(ValueError):tx.commit()
        finally:tx.files.chmod(0o755)

    async def test_acl_only_change_is_published_on_macos(self):
        import sys,subprocess
        if sys.platform!='darwin':self.skipTest('macOS ACL integration')
        tx=self.begin()
        subprocess.run(['/bin/chmod','+a','everyone allow read',str(tx.files/'a.txt')],check=True,capture_output=True)
        staged=_fingerprint(tx.files)
        self.assertTrue(tx.commit()['changed'])
        self.assertEqual(_fingerprint(self.target),staged)

    async def test_legacy_fingerprint_receipt_can_resume(self):
        tx=self.begin('legacy-fingerprint')
        tx.abandon(paused=True);tx.release()
        meta=json.loads(tx.journal.read_text())
        meta.pop('fingerprint_version',None)
        meta['baseline']=_fingerprint(self.target,version=1)
        tx.journal.write_text(json.dumps(meta))
        self.transaction=StagedWorkspace(self.target,'legacy-fingerprint').begin()
        (workspace()/'a.txt').write_text('legacy final')
        self.transaction.commit()
        self.assertEqual((self.target/'a.txt').read_text(),'legacy final')

    async def test_wrong_commit_id_in_journal_is_rejected(self):
        tx=self.begin('commit-identity')
        tx.abandon(paused=True);tx.release()
        meta=json.loads(tx.journal.read_text());meta['commit_id']='0'*32
        tx.journal.write_text(json.dumps(meta))
        with self.assertRaisesRegex(RuntimeError,'事务记录'):
            StagedWorkspace(self.target,'commit-identity').begin()
