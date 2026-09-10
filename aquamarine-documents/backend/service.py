"""Application services with server-side role and resource-grant authorization."""
import hmac,re,uuid
from datetime import date,datetime,timedelta,timezone
from . import config
from .access_control import grant_allows,link_client_person,validate_staff_role
from .db import now
from .schema import COLUMNS,INT_FIELDS
from .security import AppError,blind,check_totp,mask,phone,token,verify_password
ROLE_ALIASES={'admin':'director','senior':'senior_manager'}
STATUSES=['Новый лид','Подбор тура','Подборка отправлена','Согласован вариант','Собираем документы','Заявка отправлена оператору','Забронировано (ожидает оплаты)','Оплачено частично','Оплачено полностью','Документы на вылет выданы','В поездке','Завершено','Аннулировано']
DIRECTORY={'operators','countries','hotels','templates','notification_templates'}
CRUD={'clients','persons','client_person_relations','identity_documents','addresses','deals','deal_persons','tasks'}|DIRECTORY
IMMUTABLE={'password_hash','totp_secret','last_totp_step','failed_attempts','locked_until','identity_key','document_key','contact_key','paid_client','paid_operator','revision','processing_blocked','deletion_due','legal_hold','status','number','verification_status','manual_mrz_review'}
REQUIRED={'clients':['full_name','phone'],'persons':['last_name_ru','first_name_ru','birth_date'],'client_person_relations':['client_id','person_id','relation_type'],'identity_documents':['person_id','type','number'],'deals':['client_id','country_id','operator_id','date_from','date_to'],'deal_persons':['deal_id','person_id'],'operators':['full_name','short_name','registry_number'],'countries':['code','name'],'templates':['name','type','body'],'tasks':['title','due_at'],'addresses':['person_id','type']}
FINANCIAL_DOCS={'invoice','receipt','act','agent_report'}
def normalize_role(role):return ROLE_ALIASES.get(role,role)
def normalized_user(user):return {**user,'role':normalize_role(user.get('role'))} if user else None
def dt(value):return datetime.fromisoformat(value.replace('Z','+00:00'))
def as_date(value):return date.fromisoformat(str(value)[:10])
def age(birth,on):
 b=as_date(birth);d=as_date(on);return d.year-b.year-((d.month,d.day)<(b.month,b.day))
def fio(person):return ' '.join(str(person.get(key) or '') for key in('last_name_ru','first_name_ru','patronymic_ru')).strip()
def identity_key(person):return blind('|'.join(re.sub(r'\s+',' ',str(person.get(key) or '').strip().upper().replace('Ё','Е')) for key in('last_name_ru','first_name_ru','patronymic_ru','birth_date')))
def document_key(document):return blind((document.get('issuing_country') or 'RUS')+'|'+document['type']+'|'+re.sub(r'\W','',str(document.get('series') or '')+str(document['number'])).upper())
def safe_user(user):
 user=normalized_user(user);return {key:user.get(key) for key in('id','full_name','email','role','active','two_fa_enabled','last_login')}
