"""Write-through tool permissions with a fixed seven-day, non-sliding lifetime."""
import time

ENABLED_TTL_SECONDS = 7 * 24 * 60 * 60


class ToolEnableCache:
    def __init__(self, store, clock=time.monotonic):
        self.store = store
        self.clock = clock
        self.loaded_at = None
        self._rows = {}
        self._identities = {}

    def _replace(self, rows):
        self._rows = {row['tool_id']:dict(row) for row in rows}
        self._identities = {(row['source_id'],row['tool_key']):row['tool_id'] for row in rows}

    def _ensure(self):
        now = self.clock()
        if self.loaded_at is None or now-self.loaded_at >= ENABLED_TTL_SECONDS:
            # Failed reads do not renew the cache or turn unknown tools into allowed ones.
            rows = self.store.permission_rows()
            self._replace(rows)
            self.loaded_at = self.clock()

    def states(self):
        self._ensure()
        return {tid:bool(row['enabled']) for tid,row in self._rows.items()}

    def active_ids(self):
        self._ensure()
        return [tid for tid,row in self._rows.items() if row['enabled'] and row['availability']!='missing']

    def require(self, source_id, tool_key):
        self._ensure()
        tid = self._identities.get((source_id,tool_key))
        row = self._rows.get(tid)
        if row is None or row['availability']=='missing':
            raise PermissionError('工具未注册或已移除：'+source_id+' / '+tool_key)
        if not row['enabled']:
            raise PermissionError('工具已禁用：'+source_id+' / '+tool_key)
        return tid

    def allows(self, source_id, tool_key):
        try:
            self.require(source_id,tool_key)
            return True
        except PermissionError:
            return False

    def set_enabled(self, tool_id, enabled):
        # No await between the DB commit and cache update: requests on the app's
        # event loop see the old pair or the committed pair, never a cache-only write.
        self.store.set_enabled(tool_id,enabled)
        if self.loaded_at is None or self.clock()-self.loaded_at >= ENABLED_TTL_SECONDS or tool_id not in self._rows:
            self._ensure()
            # An externally-added identity can be absent from a still-fresh snapshot.
            if tool_id not in self._rows:
                self.sync_source(self.store.get(tool_id)['source_id'])
        self._rows[tool_id]['enabled'] = enabled
        return {'tool_id':tool_id,'enabled':enabled}

    def sync_source(self, source_id, rows=None):
        """Merge registration/removal changes without extending the full snapshot TTL."""
        if self.loaded_at is None:
            return
        if rows is None:rows=self.store.permission_rows(source_id)
        retained=[row for row in self._rows.values() if row['source_id']!=source_id]
        self._replace(retained+rows)

    def expires_in(self):
        self._ensure()
        return max(0,ENABLED_TTL_SECONDS-(self.clock()-self.loaded_at))
