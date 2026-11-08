"""Owned local metadata persistence; not a platform-data or cloud deletion adapter."""
import json
from pathlib import Path
import sqlite3
from enterprise_ai_platform.modules.privacy.ports import PrivacyFailure
from enterprise_ai_platform.modules.privacy.domain import identifier,Policy,Rule,Category


def encoded(value):
    try:
        raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(raw.encode())>1048576:raise ValueError
        return raw
    except (ValueError,TypeError):raise PrivacyFailure('invalid') from None


class OwnedPrivacyDatabase:
    def __init__(self,root,namespace,owner_token,kind):
        identifier(namespace);identifier(owner_token)
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/('privacy-'+namespace+'.sqlite3');self.kind=kind;self.owner_token=owner_token
        if self.path.is_symlink():raise PrivacyFailure('denied')
        fresh=False
        try:
            with self.path.open('xb'):pass
            fresh=True
        except FileExistsError:pass
        with self.connect() as db:
            if fresh:
                db.execute('CREATE TABLE private_owner(kind TEXT,token TEXT,version INTEGER)')
                db.execute('INSERT INTO private_owner VALUES(?,?,1)',(kind,owner_token))
            try:owners=db.execute('SELECT kind,token,version FROM private_owner').fetchall()
            except sqlite3.Error:raise PrivacyFailure('denied') from None
            if owners!=[(kind,owner_token,1)]:raise PrivacyFailure('denied')
        with self.connect() as db:db.execute('PRAGMA journal_mode=WAL')

    def connect(self):
        return Connection(self.path)


class Connection:
    def __init__(self,path):self.path=path
    def __enter__(self):
        self.db=sqlite3.connect(self.path,timeout=2);self.db.execute('PRAGMA synchronous=FULL');return self.db
    def __exit__(self,kind,error,tb):
        try:
            if kind is None:self.db.commit()
            else:self.db.rollback()
        finally:self.db.close()


class LocalPrivacyRepository(OwnedPrivacyDatabase):
    def __init__(self,root,namespace,owner_token):
        super().__init__(root,namespace,owner_token,'operations')
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS operations(tenant TEXT,operation_id TEXT,request_digest TEXT,revision INTEGER,body TEXT,PRIMARY KEY(tenant,operation_id))')

    async def get(self,tenant_id,operation_id):
        identifier(tenant_id);identifier(operation_id)
        with self.connect() as db:row=db.execute('SELECT body FROM operations WHERE tenant=? AND operation_id=?',(tenant_id,operation_id)).fetchone()
        return json.loads(row[0]) if row else None

    async def create(self,tenant_id,operation_id,request_digest,body):
        identifier(tenant_id);identifier(operation_id)
        if body.get('tenant_id')!=tenant_id or body.get('id')!=operation_id or body.get('revision')!=1 or body.get('request_digest')!=request_digest:raise PrivacyFailure('invalid')
        raw=encoded(body)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT request_digest,body FROM operations WHERE tenant=? AND operation_id=?',(tenant_id,operation_id)).fetchone()
            if row:
                if row[0]!=request_digest:raise PrivacyFailure('conflict')
                return json.loads(row[1])
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?)',(tenant_id,operation_id,request_digest,1,raw))
        return json.loads(raw)

    async def save(self,tenant_id,operation_id,expected_revision,body):
        identifier(tenant_id);identifier(operation_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT revision,body FROM operations WHERE tenant=? AND operation_id=?',(tenant_id,operation_id)).fetchone()
            if row is None or row[0]!=expected_revision:raise PrivacyFailure('conflict')
            prior=json.loads(row[1]);value=json.loads(encoded(body))
            immutable=('id','tenant_id','subject_id','actor_id','grant_revision','operation','category','request_digest','policy_digest')
            if any(value.get(k)!=prior.get(k) for k in immutable) or value.get('revision')!=expected_revision:raise PrivacyFailure('conflict')
            if value.get('state') not in {'queued','held','complete'} or len(value['steps'])!=len(prior['steps']):raise PrivacyFailure('invalid')
            if prior['state'] in {'held','complete'} and value['state']!=prior['state']:raise PrivacyFailure('held')
            for old,new in zip(prior['steps'],value['steps']):
                allowed={'pending':{'pending','intent'},'intent':{'intent','complete'},'complete':{'complete'}}
                if new['asset']!=old['asset'] or new['effect_key']!=old['effect_key'] or new['state'] not in allowed[old['state']]:raise PrivacyFailure('conflict')
            if value['state']=='complete' and any(v['state']!='complete' for v in value['steps']):raise PrivacyFailure('held')
            value['revision']=expected_revision+1;raw=encoded(value)
            db.execute('UPDATE operations SET revision=?,body=? WHERE tenant=? AND operation_id=?',(value['revision'],raw,tenant_id,operation_id))
        return value


class LocalPrivacyJournal(OwnedPrivacyDatabase):
    def __init__(self,root,namespace,owner_token):
        super().__init__(root,namespace,owner_token,'journal')
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT,operation_id TEXT,effect_key TEXT,kind TEXT,body TEXT,UNIQUE(tenant,operation_id,effect_key,kind))')
            db.execute("CREATE TRIGGER IF NOT EXISTS immutable_event_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable event');END")
            db.execute("CREATE TRIGGER IF NOT EXISTS immutable_event_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable event');END")

    async def append(self,tenant_id,operation_id,effect_key,kind,value):
        identifier(tenant_id);identifier(operation_id);raw=encoded(value)
        if kind not in {'intent','result'} or not isinstance(effect_key,str) or not (effect_key=='scope' or len(effect_key)==64 and all(c in '0123456789abcdef' for c in effect_key)):raise PrivacyFailure('invalid')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if kind=='result' and db.execute('SELECT 1 FROM events WHERE tenant=? AND operation_id=? AND effect_key=? AND kind=\'intent\'',(tenant_id,operation_id,effect_key)).fetchone() is None:raise PrivacyFailure('held')
            old=db.execute('SELECT body FROM events WHERE tenant=? AND operation_id=? AND effect_key=? AND kind=?',(tenant_id,operation_id,effect_key,kind)).fetchone()
            if old:
                if old[0]!=raw:raise PrivacyFailure('conflict')
                return
            db.execute('INSERT INTO events(tenant,operation_id,effect_key,kind,body) VALUES(?,?,?,?,?)',(tenant_id,operation_id,effect_key,kind,raw))

    async def intent(self,tenant_id,operation_id,effect_key,metadata):
        await self.append(tenant_id,operation_id,effect_key,'intent',metadata)

    async def result(self,tenant_id,operation_id,effect_key,outcome):
        if outcome not in {'applied','not_applied','unknown','retained','held','recorded'}:raise PrivacyFailure('invalid')
        await self.append(tenant_id,operation_id,effect_key,'result',dict(outcome=outcome))


