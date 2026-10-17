"""Persistent tool definitions, versioned fingerprints and a rebuildable search index.

This module stores metadata only. Connections, executable callbacks and enabled-state
caches belong to the runtime registry, not to this database layer.
"""
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

FINGERPRINT_VERSION = 'sha256:tool-definition:v1'
KINDS = {'mcp', 'plugin', 'http'}
AVAILABILITY = {'unknown', 'available', 'unavailable', 'missing'}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def definition_fingerprint(name, description, parameters):
    # Object key order is irrelevant; array order is preserved intentionally.
    definition = {'version': FINGERPRINT_VERSION, 'name': name,
                  'description': description, 'parameters': parameters}
    return hashlib.sha256(canonical_json(definition).encode('utf-8')).hexdigest()


class ToolStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS tool_catalog_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO tool_catalog_meta VALUES ('schema_version', '1');
                CREATE TABLE IF NOT EXISTS tool_catalog (
                    tool_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL CHECK(source_kind IN ('mcp','plugin','http','builtin')),
                    tool_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    availability TEXT NOT NULL DEFAULT 'unknown'
                        CHECK(availability IN ('unknown','available','unavailable','missing')),
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    checked_at REAL NOT NULL,
                    UNIQUE(source_id, tool_key));
                CREATE INDEX IF NOT EXISTS tool_catalog_source ON tool_catalog(source_id);
                CREATE TABLE IF NOT EXISTS tool_fingerprints (
                    tool_id TEXT PRIMARY KEY REFERENCES tool_catalog(tool_id) ON DELETE CASCADE,
                    algorithm TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    updated_at REAL NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS tool_catalog_fts USING fts5(
                    tool_id UNINDEXED, name, description, parameter_text, source_id,
                    tokenize='trigram');
                DROP TRIGGER IF EXISTS tool_catalog_search_insert;
                CREATE TRIGGER tool_catalog_search_insert AFTER INSERT ON tool_catalog BEGIN
                    INSERT INTO tool_catalog_fts(tool_id,name,description,parameter_text,source_id)
                    SELECT new.tool_id,new.name,new.description,new.parameters_json,new.source_id WHERE new.availability!='missing';
                END;
                DROP TRIGGER IF EXISTS tool_catalog_search_update;
                CREATE TRIGGER tool_catalog_search_update
                AFTER UPDATE OF name,description,parameters_json,source_id,availability ON tool_catalog BEGIN
                    DELETE FROM tool_catalog_fts WHERE tool_id=old.tool_id;
                    INSERT INTO tool_catalog_fts(tool_id,name,description,parameter_text,source_id)
                    SELECT new.tool_id,new.name,new.description,new.parameters_json,new.source_id WHERE new.availability!='missing';
                END;
                CREATE TRIGGER IF NOT EXISTS tool_catalog_search_delete AFTER DELETE ON tool_catalog BEGIN
                    DELETE FROM tool_catalog_fts WHERE tool_id=old.tool_id;
                END;
            ''')
            version = db.execute("SELECT value FROM tool_catalog_meta WHERE key='schema_version'").fetchone()[0]
            if version not in ('1','2'):
                raise RuntimeError('Unsupported tool catalog schema version: ' + version)
            if version=='1':
                db.execute('ALTER TABLE tool_catalog ADD COLUMN server_id TEXT')
                db.execute("UPDATE tool_catalog_meta SET value='2' WHERE key='schema_version'")
            db.execute('CREATE INDEX IF NOT EXISTS tool_catalog_server ON tool_catalog(server_id)')
            # Remove legacy builtin metadata and its FK/trigger-managed derivatives.
            # The legacy CHECK accepts old rows so existing databases need no rewrite;
            # public registration rejects builtin sources via KINDS.
            db.execute("DELETE FROM tool_catalog WHERE source_kind='builtin'")
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @staticmethod
    def _definition(value):
        definition = dict(value)
        for field in ('source_id', 'tool_key', 'name'):
            if not isinstance(definition.get(field), str) or not definition[field].strip():
                raise ValueError(field + ' must be a nonempty string')
        if definition.get('source_kind') not in KINDS:
            raise ValueError('Unsupported tool source kind')
        if not isinstance(definition.get('description'), str):
            raise ValueError('description must be a string')
        if not isinstance(definition.get('parameters'), dict):
            raise ValueError('parameters must be a JSON schema object')
        server_id=definition.get('server_id')
        if server_id is not None and (definition['source_kind']!='mcp' or not isinstance(server_id,str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',server_id)):
            raise ValueError('server_id must be a valid remote MCP server identifier')
        definition['server_id']=server_id
        definition['parameters_json'] = canonical_json(definition['parameters'])
        definition['digest'] = definition_fingerprint(
            definition['name'], definition['description'], definition['parameters'])
        return definition

    def upsert(self, *, source_id, source_kind, tool_key, name, description, parameters, server_id=None):
        """Store a definition without overwriting the user's enabled/health state."""
        return self.upsert_many([dict(source_id=source_id, source_kind=source_kind,
                                     tool_key=tool_key, name=name, description=description,
                                     parameters=parameters,server_id=server_id)])[0]

    def upsert_many(self, definitions):
        """Atomically register definitions and their fingerprints/index entries.

        This is not a complete discovery snapshot: absence from this list never
        means deletion. Provider refresh uses reconcile_source for removals.
        """
        prepared = [self._definition(value) for value in definitions]
        keys = [(d['source_id'], d['tool_key']) for d in prepared]
        if len(set(keys)) != len(keys):
            raise ValueError('Duplicate tool identity in registration batch')
        with closing(self.connect()) as db, db:
            return self._upsert(db,prepared)

    def _upsert(self, db, prepared):
        now = time.time()
        results = []
        for d in prepared:
            old = db.execute('''SELECT t.tool_id,t.source_kind,f.digest,f.algorithm
                FROM tool_catalog t LEFT JOIN tool_fingerprints f USING(tool_id)
                WHERE source_id=? AND tool_key=?''', (d['source_id'], d['tool_key'])).fetchone()
            if old and old['source_kind'] != d['source_kind']:
                raise ValueError('A registered source cannot change kind')
            if d['server_id'] is None:
                binding=db.execute('SELECT server_id FROM tool_catalog WHERE source_id=? AND server_id IS NOT NULL LIMIT 1',(d['source_id'],)).fetchone()
                if binding is not None:d={**d,'server_id':binding['server_id']}
            if d['server_id'] is not None:
                self._bind_server_id(db,d['source_id'],d['server_id'])
            tool_id = old['tool_id'] if old else 'tool_' + uuid.uuid4().hex
            changed = not old or old['digest'] != d['digest'] or old['algorithm'] != FINGERPRINT_VERSION
            if not old:
                db.execute('''INSERT INTO tool_catalog
                    (tool_id,source_id,source_kind,tool_key,name,description,parameters_json,
                     created_at,updated_at,checked_at,server_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (tool_id,d['source_id'],d['source_kind'],d['tool_key'],d['name'],
                     d['description'],d['parameters_json'],now,now,now,d['server_id']))
            elif changed:
                db.execute('''UPDATE tool_catalog SET name=?,description=?,parameters_json=?,
                    updated_at=?,checked_at=? WHERE tool_id=?''',
                    (d['name'],d['description'],d['parameters_json'],now,now,tool_id))
            else:
                db.execute('UPDATE tool_catalog SET checked_at=? WHERE tool_id=?', (now,tool_id))
            if changed:
                db.execute('''INSERT INTO tool_fingerprints VALUES (?,?,?,?)
                    ON CONFLICT(tool_id) DO UPDATE SET algorithm=excluded.algorithm,
                    digest=excluded.digest,updated_at=excluded.updated_at''',
                    (tool_id,FINGERPRINT_VERSION,d['digest'],now))
            server_id=db.execute('SELECT server_id FROM tool_catalog WHERE tool_id=?',(tool_id,)).fetchone()[0]
            results.append({'tool_id': tool_id, 'server_id':server_id,'source_id':d['source_id'], 'changed': bool(changed), 'fingerprint': d['digest']})
        return results

    @staticmethod
    def _bind_server_id(db,source_id,server_id):
        existing=db.execute('SELECT DISTINCT server_id,source_kind FROM tool_catalog WHERE source_id=?',(source_id,)).fetchall()
        if any(row['source_kind']!='mcp' or row['server_id'] not in (None,server_id) for row in existing):
            raise ValueError('A tool source cannot change its server_id')
        db.execute('UPDATE tool_catalog SET server_id=? WHERE source_id=? AND server_id IS NULL',(server_id,source_id))

    def bind_server_id(self,source_id,server_id):
        # Startup backfill uses the authoritative config key, even if discovery fails.
        if not isinstance(server_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',server_id):
            raise ValueError('Invalid server_id')
        with closing(self.connect()) as db, db:
            self._bind_server_id(db,source_id,server_id)

    def reconcile_source(self, source_id, source_kind, definitions):
        """Commit a complete successful snapshot, retaining missing identities/settings."""
        if source_kind not in KINDS:
            raise ValueError('Unsupported tool source kind')
        prepared=[self._definition(value) for value in definitions]
        if any(d['source_id']!=source_id or d['source_kind']!=source_kind for d in prepared):
            raise ValueError('Snapshot crosses tool source boundaries')
        keys={d['tool_key'] for d in prepared}
        if len(keys)!=len(prepared):
            raise ValueError('Duplicate tool identity in source snapshot')
        with closing(self.connect()) as db, db:
            previous=db.execute('SELECT tool_id,tool_key,source_kind,availability FROM tool_catalog WHERE source_id=?',(source_id,)).fetchall()
            if any(row['source_kind']!=source_kind for row in previous):
                raise ValueError('A registered source cannot change kind')
            result=self._upsert(db,prepared)
            removed=restored=0
            for row in previous:
                if row['tool_key'] not in keys and row['availability']!='missing':
                    db.execute("UPDATE tool_catalog SET availability='missing',checked_at=? WHERE tool_id=?",(time.time(),row['tool_id']))
                    removed+=1
                elif row['tool_key'] in keys and row['availability']=='missing':
                    db.execute("UPDATE tool_catalog SET availability='unknown' WHERE tool_id=?",(row['tool_id'],))
                    restored+=1
            permissions=[dict(row) for row in db.execute("SELECT tool_id,source_id,tool_key,enabled,availability FROM tool_catalog WHERE source_id=?",(source_id,))]
            return {'tools':result,'removed':removed,'restored':restored,'permissions':permissions}

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        value = dict(row)
        value['parameters'] = json.loads(value.pop('parameters_json'))
        value['enabled'] = bool(value['enabled'])
        return value

    def get(self, tool_id):
        with closing(self.connect()) as db:
            row = db.execute('''SELECT t.*,f.algorithm,f.digest AS fingerprint,
                f.updated_at AS fingerprint_updated_at FROM tool_catalog t
                LEFT JOIN tool_fingerprints f USING(tool_id) WHERE t.tool_id=?''', (tool_id,)).fetchone()
        return self._decode(row)

    def list_tools(self, source_id=None, *, server_id=None):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT * FROM tool_catalog WHERE (? IS NULL OR source_id=?) AND (? IS NULL OR server_id=?) ORDER BY source_id,name,tool_id',
                              (source_id,source_id,server_id,server_id)).fetchall()
        return [self._decode(row) for row in rows]

    def permission_rows(self, source_id=None):
        with closing(self.connect()) as db:
            rows=db.execute('''SELECT tool_id,source_id,tool_key,enabled,availability FROM tool_catalog
                WHERE (? IS NULL OR source_id=?)''',(source_id,source_id)).fetchall()
        return [dict(row) for row in rows]

    def set_enabled(self, tool_id, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        with closing(self.connect()) as db, db:
            if not db.execute('UPDATE tool_catalog SET enabled=? WHERE tool_id=?', (int(enabled),tool_id)).rowcount:
                raise KeyError(tool_id)

    def set_availability(self, tool_id, availability):
        if availability not in AVAILABILITY:
            raise ValueError('Unsupported availability')
        with closing(self.connect()) as db, db:
            if not db.execute('UPDATE tool_catalog SET availability=? WHERE tool_id=?', (availability,tool_id)).rowcount:
                raise KeyError(tool_id)

    def search(self, query, limit=10, enabled_only=True, tool_ids=None, server_id=None):
        """Literal keyword search, including Chinese; not semantic retrieval.

        Short terms use substring matching because trigram FTS requires 3 characters.
        Readiness is deliberately separate: registration alone does not prove that a
        runtime executor is connected. The registry must enforce readiness/permissions.
        """
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError('query must contain 1 to 200 characters')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('limit must be 1 to 50')
        terms = query.strip().split()
        allowed=json.dumps(list(tool_ids)) if tool_ids is not None else None
        with closing(self.connect()) as db:
            if all(len(term) >= 3 for term in terms):
                match = ' AND '.join('"' + term.replace('"','""') + '"' for term in terms)
                rows = db.execute('''SELECT t.* FROM tool_catalog_fts
                    JOIN tool_catalog t ON t.tool_id=tool_catalog_fts.tool_id
                    WHERE tool_catalog_fts MATCH ? AND (?=0 OR t.enabled=1)
                    AND (? IS NULL OR t.server_id=?)
                    AND (? IS NULL OR t.tool_id IN (SELECT value FROM json_each(?)))
                    ORDER BY bm25(tool_catalog_fts),t.tool_id LIMIT ?''',
                    (match,int(enabled_only),server_id,server_id,allowed,allowed,limit)).fetchall()
            else:
                clauses = ['instr(lower(f.name||\' \'||f.description||\' \'||f.parameter_text||\' \'||f.source_id),lower(?))>0' for _ in terms]
                rows = db.execute('''SELECT t.* FROM tool_catalog_fts f
                    JOIN tool_catalog t ON t.tool_id=f.tool_id WHERE ''' + ' AND '.join(clauses) +
                    ' AND (?=0 OR t.enabled=1) AND (? IS NULL OR t.server_id=?) AND (? IS NULL OR t.tool_id IN (SELECT value FROM json_each(?))) ORDER BY t.source_id,t.name,t.tool_id LIMIT ?',
                    (*terms,int(enabled_only),server_id,server_id,allowed,allowed,limit)).fetchall()
        return [self._decode(row) for row in rows]

    def rebuild_index(self):
        """Recover the derived index from authoritative definitions, atomically."""
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM tool_catalog_fts')
            db.execute('''INSERT INTO tool_catalog_fts(tool_id,name,description,parameter_text,source_id)
                SELECT tool_id,name,description,parameters_json,source_id FROM tool_catalog WHERE availability!='missing'
                ''')
