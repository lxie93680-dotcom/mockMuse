"""Additional independent boundary cases, executed against the product registry."""
import asyncio
import copy
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

from agents import function_tool
from muse.delegation import DENIED_TOOLS,select_child_tools
from muse.tool_access import ToolAccess
from muse.tool_enable_cache import ENABLED_TTL_SECONDS
from muse.tool_failures import ToolCallFailure,ToolBusinessError
from muse.tool_providers import MCPProvider
from muse.tool_store import definition_fingerprint
from .fixtures import EvalMCP


def probe(name,description):
    @function_tool(name_override=name,description_override=description,strict_mode=False)
    async def invoke(query: str = '') -> str:
        return query or 'fixture'
    return invoke


async def exercise(case,root,registry,server,access,trace,talk,check):
    # Imported lazily to keep the runner/extension dependency acyclic.
    from .run import refreshed,context,require,searched
    kind=case['id'];primary=MCPProvider(server).source_id
    extras=[]
    async with AsyncExitStack() as stack:
        async def peer(same_source=False):
            class Peer(EvalMCP):
                @property
                def name(self):return 'Tool Lifecycle Evaluation Peer'
            other=(EvalMCP if same_source else Peer)(root)
            await stack.enter_async_context(other.session())
            provider=MCPProvider(other)
            if provider.source_id!=primary:extras.append(provider.source_id)
            return other,provider
        async def expect(error,action,label):
            try:await action()
            except error:check(True,label)
            else:raise AssertionError(label)
        def row(name='eval_lookup'):
            return next(r for r in registry.store.list_tools(primary) if r['name']==name)
        async def call(tid,args=None):
            info=access.describe(tid)
            return await access.call(context(),tid,args or {},info['fingerprint'])
        try:
            if kind=='delete_empty_snapshot':
                ids=[r['tool_id'] for r in registry.store.list_tools(primary)]
                server.functions={};await refreshed(registry,server)
                check(not access.search('browser') and not access.search('eval_lookup'),'empty_snapshot_search_empty')
                check(all(registry.store.get(tid)['availability']=='missing' for tid in ids),'all_tools_marked_missing')

            elif kind=='delete_disabled_restore':
                tid=row()['tool_id'];registry.set_enabled(tid,False)
                tool=server.functions.pop('eval_lookup');await refreshed(registry,server)
                server.functions['eval_lookup']=tool;await refreshed(registry,server)
                check(row()['tool_id']==tid and not registry.enabled_states()[tid],'disabled_identity_survives_remove_restore')
                check(not access.search('eval_lookup'),'restoration_does_not_enable_tool')

            elif kind=='delete_three_cycles':
                tid=row()['tool_id'];tool=server.functions['eval_lookup']
                for cycle in range(3):
                    server.functions.pop('eval_lookup');await refreshed(registry,server)
                    check(not access.search('eval_lookup'),'removed_cycle_'+str(cycle))
                    server.functions['eval_lookup']=tool;await refreshed(registry,server)
                    check(access.search('eval_lookup')[0]['tool_id']==tid,'restored_id_cycle_'+str(cycle))

            elif kind=='disable_all':
                for r in registry.store.list_tools(primary):registry.set_enabled(r['tool_id'],False)
                check(not access.search('browser') and not access.search('eval'),'all_disabled_queries_empty')
                check(len(registry.store.list_tools(primary))==len(server.functions),'all_disabled_management_rows_preserved')

            elif kind=='disable_before_limit':
                for i in range(12):
                    server.functions['limit_probe_'+str(i)]=probe('limit_probe_'+str(i),'limit_fixture cohort')
                await refreshed(registry,server)
                cohort=[r for r in registry.store.list_tools(primary) if r['name'].startswith('limit_probe_')]
                keep=cohort[-1]['tool_id']
                for r in cohort[:-1]:registry.set_enabled(r['tool_id'],False)
                check([r['tool_id'] for r in access.search('limit_fixture',1)]==[keep],'disabled_filter_precedes_limit')

            elif kind=='disable_toggle_loop':
                tid=row()['tool_id'];registry.enabled_states();loaded=registry.enabled_cache.loaded_at
                for flag in (False,True,False,True,False,True):
                    registry.set_enabled(tid,flag)
                    check(bool(access.search('eval_lookup'))==flag,'toggle_visible_'+str(flag))
                check(registry.enabled_cache.loaded_at==loaded,'toggle_loop_does_not_extend_ttl')

            elif kind=='cache_ttl_refresh':
                now=[100.0];registry.enabled_cache.clock=lambda:now[0]
                tid=row()['tool_id'];registry.enabled_states()
                registry.store.set_enabled(tid,False)
                now[0]+=ENABLED_TTL_SECONDS-1
                check(bool(access.search('eval_lookup')),'external_db_change_not_seen_before_ttl')
                now[0]+=1
                check(not access.search('eval_lookup'),'expired_cache_reloads_database')

            elif kind=='cache_write_failure':
                tid=row()['tool_id'];registry.enabled_states()
                with patch.object(registry.store,'set_enabled',side_effect=OSError('injected')):
                    try:registry.set_enabled(tid,False)
                    except OSError:pass
                    else:raise AssertionError('Injected write did not fail')
                check(registry.enabled_states()[tid] and row()['enabled'],'failed_write_keeps_cache_and_database')

            elif kind=='cache_unchanged_registration':
                tid=row()['tool_id'];registry.set_enabled(tid,False);loaded=registry.enabled_cache.loaded_at
                fp=access.registry.store.get(tid)['fingerprint']
                result=await registry.register(MCPProvider(server))
                check(result['changed']==0,'unchanged_registration_no_definition_change')
                check(not registry.enabled_states()[tid] and registry.enabled_cache.loaded_at==loaded,'unchanged_registration_preserves_cached_choice')
                check(registry.store.get(tid)['fingerprint']==fp,'unchanged_registration_preserves_hash')

            elif kind in ('schema_add_optional','schema_remove_parameter','description_only_refresh'):
                tid=row()['tool_id'];before=access.describe(tid);tool=server.functions['eval_lookup']
                if kind=='schema_add_optional':
                    tool.params_json_schema['properties']['locale']={'type':'string','default':'zh'}
                elif kind=='schema_remove_parameter':
                    server.install_lookup(2);await refreshed(registry,server)
                    before=access.describe(tid);server.install_lookup(1)
                else:tool.description+=' DESCRIPTION_ONLY_LATEST'
                await refreshed(registry,server);after=access.describe(tid)
                check(after['fingerprint']!=before['fingerprint'],'definition_change_updates_fingerprint')
                if kind=='schema_add_optional':
                    check('locale' in after['parameters']['properties'] and 'locale' not in after['parameters'].get('required',[]),'optional_parameter_is_not_required')
                    await call(tid,{'query':'optional'})
                elif kind=='schema_remove_parameter':
                    check('revision' not in after['parameters']['properties'],'removed_parameter_not_disclosed')
                    await call(tid,{'query':'removed'})
                else:
                    check(after['parameters']==before['parameters'] and bool(access.search('DESCRIPTION_ONLY_LATEST')),'description_change_updates_index_without_schema_change')
                if talk:
                    evidence=await talk.ask('仅查询工具目录：请重新实际描述 eval_lookup 的当前完整参数，不执行，不使用浏览器。')
                    values=[o['value'] for o in evidence['outputs'] if o['tool']=='tool_describe']
                    check(any(v['fingerprint']==after['fingerprint'] for v in values),'dialogue_describes_current_fingerprint')

            elif kind in ('notification_burst','notification_during_refresh','refresh_cancel_preserves_snapshot'):
                tid=row()['tool_id'];before=registry.store.get(tid)
                if kind=='notification_burst':
                    server.functions['eval_lookup'].description+=' NOTIFY_FINAL'
                    with patch.object(registry,'refresh',wraps=registry.refresh) as refresh:
                        for _ in range(10):server.emit_catalog_event('tools_changed')
                        await registry.wait_for_refresh()
                        check(refresh.await_count==1,'burst_coalesced_to_one_refresh')
                    check(bool(access.search('NOTIFY_FINAL')),'burst_final_description_indexed')
                elif kind=='notification_during_refresh':
                    original=server.list_tools;calls=[0]
                    async def listing(*args,**kwargs):
                        calls[0]+=1
                        server.functions['eval_lookup'].description='INFLIGHT_VERSION_'+str(calls[0])
                        if calls[0]==1:server.emit_catalog_event('tools_changed')
                        return await original(*args,**kwargs)
                    with patch.object(server,'list_tools',side_effect=listing):
                        server.emit_catalog_event('tools_changed')
                        await registry.wait_for_refresh()
                    check(calls[0]==2,'notification_during_refresh_replayed_once')
                    check(bool(access.search('INFLIGHT_VERSION_2')) and not access.search('INFLIGHT_VERSION_1'),'latest_notification_snapshot_wins')
                else:
                    entered=asyncio.Event()
                    async def blocked(*args,**kwargs):
                        entered.set();await asyncio.Event().wait()
                    with patch.object(server,'list_tools',side_effect=blocked):
                        task=asyncio.create_task(registry.refresh(primary))
                        await entered.wait();task.cancel()
                        try:await task
                        except asyncio.CancelledError:check(True,'refresh_cancelled')
                        else:raise AssertionError('Refresh did not cancel')
                    check(registry.store.get(tid)==before,'cancelled_discovery_no_commit')
                    check(bool(access.search('eval_lookup')),'cancelled_discovery_keeps_searchable_definition')

            elif kind.startswith('search_'):
                fixtures={
                    'search_short_chinese':('search_probe','查天气 旅游 出行','天气',True),
                    'search_short_ascii':('search_probe','id lookup','id',True),
                    'search_literal_quotes':('search_probe','literal alpha "beta" label','"beta"',True),
                    'search_multikeyword':('search_probe','crimson habitat marker','crimson habitat',True),
                    'search_no_match':('search_probe','generic fixture','not_registered_985412',False),
                    'search_casefold':('MixedCaseProbe','MIXEDCASE label','mixedcaseprobe',True),
                    'search_limit':('search_probe','bounded_marker','bounded_marker',True)}
                name,description,query,present=fixtures[kind]
                server.functions[name]=probe(name,description)
                if kind=='search_limit':
                    for i in range(7):server.functions['bounded_'+str(i)]=probe('bounded_'+str(i),'bounded_marker')
                if kind=='search_multikeyword':
                    server.functions['distractor']=probe('distractor','crimson only')
                await refreshed(registry,server)
                effect_count=len(server.effects)
                found=access.search(query,3)
                check(bool(found)==present,'literal_query_expected_presence')
                if present and kind!='search_limit':check(any(r['name']==name for r in found),'correct_literal_candidate')
                if kind=='search_multikeyword':check(all(r['name']!='distractor' for r in found),'multiword_and_excludes_partial_match')
                if kind=='search_limit':check(len(found)==3,'result_limit_respected')
                if kind=='search_no_match':
                    check(effect_count==len(server.effects),'no_effect_from_search')

            elif kind.startswith('source_'):
                other,provider=await peer(kind!='source_same_name')
                tid=row()['tool_id'];old=access.describe(tid)
                if kind=='source_same_name':
                    await registry.register(provider)
                    found=access.search('eval_lookup')
                    check(len(found)==2 and len({r['tool_id'] for r in found})==2,'same_name_distinct_ids')
                    registry.set_enabled(tid,False)
                    found=access.search('eval_lookup')
                    check(len(found)==1 and found[0]['source_id']==provider.source_id,'disable_one_source_keeps_other')
                    await call(found[0]['tool_id'],{'query':'peer'})
                    check(len(other.effects)==1 and not server.effects,'execution_routed_to_exact_source')
                elif kind=='source_failed_replacement':
                    with patch.object(other,'list_tools',AsyncMock(side_effect=ConnectionError('injected'))):
                        await expect(ConnectionError,lambda:registry.register(provider),'replacement_failure_reported')
                    registry.set_enabled(tid,False)
                    await expect(PermissionError,lambda:server.call_tool('eval_lookup',{'query':'denied'}),'old_connection_guard_survives_failed_replace')
                else:
                    await registry.register(provider)
                    await expect(PermissionError,lambda:server.call_tool('eval_lookup',{'query':'retired'}),'retired_connection_rejected')
                    await call(tid,{'query':'replacement'})
                    check(bool(other.effects) and not server.effects,'replacement_executes_new_connection')

            elif kind.startswith('fingerprint_'):
                tid=row()['tool_id'];before=access.describe(tid);tool=server.functions['eval_lookup']
                if kind=='fingerprint_keyorder':
                    schema=tool.params_json_schema;tool.params_json_schema=dict(reversed(list(schema.items())))
                    await refreshed(registry,server)
                    check(access.describe(tid)['fingerprint']==before['fingerprint'],'object_key_order_hash_stable')
                elif kind=='fingerprint_enabled_independent':
                    registry.set_enabled(tid,False)
                    check(registry.store.get(tid)['fingerprint']==before['fingerprint'],'disabled_not_part_of_hash')
                    registry.set_enabled(tid,True)
                    check(access.describe(tid)['fingerprint']==before['fingerprint'],'reenabled_hash_stable')
                else:
                    tool.params_json_schema['properties']['query']['enum']=['alpha','beta']
                    await refreshed(registry,server);first=access.describe(tid)
                    tool.params_json_schema['properties']['query']['enum'].reverse()
                    await refreshed(registry,server)
                    check(access.describe(tid)['fingerprint']!=first['fingerprint'],'array_order_hash_sensitive')

            elif kind.startswith('fault_'):
                tid=row()['tool_id'];info=access.describe(tid)
                if kind in ('fault_connection_refresh','fault_business_no_refresh'):
                    failure=ConnectionError('injected') if kind=='fault_connection_refresh' else ToolBusinessError('fixture business error')
                    with patch.object(server,'call_tool',AsyncMock(side_effect=failure)) as execute,patch.object(registry,'rediscover_on_failure',wraps=registry.rediscover_on_failure) as discover:
                        try:await access.call(context(),tid,{'query':'failure'},info['fingerprint'])
                        except ToolCallFailure as error:
                            check(error.details['category']==('connection' if kind=='fault_connection_refresh' else 'business'),'typed_failure_classified')
                            check(error.details['business_call_retried'] is False,'business_not_replayed')
                        else:raise AssertionError('Failure not reported')
                        check(execute.await_count==1,'exactly_one_business_attempt')
                        check(discover.await_count==(1 if kind=='fault_connection_refresh' else 0),'discovery_only_for_transport_failure')
                elif kind=='fault_concurrent_coalesce':
                    with patch.object(registry,'refresh',wraps=registry.refresh) as refresh:
                        statuses=await asyncio.gather(*(registry.rediscover_on_failure(primary) for _ in range(12)))
                        check(statuses==['refreshed']*12 and refresh.await_count==1,'concurrent_failures_one_discovery')
                else:
                    before=registry.store.get(tid)
                    with patch.object(server,'list_tools',AsyncMock(side_effect=ConnectionError('injected'))):
                        check(await registry.rediscover_on_failure(primary)=='failed','failed_discovery_explicit')
                    check(registry.store.get(tid)==before,'failed_refresh_preserves_full_definition')
                    check(bool(access.search('eval_lookup')),'failed_refresh_keeps_old_index')

            elif kind.startswith('delegation_'):
                parent=access.tools();child=access.child(DENIED_TOOLS)
                tid=row()['tool_id'];browser=row('browser_action')['tool_id']
                if kind=='delegation_denied_action':
                    check(not child.search('browser_action'),'child_cannot_search_mixed_browser_writer')
                    try:child.describe(browser)
                    except PermissionError:check(True,'child_cannot_describe_parent_writer')
                    else:raise AssertionError('Child described forbidden tool')
                    try:select_child_tools(parent,['tool_call'])
                    except ValueError:check(True,'cannot_inherit_parent_generic_caller')
                    else:raise AssertionError('Parent generic bridge inherited')
                elif kind=='delegation_explicit_subset':
                    selected=select_child_tools(parent,[tid])[0]._muse_tool_access
                    check(bool(selected.search('eval_lookup')) and not selected.search('read_browser'),'explicit_subset_filters_search')
                    try:selected.describe(row('read_browser')['tool_id'])
                    except PermissionError:check(True,'explicit_subset_rejects_other_id')
                    else:raise AssertionError('Subset escaped')
                    check(not select_child_tools(parent,[]),'empty_selection_has_no_tools')
                else:
                    info=child.describe(tid);registry.set_enabled(tid,False)
                    check(not child.search('eval_lookup'),'child_search_updates_after_toggle')
                    await expect(PermissionError,lambda:child.call(context(),tid,{'query':'denied'},info['fingerprint']),'child_old_id_disabled_call_denied')

            elif kind=='storage_rebuild_index':
                before=[r['tool_id'] for r in access.search('eval_lookup')]
                registry.store.rebuild_index()
                check([r['tool_id'] for r in access.search('eval_lookup')]==before,'rebuild_keeps_search_membership')
                server.functions.pop('eval_lookup');await refreshed(registry,server)
                registry.store.rebuild_index()
                check(not access.search('eval_lookup'),'rebuild_does_not_restore_missing_tool')

            elif kind=='storage_builtin_rejected':
                class Builtin:
                    source_id='builtin:forbidden';source_kind='builtin'
                    async def discover_tools(self):return []
                before=len(registry.store.list_tools())
                await expect(ValueError,lambda:registry.register(Builtin()),'builtin_storage_rejected')
                check(len(registry.store.list_tools())==before,'builtin_rejection_no_rows_added')

            elif kind=='validation_badargs_noeffect':
                tid=row()['tool_id'];info=access.describe(tid)
                for args in ({},{'query':5},{'query':'ok','unknown':True}):
                    await expect(ValueError,lambda args=args:access.call(context(),tid,args,info['fingerprint']),'invalid_args_rejected')
                check(not server.effects,'invalid_args_produced_no_effect')

            else:raise ValueError('Unknown extended case: '+kind)
            return {'status':'pass','checks':[]}
        finally:
            # Restore original active provider before closing replacement transports.
            await registry.register(MCPProvider(server),force=True)
            for source_id in extras:
                class Removed:
                    source_kind='mcp'
                    async def discover_tools(self):return []
                removed=Removed();removed.source_id=source_id
                await registry.register(removed)
