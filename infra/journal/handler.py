"""IAM-invoked append-only journal. No body/query logging or public function URL."""
import hashlib,json,os,re,time
from uuid import UUID
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
def sha(x):return hashlib.sha256(x.encode()).hexdigest()
def valid_event(e,source):
 if type(e) is not dict or set(e)!={'event_id','tenant_id','source','resource_kind','resource_id','action','revision','effect_digest'}:raise ValueError()
 for k in ('tenant_id','resource_id'):
  if str(UUID(e[k]))!=e[k]:raise ValueError()
 for k in ('event_id','effect_digest'):
  if type(e[k]) is not str or not re.fullmatch('[a-f0-9]{64}',e[k]):raise ValueError()
 if e['source']!=source or e['resource_kind'] not in {'session','principal','membership','document','policy','conversation','claim'} or e['action'] not in {'deny','delete','access_change','stage_change','package_withdrawal'}:raise ValueError()
 if type(e['revision']) is not str or not re.fullmatch('[A-Za-z0-9_.:+-]{1,128}',e['revision']):raise ValueError()

class Store:
 def __init__(self,client,table,journal_id):self.db,self.table,self.jid=client,table,journal_id
 def get(self,t,sk):
  x=self.db.get_item(TableName=self.table,Key={'pk':{'S':t},'sk':{'S':sk}},ConsistentRead=True).get('Item')
  return json.loads(x['body']['S']) if x else None
 def item(self,t,sk,body):return {'pk':{'S':t},'sk':{'S':sk},'body':{'S':canonical(body)}}
 def head(self,t):return self.get(t,'HEAD') or {'seq':0,'hash':sha(self.jid+':'+t)}
 def append(self,event,phase):
  t=event['tenant_id'];identity='E#'+event['event_id']+'#'+phase
  for _ in range(4):
   prior=self.get(t,identity)
   if prior:
    if prior['event']!=event:raise ValueError()
    return prior
   if phase=='result':
    intent=self.get(t,'E#'+event['event_id']+'#intent')
    if not intent or intent['event']!=event:raise ValueError()
   head=self.head(t);seq=head['seq']+1
   if seq>=10**20:raise ValueError()
   row={'journal_id':self.jid,'tenant_id':t,'seq':seq,'previous_hash':head['hash'],'phase':phase,'event':event,'at':time.time()}
   row['hash']=sha(canonical(row));new_head={'seq':seq,'hash':row['hash']}
   items=[{'Put':{'TableName':self.table,'Item':self.item(t,'S#'+str(seq).zfill(20),row),'ConditionExpression':'attribute_not_exists(pk)'}},
          {'Put':{'TableName':self.table,'Item':self.item(t,identity,row),'ConditionExpression':'attribute_not_exists(pk)'}},
          {'Put':{'TableName':self.table,'Item':self.item(t,'HEAD',new_head),'ConditionExpression':'#b = :old' if head['seq'] else 'attribute_not_exists(pk)',**({'ExpressionAttributeNames':{'#b':'body'},'ExpressionAttributeValues':{':old':{'S':canonical(head)}}} if head['seq'] else {})}}]
   try:self.db.transact_write_items(TransactItems=items);return row
   except ClientError as error:
    if error.response['Error']['Code'] not in {'TransactionCanceledException','TransactionConflictException'}:raise
  raise ValueError()
 def replay(self,t,after,until):
  if not 0<=after<=until or until-after>200:raise ValueError()
  out=[]
  for seq in range(after+1,until+1):
   row=self.get(t,'S#'+str(seq).zfill(20))
   if row is None:raise ValueError()
   out.append(row)
  return out

def handler(event,context):
 try:
  if type(event) is not dict or len(canonical(event))>65536:raise ValueError()
  table=os.environ['JOURNAL_TABLE'];jid=os.environ['JOURNAL_ID'];source=os.environ['SOURCE'];mode=os.environ['MODE']
  store=Store(boto3.client('dynamodb',config=Config(connect_timeout=2,read_timeout=3,retries={'total_max_attempts':1})),table,jid)
  action=event.get('operation')
  if mode=='writer' and action in {'intent','result'} and set(event)=={'operation','event'}:
   valid_event(event['event'],source);result=store.append(event['event'],action)
   return {'status':'ok','record':result}
  if mode=='reader' and action in {'head','replay'}:
   t=event['tenant_id'];UUID(t);challenge=event['challenge']
   if not re.fullmatch('[a-f0-9]{64}',challenge):raise ValueError()
   h=store.head(t)
   if h['seq']:
    last=store.get(t,'S#'+str(h['seq']).zfill(20))
    if not last or last['hash']!=h['hash']:raise ValueError()
   result={'status':'ok','journal_id':jid,'tenant_id':t,'challenge':challenge,'issued_at':time.time(),**h}
   if action=='replay':result['records']=store.replay(t,event['after'],min(event['until'],h['seq']))
   return result
  raise ValueError()
 except Exception:return {'status':'held'}
