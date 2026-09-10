import os, sqlite3, uuid, json, hmac, hashlib
from datetime import datetime, timezone
from contextlib import contextmanager
from . import config
from .schema import SCHEMA, COLUMNS, PII, JSON_FIELDS, INT_FIELDS, FKS, UNIQUE
from .security import encrypt, decrypt, canonical, AppError

COMMON=["id","created_at","updated_at","created_by","deleted_at"]
def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def uid(): return str(uuid.uuid4())
class Database:
    def __init__(self,url=None):
        self.url=url or config.DATABASE_URL; self.pg=self.url.startswith("postgresql://")
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            self.conn=psycopg.connect(self.url,row_factory=dict_row,autocommit=True)
        else:
            path=self.url.removeprefix("sqlite:///")
            self.conn=sqlite3.connect(path,timeout=30,isolation_level=None,check_same_thread=False)
            self.conn.row_factory=sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=30000")
        self.depth=0
    def execute(self,sql,args=()): return self.conn.execute(sql.replace("?","%s") if self.pg else sql,args)
    def close(self): self.conn.close()
    def table_exists(self,table):
        if self.pg:
            return bool(self.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=?",(table,)).fetchone())
        return bool(self.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())
    def table_columns(self,table):
        if not self.table_exists(table): return set()
        if self.pg:
            rows=self.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?",(table,)).fetchall()
            return {row["column_name"] for row in rows}
        return {row["name"] for row in self.execute(f'PRAGMA table_info("{table}")').fetchall()}
    def column_sql(self,table,name,typ,with_reference=True):
        sqltype="BIGINT" if typ=="int" else "TEXT"
        result=f'"{name}" {sqltype}'
        if with_reference and not self.pg and name in FKS.get(table,{}):
            result+=f' REFERENCES "{FKS[table][name]}"(id)'
        return result
    @contextmanager
    def atomic(self):
        if self.depth:
            self.depth+=1
            try: yield self
            finally: self.depth-=1
            return
        self.execute("BEGIN" if self.pg else "BEGIN IMMEDIATE"); self.depth=1
        try:
            if self.pg: self.execute("SELECT pg_advisory_xact_lock(71420861)")
            yield self
            self.conn.commit()
        except Exception:
            self.conn.rollback(); raise
        finally: self.depth=0
    def migrate(self):
        with self.atomic():
            for table,spec in SCHEMA.items():
                cols=["id TEXT PRIMARY KEY", "created_at TEXT NOT NULL", "updated_at TEXT NOT NULL", "created_by TEXT", "deleted_at TEXT"]
                for item in spec.split():
                    name,typ=item.split(":"); cols.append(self.column_sql(table,name,typ))
                self.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ('+", ".join(cols)+")")
                existing=self.table_columns(table)
                for item in spec.split():
                    name,typ=item.split(":")
                    if name not in existing:
                        self.execute(f'ALTER TABLE "{table}" ADD COLUMN '+self.column_sql(table,name,typ))
            self._migrate_person_client_links()
            if self.pg:
                for table,fields in FKS.items():
                    for field,target in fields.items():
                        name=f"fk_{table}_{field}"
                        exists=self.execute("SELECT 1 FROM pg_constraint WHERE conname=?",(name,)).fetchone()
                        if not exists: self.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" FOREIGN KEY ("{field}") REFERENCES "{target}"(id)')
            for table,col in UNIQUE:
                self.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "uq_{table}_{col}" ON "{table}" ("{col}")')
            self.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_deal_person ON deal_persons(deal_id,person_id) WHERE deleted_at IS NULL')
            self.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_client_person_relation ON client_person_relations(client_id,person_id,relation_type) WHERE deleted_at IS NULL')
            self.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_client_primary_person_relation ON client_person_relations(client_id) WHERE is_primary=1 AND status='active' AND deleted_at IS NULL")
            self.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_data_migration_entity ON data_migration_events(migration,entity_type,entity_id) WHERE deleted_at IS NULL')
            self.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_template_version ON template_versions(template_id,version)')
            self.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_job_run ON job_runs(name,run_date)')
            for table,field in [("deals","manager_id"),("clients","manager_id"),("clients","person_id"),("persons","identity_key"),("client_person_relations","client_id"),("client_person_relations","person_id"),("access_grants","subject_id"),("access_grants","resource_id"),("identity_documents","person_id"),("consents","person_id"),("documents","deal_id"),("tasks","due_at"),("notifications","status")]:
                self.execute(f'CREATE INDEX IF NOT EXISTS "ix_{table}_{field}" ON "{table}" ("{field}")')
            evidence_tables=("audit_log","ocr_runs","ocr_confirmations","integration_calls","file_scan_events","access_grant_events","data_migration_events")
            if self.pg:
                self.execute("CREATE OR REPLACE FUNCTION deny_evidence_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Evidence is immutable'; END $$")
                for table in evidence_tables:
                    trigger="immutable_"+table
                    self.execute(f'DROP TRIGGER IF EXISTS "{trigger}" ON "{table}"')
                    self.execute(f'CREATE TRIGGER "{trigger}" BEFORE UPDATE OR DELETE OR TRUNCATE ON "{table}" FOR EACH STATEMENT EXECUTE FUNCTION deny_evidence_mutation()')
            else:
                for table in evidence_tables:
                    for action in ("UPDATE","DELETE"):
                        trigger=f"{table}_no_{action.lower()}"
                        self.execute(f'CREATE TRIGGER IF NOT EXISTS "{trigger}" BEFORE {action} ON "{table}" BEGIN SELECT RAISE(ABORT,\'Evidence is immutable\'); END')
    def _migrate_person_client_links(self):
        """Backfill the legacy person owner link without inventing its semantics."""
        if not self.table_exists("persons") or "client_id" not in self.table_columns("persons"):
            return
        migration="persons_client_id_to_relations_v1"
        rows=self.execute('SELECT id,client_id,created_at,created_by,deleted_at FROM persons WHERE client_id IS NOT NULL AND client_id<>?',("",)).fetchall()
        for raw in rows:
            person=dict(raw); client_id=person["client_id"]
            if not self.execute('SELECT 1 FROM clients WHERE id=?',(client_id,)).fetchone():
                raise AppError("Миграция остановлена: у person.client_id нет существующего клиента",500,{"person_id":person["id"]})
            linked=self.execute('SELECT id FROM client_person_relations WHERE client_id=? AND person_id=? ORDER BY created_at,id LIMIT 1',(client_id,person["id"])).fetchone()
            if linked:
                relation_id=dict(linked)["id"]; outcome="already_linked"
            else:
                relation_id=uid(); at=person.get("created_at") or now()
                self.execute('INSERT INTO client_person_relations (id,created_at,updated_at,created_by,deleted_at,client_id,person_id,relation_type,is_primary,status,valid_from,valid_to,source,verified_by,verified_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(relation_id,at,at,person.get("created_by"),person.get("deleted_at"),client_id,person["id"],"unknown",0,"review_required",at,None,"legacy_person_client_id",None,None))
                outcome="backfilled_review_required"
            recorded=self.execute('SELECT 1 FROM data_migration_events WHERE migration=? AND entity_type=? AND entity_id=?',(migration,"persons",person["id"])).fetchone()
            if not recorded:
                at=now(); self.execute('INSERT INTO data_migration_events (id,created_at,updated_at,created_by,deleted_at,migration,entity_type,entity_id,outcome,details,occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uid(),at,at,None,None,migration,"persons",person["id"],outcome,canonical({"relation_id":relation_id,"semantic_status":"unknown_requires_review"}),at))
        if self.pg:
            self.execute('ALTER TABLE persons DROP CONSTRAINT IF EXISTS fk_persons_client_id')
            self.execute('ALTER TABLE persons DROP COLUMN IF EXISTS client_id')
        else:
            self.execute('DROP INDEX IF EXISTS ix_persons_client_id')
            self.execute('ALTER TABLE persons DROP COLUMN client_id')
    def decode(self,table,row):
        if row is None: return None
        out=dict(row)
        for field in PII.get(table,set()):
            if out.get(field) is not None: out[field]=decrypt(out[field],table+"."+field)
        for field in JSON_FIELDS[table]:
            if out.get(field) is not None: out[field]=json.loads(out[field])
        return out
    def all(self,table,where="",args=(),include_deleted=False):
        if table not in SCHEMA: raise AppError("Неизвестный справочник",404)
        condition="1=1" if include_deleted else "deleted_at IS NULL"
        if where: condition+=" AND ("+where+")"
        rows=self.execute(f'SELECT * FROM "{table}" WHERE '+condition+" ORDER BY created_at,id",args).fetchall()
        return [self.decode(table,r) for r in rows]
    def one(self,table,id,include_deleted=False):
        rows=self.all(table,"id=?",(str(id),),include_deleted)
        if not rows: raise AppError("Запись не найдена или недоступна",404)
        return rows[0]
    def find(self,table,where,args=()):
        rows=self.all(table,where,args); return rows[0] if rows else None
    def encode(self,table,data):
        out={}
        for field,value in data.items():
            if field not in COMMON+COLUMNS[table]: raise AppError("Неизвестное поле: "+field)
            if field in JSON_FIELDS[table] and value is not None: value=canonical(value)
            if field in PII.get(table,set()) and value is not None: value=encrypt(value,table+"."+field)
            out[field]=value
        return out
    def insert(self,table,data,actor=None):
        at=now(); obj={"id":uid(),"created_at":at,"updated_at":at,"created_by":actor,"deleted_at":None,**data}
        uuid.UUID(obj["id"])
        enc=self.encode(table,obj); fields=list(enc)
        self.execute(f'INSERT INTO "{table}" ('+",".join('"'+f+'"' for f in fields)+") VALUES ("+",".join("?" for _ in fields)+")",tuple(enc.values()))
        return self.one(table,obj["id"])
    def update(self,table,id,data):
        if table=="audit_log": raise AppError("Журнал аудита неизменяем",403)
        obj=self.encode(table,{**data,"updated_at":now()})
        self.execute(f'UPDATE "{table}" SET '+",".join('"'+f+'"=?' for f in obj)+" WHERE id=?",(*obj.values(),id))
        return self.one(table,id,True)
    def soft_delete(self,table,id): return self.update(table,id,{"deleted_at":now()})
    def audit(self,actor,action,entity,entity_id,details=None,ip="",agent=""):
        rows=self.all("audit_log")
        referenced={r["previous_hash"] for r in rows}
        tails=[r for r in rows if r["entry_hash"] not in referenced]
        previous=tails[0]["entry_hash"] if tails else "0"*64
        item={"user_id":actor,"action":action,"entity":entity,"entity_id":entity_id,"details":details or {},"ip":ip,"user_agent":agent[:500],"time":now(),"previous_hash":previous}
        item["entry_hash"]=hmac.new(config.AUDIT_KEY,canonical(item).encode(),hashlib.sha256).hexdigest()
        return self.insert("audit_log",item,actor)
    def verify_audit(self):
        rows=self.all("audit_log"); previous="0"*64; remaining={r["previous_hash"]:r for r in rows}
        if len(remaining)!=len(rows): return False
        while previous in remaining:
            row=remaining.pop(previous); data={k:row[k] for k in COLUMNS["audit_log"] if k!="entry_hash"}
            actual=hmac.new(config.AUDIT_KEY,canonical(data).encode(),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(actual,row["entry_hash"]): return False
            previous=actual
        return not remaining