class Service:
 def __init__(self,db,user=None,ip='',agent=''):
  self.db=db;self.user=normalized_user(user);self.ip=ip;self.agent=agent
  if self.user:validate_staff_role(self.user.get('role'))
 @property
 def actor(self):return self.user['id'] if self.user else None
 def audit(self,action,table,id,details=None):return self.db.audit(self.actor,action,table,id,details,self.ip,self.agent)
 def require(self,*roles):
  expected={normalize_role(role) for role in roles}
  if not self.user or self.user['role'] not in expected:self.audit('access_denied','roles',None,{'required':sorted(expected)});raise AppError('Недостаточно прав для этого действия',403)
 def staff(self):
  if not self.user:raise AppError('Войдите в систему',401)
  validate_staff_role(self.user['role'])
 def granted(self,table,obj,permission='read'):return bool(self.user and obj.get('id') and grant_allows(self.db,'user',self.actor,table,obj['id'],permission))
 def can(self,table,obj):
  if not self.user:return False
  if self.granted(table,obj,'read'):return True
  role=self.user['role']
  if role in('director','senior_manager'):return True
  if role=='dpo' and table in {'clients','persons','client_person_relations','identity_documents','addresses','consents','audit_log','access_grants','access_grant_events','external_subjects','files'}:return True
  if table in DIRECTORY:return role!='tech_admin'
  if table=='users':return obj['id']==self.actor
  if table=='clients':return role=='accountant' or obj.get('manager_id')==self.actor
  if table=='deals':return role=='accountant' or obj.get('manager_id')==self.actor
  if table=='persons':
   relations=self.db.all('client_person_relations','person_id=? AND status=?',(obj['id'],'active'))
   if any(self.can('clients',self.db.one('clients',relation['client_id'])) for relation in relations):return True
   return bool(obj.get('client_id') and self.can('clients',self.db.one('clients',obj['client_id'])))
  if table=='client_person_relations':return self.can('clients',self.db.one('clients',obj['client_id']))
  if table in('identity_documents','addresses','consents'):return self.can('persons',self.db.one('persons',obj['person_id']))
  if table=='notifications':return obj.get('user_id')==self.actor
  if table=='files':
   target=obj.get('entity_type');target_id=obj.get('entity_id');return self.can(target,self.db.one(target,target_id)) if target in('deals','persons','clients') and target_id else obj.get('uploaded_by')==self.actor
  if obj.get('deal_id'):return self.can('deals',self.db.one('deals',obj['deal_id']))
  if table=='tasks':return obj.get('assignee_id')==self.actor
  return False
 def get(self,table,id,processing=False):
  obj=self.db.one(table,id)
  if not self.can(table,obj):self.audit('access_denied',table,id);raise AppError('Запись не найдена или недоступна',404)
  if processing:
   persons=[]
   if table=='persons':persons=[obj]
   if table=='identity_documents':persons=[self.db.one('persons',obj['person_id'])]
   if table=='deals':persons=[self.db.one('persons',item['person_id']) for item in self.db.all('deal_persons','deal_id=?',(id,))]
   if any(person.get('processing_blocked') for person in persons):raise AppError('Обработка данных остановлена: согласие отозвано',409)
  return obj
 def public(self,table,obj,reveal=False):
  out={key:value for key,value in obj.items() if key not in('password_hash','totp_secret','token_hash','csrf_hash','sms_code_hash','code_hash','identity_key','document_key','contact_key','storage_path')}
  if table=='users':return safe_user(obj)
  if table=='persons':out['full_name']=fio(obj);out['is_minor']=age(obj['birth_date'],date.today())<18 if obj.get('birth_date') else False;out.pop('client_id',None)
  if table=='identity_documents':
   out['series']=mask(obj.get('series'));out['number']=mask(obj.get('number'))
   for key in('mrz_raw','issued_by','division_code','confidence'):out.pop(key,None)
   if reveal and self.user['role'] not in('accountant','tech_admin'):out.update({key:obj.get(key) for key in('series','number','mrz_raw','issued_by','division_code','confidence')});self.audit('passport_view',table,obj['id'],{'fields':['series','number','mrz_raw','issued_by']})
  if self.user and self.user['role']=='accountant':
   if table=='clients':
    for key in('birth_date','registration_address','comment','messenger_username'):out.pop(key,None)
   if table=='persons':out={key:value for key,value in out.items() if key in('id','full_name','is_minor')}
   if table=='contracts':
    for key in('evidence','signer_phone','signer_ip','pdf_file_id','signed_file_id'):out.pop(key,None)
  return out
 def list(self,table,query='',limit=50,offset=0):
  self.staff()
  if table=='audit_log':self.require('director','dpo')
  allowed=CRUD|{'users','documents','contracts','consents','payments','notifications','audit_log','packages','messages','template_versions','external_subjects','access_grants','access_grant_events'}
  if table not in allowed:raise AppError('Раздел не найден',404)
  rows=[]
  for row in self.db.all(table):
   if table=='audit_log' or self.can(table,row):
    item=self.public(table,row)
    if not query or query.casefold() in str(item).casefold():rows.append(item)
  if table=='users':rows=[safe_user(row) for row in self.db.all('users')] if self.user['role'] in('director','senior_manager') else [safe_user(self.user)]
  return {'items':rows[offset:offset+min(int(limit),100)],'total':len(rows)}
 def validate_input(self,table,data,existing=None):
  if not isinstance(data,dict):raise AppError('Ожидается объект с полями')
  for key,value in data.items():
   if key not in COLUMNS[table]:raise AppError('Недопустимое поле: '+key)
   if key in INT_FIELDS[table] and value is not None and(isinstance(value,bool) or not isinstance(value,int)):raise AppError('Поле '+key+' должно быть целым числом')
   if isinstance(value,str) and len(value)>100000:raise AppError('Слишком длинное значение: '+key)
   if key.endswith('_id') and value:
    try:uuid.UUID(value)
    except(ValueError,TypeError):raise AppError('Некорректная ссылка на запись')
  merged={**(existing or {}),**data};missing=[key for key in REQUIRED.get(table,[]) if merged.get(key) in(None,'')]
  if missing:raise AppError('Заполните обязательные поля',422,missing)
  for key in('date_from','date_to','birth_date','issue_date','expiry_date','guarantee_from','guarantee_to'):
   if merged.get(key):
    try:as_date(merged[key])
    except(ValueError,TypeError):raise AppError('Некорректная дата: '+key)
  for key in('operator_deadline','client_deadline','due_at'):
   if merged.get(key):
    try:
     if dt(merged[key]).tzinfo is None:raise ValueError()
    except(ValueError,TypeError):raise AppError('Укажите дату, время и часовой пояс: '+key)
  for key in('client_price','operator_net','commission','guarantee_amount','adults','children','infants'):
   if merged.get(key) is not None and(merged[key]<0 or merged[key]>10**14):raise AppError('Недопустимое значение: '+key)
  if merged.get('birth_date') and as_date(merged['birth_date'])>date.today():raise AppError('Дата рождения не может быть в будущем')
  if table=='identity_documents' and merged['type'] not in('passport_rf','international_passport','birth_certificate','visa','residence_permit'):raise AppError('Неизвестный тип документа')
  if table=='clients' and merged.get('type','individual') not in('individual','company'):raise AppError('Неизвестный тип заказчика')
  if table=='deals' and as_date(merged['date_to'])<as_date(merged['date_from']):raise AppError('Возвращение не может быть раньше вылета')
  if merged.get('phone'):data['phone']=phone(merged['phone'])
  return data
 def create(self,table,data):
  self.staff()
  if table not in CRUD:raise AppError('Создание этой записи недоступно',403)
  if table in DIRECTORY:self.require('director','senior_manager')
  else:self.require('director','senior_manager','manager')
  data=dict(data)
  if table=='client_person_relations':return self.public(table,link_client_person(self.db,self.user,data.get('client_id'),data.get('person_id'),data.get('relation_type','traveler'),bool(data.get('is_primary')),data.get('valid_to')))
  link_client_id=data.pop('client_id',None) if table=='persons' else None
  for field in set(data)&IMMUTABLE:raise AppError('Поле изменяется отдельным действием: '+field)
  self.validate_input(table,data)
  if table=='clients':data['manager_id']=self.actor if self.user['role']=='manager' else data.get('manager_id',self.actor);data.update(status='лид',type=data.get('type','individual'),contact_key=blind(data['phone']),tags=data.get('tags',[]))
  if table=='persons':
   if link_client_id:self.get('clients',link_client_id)
   data.update(identity_key=identity_key(data),processing_blocked=0,legal_hold=0);twins=self.db.all('persons','identity_key=?',(data['identity_key'],))
   if twins:raise AppError('Найден возможный дубль. Выберите существующую карточку или подтвердите однофамильца.',409,{'duplicates':[{'id':person['id'],'full_name':fio(person)} for person in twins if self.can('persons',person)],'separate_allowed':True})
  if table in('identity_documents','addresses'):self.get('persons',data['person_id'],True)
  if table=='identity_documents':
   data.update(document_key=document_key(data),verification_status='распознан',confidence={},is_primary=data.get('is_primary',1))
   if self.db.find(table,'document_key=?',(data['document_key'],)):raise AppError('Документ уже зарегистрирован. Используйте существующего туриста.',409)
  if table=='deals':
   client=self.get('clients',data['client_id']);manager=client['manager_id']
   if self.user['role']=='manager' and manager!=self.actor:raise AppError('Нельзя создать сделку чужого заказчика',403)
   country=self.db.one('countries',data['country_id']);self.db.one('operators',data['operator_id']);year=str(date.today().year);counter=self.db.find('counters','name=?',(year,));counter=self.db.update('counters',counter['id'],{'value':counter['value']+1}) if counter else self.db.insert('counters',{'name':year,'value':1});data.update(number=f"АКВ-{year}-{counter['value']:05d}",manager_id=manager,status=STATUSES[0],nights=(as_date(data['date_to'])-as_date(data['date_from'])).days,cross_border=int(country['code']!='RU'),revision=1,paid_client=0,paid_operator=0)
   for key,value in {'currency':'RUB','exchange_rate':'1','adults':1,'children':0,'infants':0,'client_price':0,'operator_net':0,'commission':0,'tour_type':'package','recipients':[]}.items():data.setdefault(key,value)
  if table=='deal_persons':
   deal=self.get('deals',data['deal_id'],True);person=self.get('persons',data['person_id'],True);linked=self.db.find('client_person_relations','client_id=? AND person_id=? AND status=?',(deal['client_id'],person['id'],'active'))
   if not linked and person.get('client_id')!=deal['client_id']:raise AppError('Турист должен быть связан с заказчиком сделки')
   data.setdefault('role','турист');data.setdefault('tariff','adult');data.setdefault('parents_count',0)
  if table=='tasks':
   if data.get('deal_id'):self.get('deals',data['deal_id'])
   if data.get('assignee_id') and self.user['role']=='manager' and data['assignee_id']!=self.actor:raise AppError('Нельзя назначать задачи другому сотруднику',403)
   data.update(status='open',automatic=0);data.setdefault('assignee_id',self.actor)
  if table=='templates':data.update(version=1,active=1,changed_by=self.actor,format=data.get('format','html'))
  obj=self.db.insert(table,data,self.actor)
  if table=='persons' and link_client_id:link_client_person(self.db,self.user,link_client_id,obj['id'])
  if table=='templates':self.db.insert('template_versions',{'template_id':obj['id'],'version':1,'body':obj['body'],'required_fields':obj.get('required_fields',[]),'changed_by':self.actor},self.actor)
  if table=='clients' and data['type']=='individual' and data.get('birth_date'):
   parts=data['full_name'].split();person_data={'last_name_ru':parts[0],'first_name_ru':' '.join(parts[1:2]) or parts[0],'patronymic_ru':' '.join(parts[2:]),'birth_date':data['birth_date'],'phone':data['phone'],'citizenship':'RUS','relationship':'заказчик','processing_blocked':0,'legal_hold':0};person_data['identity_key']=identity_key(person_data);person=self.db.insert('persons',person_data,self.actor);link_client_person(self.db,self.user,obj['id'],person['id'],'customer',True)
  self.audit('create',table,obj['id'],{'fields':list(data)})
  if table=='deal_persons':self.bump_deal(data['deal_id'])
  return self.public(table,obj)
 def bump_deal(self,id):
  deal=self.db.one('deals',id);self.db.update('deals',id,{'revision':(deal.get('revision') or 0)+1})
  for contract in self.db.all('contracts','deal_id=?',(id,)):
   if contract['status'] in('черновик','отправлен на подпись'):self.db.update('contracts',contract['id'],{'status':'аннулирован'})
  for link in self.db.all('portal_links','deal_id=?',(id,)):
   if link.get('package_id'):self.db.update('portal_links',link['id'],{'revoked_at':now()})
 def update(self,table,id,data):
  if table not in CRUD:raise AppError('Изменение этой записи недоступно',403)
  obj=self.get(table,id,table in('persons','deals','identity_documents'));has_grant=self.granted(table,obj,'write')
  if not has_grant:self.require('director','senior_manager') if table in DIRECTORY else self.require('director','senior_manager','manager')
  forbidden=(set(data)&IMMUTABLE)|(set(data)&{'client_id','person_id','deal_id','manager_id','is_primary'})
  if table=='tasks':forbidden.discard('status')
  if forbidden:raise AppError('Поле изменяется отдельным действием: '+', '.join(sorted(forbidden)))
  self.validate_input(table,data,obj)
  if table=='deals':
   if obj['status'] not in STATUSES[:5]:raise AppError('После отправки оператору используйте дополнительное соглашение',409)
   merged={**obj,**data};data={**data,'nights':(as_date(merged['date_to'])-as_date(merged['date_from'])).days,'cross_border':int(self.db.one('countries',merged['country_id'])['code']!='RU'),'revision':obj['revision']+1}
  if table=='persons':
   merged={**obj,**data};data['identity_key']=identity_key(merged)
   if self.db.all('persons','identity_key=? AND id<>?',(data['identity_key'],id)):raise AppError('Обнаружен возможный дубль туриста',409)
  if table=='identity_documents':
   merged={**obj,**data};data.update(document_key=document_key(merged),verification_status='распознан')
   if self.db.find(table,'document_key=? AND id<>?',(data['document_key'],id)):raise AppError('Документ уже зарегистрирован',409)
  if table=='templates':
   data={**data,'version':obj['version']+1,'changed_by':self.actor};merged={**obj,**data};self.db.insert('template_versions',{'template_id':id,'version':merged['version'],'body':merged['body'],'required_fields':merged['required_fields'],'changed_by':self.actor},self.actor)
  self.db.update(table,id,data);self.audit('update',table,id,{'fields':list(data)})
  if table=='deals':self.bump_deal(id)
  if table in('persons','identity_documents'):
   person_id=id if table=='persons' else obj['person_id']
   for participant in self.db.all('deal_persons','person_id=?',(person_id,)):self.bump_deal(participant['deal_id'])
  return self.public(table,self.db.one(table,id))
 def remove(self,table,id):
  obj=self.get(table,id);has_grant=self.granted(table,obj,'write')
  if not has_grant:self.require('director','senior_manager','manager')
  if table not in('tasks','addresses','hotels'):raise AppError('Для этой сущности используйте аннуляцию или раздел персональных данных',403)
  self.db.soft_delete(table,id);self.audit('soft_delete',table,id);return {'ok':True}
 def separate_person(self,data,reason):
  self.require('director','senior_manager','manager')
  if len(reason.strip())<10:raise AppError('Укажите основание: это другой человек с совпадающими ФИО и датой рождения')
  data=dict(data);client_id=data.pop('client_id',None)
  if client_id:self.get('clients',client_id)
  self.validate_input('persons',data)
  if set(data)&IMMUTABLE:raise AppError('Недопустимые поля')
  obj=self.db.insert('persons',{**data,'identity_key':identity_key(data),'processing_blocked':0},self.actor)
  if client_id:link_client_person(self.db,self.user,client_id,obj['id'])
  self.audit('namesake_confirmed','persons',obj['id'],{'reason':reason});return self.public('persons',obj)
