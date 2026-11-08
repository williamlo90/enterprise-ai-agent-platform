"""Private Lambda journal transport and transaction barrier; no success on ambiguity."""
import asyncio,hashlib,json,re,secrets,time
import boto3
from botocore.config import Config
from psycopg import sql

def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
class JournalUnavailable(RuntimeError):pass
class RecoveryJournal:
 def __init__(self,function,journal_id,source):
  if not re.fullmatch(r'arn:aws:lambda:ap-southeast-1:[0-9]{12}:function:[A-Za-z0-9_-]+',function) or not re.fullmatch('[A-Za-z0-9_-]{8,128}',journal_id) or source not in {'platform','reimbursement','reader'}:raise ValueError('Invalid private journal target.')
  self.function,self.jid,self.source=function,journal_id,source;self.slot=asyncio.Semaphore(1)
  self.client=boto3.client('lambda',region_name='ap-southeast-1',endpoint_url='https://lambda.ap-southeast-1.amazonaws.com',config=Config(connect_timeout=2,read_timeout=4,retries={'total_max_attempts':1}))
 async def call(self,body):
  async with self.slot:
   try:
    result=await asyncio.to_thread(self.client.invoke,FunctionName=self.function,InvocationType='RequestResponse',LogType='None',Payload=canonical(body).encode())
    raw=await asyncio.to_thread(result['Payload'].read,262145)
    if len(raw)>262144 or result.get('FunctionError'):raise ValueError()
    value=json.loads(raw)
    if value.get('status')!='ok':raise ValueError()
    return value
   except Exception:raise JournalUnavailable('Security journal held.') from None
 async def append(self,event,phase):
  value=await self.call({'operation':phase,'event':event});r=value.get('record',{})
  if r.get('journal_id')!=self.jid or r.get('event')!=event or r.get('phase')!=phase or type(r.get('seq')) is not int or r['seq']<1:raise JournalUnavailable()
  expected=hashlib.sha256(canonical({k:v for k,v in r.items() if k!='hash'}).encode()).hexdigest()
  if r.get('hash')!=expected:raise JournalUnavailable()
 async def before_commit(self,connection,schema):
  row=(await (await connection.execute(sql.SQL('SELECT {}.security_journal_events()').format(sql.Identifier(schema)))).fetchone())[0]
  if type(row) is not list or len(row)>64:raise JournalUnavailable()
  for event in row:
   if event.get('source')!=self.source:raise JournalUnavailable()
   await self.append(event,'intent')
  if row:
   final=(await (await connection.execute(sql.SQL('SELECT {}.security_journal_events()').format(sql.Identifier(schema)))).fetchone())[0]
   if final!=row:raise JournalUnavailable()
  return row
 async def after_commit(self,events):
  for event in events:await self.append(event,'result')
 async def head(self,tenant):
  challenge=secrets.token_hex(32);value=await self.call(dict(operation='head',tenant_id=tenant,challenge=challenge))
  if value.get('challenge')!=challenge or value.get('journal_id')!=self.jid or value.get('tenant_id')!=tenant or abs(time.time()-value.get('issued_at',0))>10:raise JournalUnavailable()
  return value
 async def replay(self,tenant,cursor):
  head=await self.head(tenant);seq=cursor['seq'];previous=cursor['hash'];events=[]
  if cursor.get('journal_id')!=self.jid or type(seq) is not int or seq<0 or seq>head['seq']:raise JournalUnavailable()
  if seq==0 and previous!=hashlib.sha256((self.jid+':'+tenant).encode()).hexdigest():raise JournalUnavailable()
  while seq<head['seq']:
   until=min(seq+100,head['seq']);challenge=secrets.token_hex(32)
   page=await self.call(dict(operation='replay',tenant_id=tenant,challenge=challenge,after=seq,until=until))
   if page.get('challenge')!=challenge or page.get('journal_id')!=self.jid or page.get('tenant_id')!=tenant or abs(time.time()-page.get('issued_at',0))>10:raise JournalUnavailable()
   rows=page.get('records')
   if type(rows) is not list or len(rows)!=until-seq:raise JournalUnavailable()
   for row in rows:
    if row.get('seq')!=seq+1 or row.get('previous_hash')!=previous or row.get('tenant_id')!=tenant or row.get('journal_id')!=self.jid or row.get('phase') not in {'intent','result'}:raise JournalUnavailable()
    if hashlib.sha256(canonical({k:v for k,v in row.items() if k!='hash'}).encode()).hexdigest()!=row.get('hash'):raise JournalUnavailable()
    seq+=1;previous=row['hash'];events.append(row)
  latest=await self.head(tenant)
  if (latest['seq'],latest['hash'])!=(seq,previous):raise JournalUnavailable()
  # Intents always deny, including unknown/rolled-back local outcomes. Results
  # never cancel a deny or authorize restored access.
  denied={(r['event']['resource_kind'],r['event']['resource_id']) for r in events if r['phase']=='intent'}
  return dict(journal_id=self.jid,tenant_id=tenant,seq=seq,hash=previous,deny=[dict(kind=k,id=i) for k,i in sorted(denied)])
 def close(self):self.client.close()