def policy_record(policy):
    return dict(tenant_id=policy.tenant_id,revision=policy.revision,approved_by=policy.approved_by,approved_at=policy.approved_at,expires_at=policy.expires_at,
        rules=[dict(category=r.category.value,retention_seconds=r.retention_seconds,regions=list(r.regions),stores=sorted(r.stores),provider_retention_seconds=r.provider_retention_seconds,training_allowed=r.training_allowed) for r in policy.rules])


def policy_from_record(value):
    try:
        rules=tuple(Rule(Category(r['category']),r['retention_seconds'],tuple(r['regions']),frozenset(r['stores']),r['provider_retention_seconds'],r['training_allowed']) for r in value['rules'])
        return Policy(value['tenant_id'],value['revision'],value['approved_by'],value['approved_at'],value['expires_at'],rules)
    except (KeyError,ValueError,TypeError):raise PrivacyFailure('policy_missing') from None


class LocalPrivacyPolicyRegistry(OwnedPrivacyDatabase):
    """Append-only local approved snapshots; no production authorization implied."""
    def __init__(self,root,namespace,owner_token):
        super().__init__(root,namespace,owner_token,'policies')
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS policies(tenant TEXT,revision INTEGER,fingerprint TEXT,body TEXT,PRIMARY KEY(tenant,revision))')
            db.execute("CREATE TRIGGER IF NOT EXISTS immutable_policy_update BEFORE UPDATE ON policies BEGIN SELECT RAISE(ABORT,'immutable policy');END")
            db.execute("CREATE TRIGGER IF NOT EXISTS immutable_policy_delete BEFORE DELETE ON policies BEGIN SELECT RAISE(ABORT,'immutable policy');END")

    async def current(self,tenant_id):
        identifier(tenant_id)
        with self.connect() as db:row=db.execute('SELECT fingerprint,body FROM policies WHERE tenant=? ORDER BY revision DESC LIMIT 1',(tenant_id,)).fetchone()
        if row is None:return None
        value=policy_from_record(json.loads(row[1]))
        if value.tenant_id!=tenant_id or value.fingerprint()!=row[0]:raise PrivacyFailure('policy_missing')
        return value

    async def append(self,policy):
        if type(policy) is not Policy:raise PrivacyFailure('invalid')
        # Full validity/current clock and approver checks belong to publication;
        # this adapter only stores an already trusted immutable snapshot.
        raw=encoded(policy_record(policy));fingerprint=policy.fingerprint()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT revision,fingerprint FROM policies WHERE tenant=? ORDER BY revision DESC LIMIT 1',(policy.tenant_id,)).fetchone()
            if row and row[0]==policy.revision and row[1]==fingerprint:return policy
            if policy.revision!=(row[0]+1 if row else 1):raise PrivacyFailure('conflict')
            db.execute('INSERT INTO policies VALUES(?,?,?,?)',(policy.tenant_id,policy.revision,fingerprint,raw))
        return policy