def rate_limit(db,key,limit,seconds=60):
 import time
 current=int(time.time());hashed=blind(key);row=db.find('rate_limits','key=?',(hashed,))
 if not row:db.insert('rate_limits',{'key':hashed,'window_start':current,'hits':1});return
 if current-row['window_start']>=seconds:db.update('rate_limits',row['id'],{'window_start':current,'hits':1});return
 if row['hits']>=limit:raise AppError('Слишком много попыток. Повторите позже.',429)
 db.update('rate_limits',row['id'],{'hits':row['hits']+1})
def login(db,email,password,code,ip='',agent=''):
 rate_limit(db,'login:'+ip,20,900);user=db.find('users','email=?',(email.strip().lower(),))
 if not user or not user.get('active') or not verify_password(password,user.get('password_hash')):db.audit(None,'login_failed','users',user['id'] if user else None,{},ip,agent);raise AppError('Неверные данные входа',401)
 user=normalized_user(user)
 if not user.get('two_fa_enabled'):raise AppError('Сначала активируйте 2FA у администратора',401)
 step=check_totp(user['totp_secret'],code,user.get('last_totp_step') or -1);db.update('users',user['id'],{'last_totp_step':step,'last_login':now()});session_token=token();csrf=blind(session_token+':csrf');db.insert('sessions',{'user_id':user['id'],'token_hash':blind(session_token),'csrf_hash':blind(csrf),'last_seen':now(),'expires_at':(datetime.now(timezone.utc)+timedelta(hours=12)).isoformat(),'ip':ip,'user_agent':agent});db.audit(user['id'],'login','users',user['id'],{},ip,agent);return {'user':safe_user(user),'csrf':csrf},session_token
def authenticate(db,session_token,csrf=None,write=False):
 session=db.find('sessions','token_hash=?',(blind(session_token),)) if session_token else None
 if not session or session.get('revoked_at'):raise AppError('Войдите в систему',401)
 current=datetime.now(timezone.utc)
 if current>dt(session['expires_at']) or current-dt(session['last_seen'])>timedelta(minutes=config.SESSION_MINUTES):raise AppError('Сессия завершена после периода бездействия',401)
 if write and not hmac.compare_digest(blind(csrf or ''),session['csrf_hash']):raise AppError('Обновите страницу и повторите действие',403)
 db.update('sessions',session['id'],{'last_seen':now()})
 if session.get('user_id'):
  user=normalized_user(db.one('users',session['user_id']))
  if not user.get('active') or not user.get('two_fa_enabled'):raise AppError('Доступ сотрудника отключён',401)
  validate_staff_role(user['role']);return user,session
 return None,session
