"""Render local JSON manifests only. Never contacts Kubernetes or AWS."""
import argparse,json,re
from pathlib import Path

def render(config):
 if config['namespace']!='enterprise-ai' or config['region']!='ap-southeast-1' or config['replicas'] not in (1,2) or config['worker_replicas'] not in (0,1):raise ValueError('Unsupported deployment scope')
 if 'REPLACE' in json.dumps(config) or not re.fullmatch('[a-f0-9]{40}',config['image_tag']):raise ValueError('Fill operator configuration and exact image SHA')
 if not re.fullmatch(r'[A-Za-z0-9./:_-]+@sha256:[a-f0-9]{64}',config['parser_image']):raise ValueError('Pin the parser digest')
 ns=config['namespace'];root=Path(__file__).resolve().parent;objects=[]
 def obj(kind,name,spec=None,**extra):
  x={'apiVersion':'v1' if kind in ('Namespace','ConfigMap','Service','ServiceAccount','ResourceQuota') else 'apps/v1','kind':kind,'metadata':{'name':name,**({} if kind=='Namespace' else {'namespace':ns})},**extra}
  if spec is not None:x['spec']=spec
  objects.append(x);return x
 obj('Namespace',ns,metadata={'name':ns,'labels':{'pod-security.kubernetes.io/enforce':'restricted'}})
 obj('Namespace','enterprise-ai-parser',metadata={'name':'enterprise-ai-parser','labels':{'pod-security.kubernetes.io/enforce':'restricted'}})
 obj('ConfigMap','private-proxy',data={name:(root/'nginx'/name).read_text() for name in ('private-platform.conf','private-reference.conf')})
 obj('ConfigMap','worker-kubeconfig',data={'config':json.dumps({'apiVersion':'v1','kind':'Config','clusters':[{'name':'cluster','cluster':{'server':'https://kubernetes.default.svc','certificate-authority':'/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'}}],'users':[{'name':'worker','user':{'tokenFile':'/var/run/secrets/kubernetes.io/serviceaccount/token'}}],'contexts':[{'name':'current','context':{'cluster':'cluster','user':'worker','namespace':'enterprise-ai-parser'}}],'current-context':'current'})})
 for name in ('platform','reference','worker'):
  obj('ServiceAccount',name,metadata={'name':name,'namespace':ns,'annotations':{'eks.amazonaws.com/role-arn':config[name+'_role_arn']}})
 security={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}}
 pod_security={'runAsNonRoot':True,'runAsUser':65532,'runAsGroup':65532,'fsGroup':65532,'seccompProfile':{'type':'RuntimeDefault'}}
 for name,image,port in [('platform','api',8000),('reference','reference',8010),('worker','worker',None),('web','web',8080)]:
  volumes=[{'name':'tmp','emptyDir':{'sizeLimit':'64Mi'}}];mounts=[{'name':'tmp','mountPath':'/tmp'}]
  container={'name':name,'image':config['registry']+'/eai-'+image+':'+config['image_tag'],'securityContext':security,'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'1','memory':'768Mi'}},'volumeMounts':mounts}
  if name!='web':
   container['envFrom']=[{'secretRef':{'name':'eai-'+name}}]
   container['env']=[{'name':'EAI_INTERNAL_CA_FILE','value':'/etc/eai/ca/ca.crt'}]
   volumes.append({'name':'ca','secret':{'secretName':'eai-private-ca'}});mounts.append({'name':'ca','mountPath':'/etc/eai/ca','readOnly':True})
  containers=[container]
  if port:
   container['ports']=[{'containerPort':port}]
   health='/healthz' if name=='web' else '/health/live';ready='/healthz' if name=='web' else '/health/ready'
   container['startupProbe']={'httpGet':{'path':health,'port':port},'periodSeconds':3,'failureThreshold':30}
   container['livenessProbe']={'httpGet':{'path':health,'port':port},'periodSeconds':15,'failureThreshold':3}
   container['readinessProbe']={'httpGet':{'path':ready,'port':port},'periodSeconds':5,'failureThreshold':2}
  if name in ('platform','reference','web'):
   secret='eai-'+name+'-tls' if name!='web' else 'eai-private-ca'
   volumes.append({'name':'tls','secret':{'secretName':secret,'defaultMode':288}})
   if name=='web':mounts.append({'name':'tls','mountPath':'/etc/eai/tls','readOnly':True});container['securityContext']={**security,'runAsUser':101,'runAsGroup':101};pod_security_web={**pod_security,'fsGroup':101}
   else:
    proxy={'name':'private-tls','image':'nginxinc/nginx-unprivileged:1.28-alpine','ports':[{'containerPort':8443}],'resources':{'requests':{'cpu':'25m','memory':'32Mi'},'limits':{'cpu':'100m','memory':'64Mi'}},'securityContext':{**security,'runAsUser':101,'runAsGroup':101},'volumeMounts':[{'name':'tls','mountPath':'/etc/eai/tls','readOnly':True},{'name':'tmp','mountPath':'/tmp'},{'name':'proxy-config','mountPath':'/etc/nginx/conf.d/default.conf','subPath':'private-'+name+'.conf','readOnly':True}]}
    volumes.append({'name':'proxy-config','configMap':{'name':'private-proxy'}});containers.append(proxy)
  if name=='worker':
   container['env'].append({'name':'EAI_PARSER_IMAGE_URI','value':config['parser_image']})
   volumes.append({'name':'kubeconfig','configMap':{'name':'worker-kubeconfig'}});mounts.append({'name':'kubeconfig','mountPath':'/etc/eai/kubeconfig','subPath':'config','readOnly':True})
  spec={'replicas':config['worker_replicas'] if name=='worker' else config['replicas'],'selector':{'matchLabels':{'app':name}},'strategy':{'type':'RollingUpdate','rollingUpdate':{'maxUnavailable':0,'maxSurge':1}},'template':{'metadata':{'labels':{'app':name}},'spec':{'automountServiceAccountToken':name=='worker','serviceAccountName':name if name!='web' else 'default','securityContext':pod_security_web if name=='web' else pod_security,'terminationGracePeriodSeconds':145,'containers':containers,'volumes':volumes}}}
  if name=='worker':spec['strategy']={'type':'Recreate'}
  obj('Deployment',name,spec)
  if name in ('platform','reference'):obj('Service',name+'-api',{'selector':{'app':name},'ports':[{'port':443,'targetPort':8443}]})
 obj('Service','web',{'type':'LoadBalancer','selector':{'app':'web'},'ports':[{'name':'https','port':443,'targetPort':8080}]},metadata={'name':'web','namespace':ns,'annotations':{'service.beta.kubernetes.io/aws-load-balancer-type':'external','service.beta.kubernetes.io/aws-load-balancer-nlb-target-type':'ip','service.beta.kubernetes.io/aws-load-balancer-scheme':'internet-facing','service.beta.kubernetes.io/aws-load-balancer-ssl-cert':config['certificate_arn'],'service.beta.kubernetes.io/aws-load-balancer-ssl-ports':'443'}})
 # Network isolation, disruption budgets and parser admission are separate static
 # resources rendered from the same reviewed, nonsecret target.
 for name in ('platform','reference','web'):
  objects.append({'apiVersion':'policy/v1','kind':'PodDisruptionBudget','metadata':{'name':name,'namespace':ns},'spec':{'maxUnavailable':1 if config['replicas']==2 else 0,'selector':{'matchLabels':{'app':name}}}})
 objects += [
 {'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':'private-ingress','namespace':ns},'spec':{'podSelector':{},'policyTypes':['Ingress'],'ingress':[{'from':[{'podSelector':{}}]}]}},
 {'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':'public-web','namespace':ns},'spec':{'podSelector':{'matchLabels':{'app':'web'}},'policyTypes':['Ingress'],'ingress':[{'ports':[{'protocol':'TCP','port':8080}]}]}},
 {'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':'no-network','namespace':'enterprise-ai-parser'},'spec':{'podSelector':{},'policyTypes':['Ingress','Egress'],'ingress':[],'egress':[]}},
 {'apiVersion':'v1','kind':'ResourceQuota','metadata':{'name':'parser-bound','namespace':'enterprise-ai-parser'},'spec':{'hard':{'pods':'1','requests.cpu':'1','requests.memory':'512Mi','limits.cpu':'1','limits.memory':'512Mi'}}},
 {'apiVersion':'rbac.authorization.k8s.io/v1','kind':'Role','metadata':{'name':'parser-launcher','namespace':'enterprise-ai-parser'},'rules':[{'apiGroups':[''],'resources':['pods'],'verbs':['create','get','list','delete','watch']},{'apiGroups':[''],'resources':['pods/exec'],'verbs':['create']}]},
 {'apiVersion':'rbac.authorization.k8s.io/v1','kind':'RoleBinding','metadata':{'name':'parser-worker','namespace':'enterprise-ai-parser'},'subjects':[{'kind':'ServiceAccount','name':'worker','namespace':ns}],'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'parser-launcher'}},
 {'apiVersion':'admissionregistration.k8s.io/v1','kind':'ValidatingAdmissionPolicy','metadata':{'name':'eai-isolated-parser'},'spec':{'failurePolicy':'Fail','matchConstraints':{'resourceRules':[{'apiGroups':[''],'apiVersions':['v1'],'operations':['CREATE','UPDATE'],'resources':['pods']}]},'matchConditions':[{'name':'parser-namespace','expression':"request.namespace == 'enterprise-ai-parser'"}],'validations':[{'expression':"object.spec.containers.size() == 1 && object.spec.containers.all(c, c.image == '"+config['parser_image']+"' && (!has(c.env) || c.env.size() == 0) && (!has(c.envFrom) || c.envFrom.size() == 0))",'message':'Only the pinned secret-free parser image is allowed.'},{'expression':"has(object.spec.automountServiceAccountToken) && object.spec.automountServiceAccountToken == false && (!has(object.spec.initContainers) || object.spec.initContainers.size() == 0) && (!has(object.spec.ephemeralContainers) || object.spec.ephemeralContainers.size() == 0) && object.spec.volumes.all(v, has(v.emptyDir))",'message':'No service token, secrets, host data or extra containers.'}]}},
 {'apiVersion':'admissionregistration.k8s.io/v1','kind':'ValidatingAdmissionPolicyBinding','metadata':{'name':'eai-isolated-parser'},'spec':{'policyName':'eai-isolated-parser','validationActions':['Deny']}}]
 return {'apiVersion':'v1','kind':'List','items':objects}

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
 data=render(json.loads(a.config.read_text(encoding='utf-8')));a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
