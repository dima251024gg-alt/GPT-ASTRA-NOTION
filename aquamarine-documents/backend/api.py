"""HTTP-independent API. FastAPI and the offline server call the same dispatcher."""
import json,os,re,traceback
from dataclasses import dataclass,field
from http.cookies import SimpleCookie
from urllib.parse import parse_qs,urlsplit
from . import config
from .access_control import create_external_subject,create_grant,link_client_person,revoke_grant,validate_staff_role
from .db import Database,now
from .security import AppError,blind,hash_password,new_totp_secret,totp
from .service import STATUSES,Service,authenticate,login,safe_user
@dataclass
class Response:
 data:object=field(default_factory=dict);status:int=200;headers:dict=field(default_factory=dict)
def cookie(value,max_age=43200):return 'aq_session='+value+'; HttpOnly; SameSite=Strict; Path=/; Max-Age='+str(max_age)+('; Secure' if config.PRODUCTION else '')
class Application:
 def __init__(self,url=None):self.url=url
 def handle(self,method,target,headers=None,body=b'',ip='127.0.0.1'):
  headers={key.lower():value for key,value in(headers or {}).items()};path=urlsplit(target).path;query=parse_qs(urlsplit(target).query)
  if len(body)>30*1024*1024:return Response({'message':'Файл слишком большой'},413)
  try:data=json.loads(body) if body else {}
  except(ValueError,UnicodeDecodeError):return Response({'message':'Некорректный запрос'},400)
  if not isinstance(data,dict):return Response({'message':'Ожидается объект запроса'},400)
  db=Database(self.url)
  try:
   with db.atomic():
    try:result=self.dispatch(db,method,path,query,headers,data,ip)
    except AppError as error:
     if error.status not in(401,403,429) and not getattr(error,'persist',False):raise
     result=Response({'message':error.message,'details':error.details},error.status)
   return result if isinstance(result,Response) else Response(result)
  except AppError as error:return Response({'message':error.message,'details':error.details},error.status)
  except(ValueError,TypeError,KeyError):
   if not config.PRODUCTION:traceback.print_exc()
   return Response({'message':'Проверьте обязательные поля и формат значений'},422)
  except Exception as error:
   if not config.PRODUCTION:traceback.print_exc()
   if 'UNIQUE' in str(error) or 'unique constraint' in str(error):return Response({'message':'Такая запись уже существует. Обновите страницу.'},409)
   return Response({'message':'Не удалось выполнить действие. Повторите позже или обратитесь к администратору.'},500)
  finally:db.close()
 def dispatch(self,db,method,path,q,h,data,ip):
  if path=='/api/health':return {'ok':True,'mode':config.MODE,'region':config.REGION,'demo':os.getenv('DEMO_MODE')=='1' and not config.PRODUCTION}
  if path=='/api/demo/access' and method=='GET':
   if config.PRODUCTION or os.getenv('DEMO_MODE')!='1' or ip not in('127.0.0.1','::1'):raise AppError('Страница не найдена',404)
   credentials=json.loads((config.VAR/'demo-credentials.json').read_text());user=db.find('users','email=?',(credentials['email'],));return {**credentials,'code':totp(user['totp_secret'])}
  if path=='/api/auth/login' and method=='POST':
   result,session_token=login(db,data.get('email',''),data.get('password',''),data.get('code',''),ip,h.get('user-agent',''));return Response(result,200,{'Set-Cookie':cookie(session_token)})
  from .portal import public_portal
  public=public_portal(db,method,path,data,ip,h)
  if public is not None:return public
  cookies=SimpleCookie();cookies.load(h.get('cookie',''));session_token=cookies['aq_session'].value if 'aq_session' in cookies else '';user,session=authenticate(db,session_token,h.get('x-csrf-token'),method not in('GET','HEAD'))
  if method not in('GET','HEAD') and h.get('origin') and h['origin']!=config.PUBLIC_URL:raise AppError('Запрос с другого сайта запрещён',403)
  if path=='/api/auth/me':return {'user':safe_user(user) if user else None,'portal':not bool(user),'csrf':blind(session_token+':csrf')}
  if path=='/api/auth/logout' and method=='POST':db.update('sessions',session['id'],{'revoked_at':now()});return Response({'ok':True},200,{'Set-Cookie':cookie('',0)})
  if not user:
   from .portal import portal_dispatch
   return portal_dispatch(db,session,method,path,data,ip,h)
  service=Service(db,user,ip,h.get('user-agent',''))
  if path=='/api/bootstrap':return {'user':safe_user(user),'statuses':STATUSES,'countries':service.list('countries',limit=100)['items'],'operators':service.list('operators',limit=100)['items'],'templates':service.list('templates',limit=100)['items']}
  if path=='/api/users' and method=='POST':
   service.require('director');role=validate_staff_role(data.get('role'));secret=new_totp_secret();created=db.insert('users',{'full_name':data['full_name'],'email':data['email'].lower(),'phone':data.get('phone'),'role':role,'active':1,'two_fa_enabled':1,'password_hash':hash_password(data['password']),'totp_secret':secret,'last_totp_step':-1},service.actor);service.audit('user_created','users',created['id']);return {'user':safe_user(created),'enrollment_secret':secret,'enrollment_uri':f"otpauth://totp/Aquamarine:{created['email']}?secret={secret}&issuer=Aquamarine"}
  match=re.fullmatch(r'/api/users/([^/]+)/disable',path)
  if match and method=='POST':
   service.require('director');user_id=match[1]
   if user_id==service.actor:raise AppError('Нельзя отключить собственную учётную запись')
   db.update('users',user_id,{'active':0})
   for active_session in db.all('sessions','user_id=?',(user_id,)):db.update('sessions',active_session['id'],{'revoked_at':now()})
   service.audit('user_disabled','users',user_id);return {'ok':True}
  if path=='/api/external-subjects' and method=='POST':return Response(create_external_subject(db,user,data.get('kind'),data.get('person_id'),data.get('client_id'),data.get('contact')),201)
  if path=='/api/access-grants' and method=='POST':
   grant=create_grant(db,user,data.get('subject_type'),data.get('subject_id'),data.get('resource_type'),data.get('resource_id'),data.get('permissions'),data.get('expires_at'),data.get('reason'));return Response(service.public('access_grants',grant),201)
  match=re.fullmatch(r'/api/access-grants/([^/]+)/revoke',path)
  if match and method=='POST':return service.public('access_grants',revoke_grant(db,user,match[1],data.get('reason')))
  if path=='/api/client-person-relations' and method=='POST':
   relation=link_client_person(db,user,data.get('client_id'),data.get('person_id'),data.get('relation_type','traveler'),bool(data.get('is_primary')),data.get('valid_to'));return Response(service.public('client_person_relations',relation),201)
  if path=='/api/persons/separate' and method=='POST':return service.separate_person(data['person'],data.get('reason',''))
  if path=='/api/audit/verify':service.require('director','dpo');return {'valid':db.verify_audit()}
  from .features import dispatch_features
  result=dispatch_features(service,method,path,q,data)
  if result is not None:return result
  parts=path.removeprefix('/api/').split('/');table=parts[0]
  if len(parts)==1:
   if method=='GET':return service.list(table,q.get('q',[''])[0],int(q.get('limit',['50'])[0]),int(q.get('offset',['0'])[0]))
   if method=='POST':return Response(service.create(table,data),201)
  if len(parts)==2:
   item_id=parts[1]
   if method=='GET':return service.public(table,service.get(table,item_id),q.get('reveal',['0'])[0]=='1')
   if method=='PATCH':return service.update(table,item_id,data)
   if method=='DELETE':return service.remove(table,item_id)
  raise AppError('Страница не найдена',404)
