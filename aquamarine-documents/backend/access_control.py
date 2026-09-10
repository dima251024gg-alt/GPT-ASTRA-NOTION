"""R1 server-side access grants. External subjects never receive staff roles."""
import re
from datetime import datetime,timedelta,timezone
from .db import now
from .schema import SCHEMA
from .security import AppError,blind
STAFF_ROLES=frozenset({'manager','senior_manager','director','accountant','dpo','tech_admin'})
SUBJECT_TYPES=frozenset({'user','external_subject'})
PERMISSIONS=frozenset({'read','write','download','upload'})
GRANTORS=frozenset({'senior_manager','director','dpo'})
TECH_ADMIN_MAX=timedelta(hours=8);EXTERNAL_MAX=timedelta(days=30);DEFAULT_MAX=timedelta(days=90)
def utc(value):
 try:result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
 except(TypeError,ValueError):raise AppError('Некорректный срок доступа')
 if result.tzinfo is None:raise AppError('Срок доступа должен содержать часовой пояс')
 return result.astimezone(timezone.utc)
def validate_staff_role(role):
 if role not in STAFF_ROLES:raise AppError('Неизвестная роль сотрудника')
 return role
def _actor(actor):
 if not actor or not actor.get('active'):raise AppError('Доступ сотрудника отключён',401)
 validate_staff_role(actor.get('role'));return actor
def _can_grant(actor,resource_type,permissions):
 _actor(actor)
 if actor['role'] not in GRANTORS:raise AppError('Недостаточно прав для выдачи доступа',403)
 if actor['role']=='senior_manager' and(resource_type in {'audit_log','access_grants','access_grant_events'} or 'download' in permissions):raise AppError('Такой доступ может выдать только директор или DPO',403)
def create_external_subject(db,actor,kind,person_id=None,client_id=None,contact=None):
 _actor(actor)
 if actor['role'] not in {'manager','senior_manager','director','dpo'}:raise AppError('Недостаточно прав',403)
 if kind not in {'traveler','customer','representative','supplier_contact'}:raise AppError('Неизвестный тип внешнего субъекта')
 if bool(person_id)==bool(client_id):raise AppError('Укажите ровно одну связанную карточку')
 if person_id:db.one('persons',person_id)
 if client_id:db.one('clients',client_id)
 row=db.insert('external_subjects',{'kind':kind,'person_id':person_id,'client_id':client_id,'contact_key':blind(contact) if contact else None,'status':'active'},actor['id']);db.audit(actor['id'],'external_subject_created','external_subjects',row['id'],{'kind':kind});return row
def create_grant(db,actor,subject_type,subject_id,resource_type,resource_id,permissions,expires_at,reason):
 permissions=sorted(set(permissions or []));_can_grant(actor,resource_type,permissions)
 if subject_type not in SUBJECT_TYPES:raise AppError('Неизвестный тип получателя доступа')
 if resource_type not in SCHEMA or resource_type in {'access_grants','access_grant_events'}:raise AppError('Недопустимый тип ресурса')
 if not re.fullmatch(r'[0-9a-fA-F-]{36}',str(resource_id or '')):raise AppError('Доступ выдаётся только к конкретному ресурсу')
 db.one(resource_type,resource_id)
 if not permissions or not set(permissions)<=PERMISSIONS:raise AppError('Недопустимый набор разрешений')
 start=datetime.now(timezone.utc);end=utc(expires_at)
 if end<=start:raise AppError('Срок доступа уже истёк')
 subject=db.one('users',subject_id) if subject_type=='user' else db.one('external_subjects',subject_id)
 if subject_type=='user':validate_staff_role(subject.get('role'))
 elif subject.get('status')!='active':raise AppError('Внешний получатель отключён',409)
 maximum=TECH_ADMIN_MAX if subject_type=='user' and subject.get('role')=='tech_admin' else(EXTERNAL_MAX if subject_type=='external_subject' else DEFAULT_MAX)
 if end-start>maximum:raise AppError('Срок доступа превышает допустимый максимум')
 if subject_type=='external_subject' and not set(permissions)<={'read','download','upload'}:raise AppError('Внешнему получателю нельзя выдавать внутреннее право изменения')
 if len(str(reason or '').strip())<12:raise AppError('Укажите обоснование временного доступа')
 row=db.insert('access_grants',{'subject_type':subject_type,'subject_id':subject_id,'resource_type':resource_type,'resource_id':resource_id,'permissions':permissions,'valid_from':start.isoformat(),'expires_at':end.isoformat(),'granted_by':actor['id'],'revoked_at':None,'reason':reason.strip()},actor['id']);db.insert('access_grant_events',{'grant_id':row['id'],'event':'granted','actor_id':actor['id'],'reason':reason.strip(),'occurred_at':now()},actor['id']);db.audit(actor['id'],'access_granted',resource_type,resource_id,{'subject_type':subject_type,'permissions':permissions,'expires_at':end.isoformat()});return row
def grant_allows(db,subject_type,subject_id,resource_type,resource_id,permission,at=None):
 if permission not in PERMISSIONS:return False
 point=at or datetime.now(timezone.utc)
 if point.tzinfo is None:point=point.replace(tzinfo=timezone.utc)
 for grant in db.all('access_grants','subject_type=? AND subject_id=? AND resource_type=? AND resource_id=?',(subject_type,subject_id,resource_type,resource_id)):
  if not grant.get('revoked_at') and utc(grant['valid_from'])<=point.astimezone(timezone.utc)<utc(grant['expires_at']) and permission in(grant.get('permissions') or []):return True
 return False
def revoke_grant(db,actor,grant_id,reason):
 grant=db.one('access_grants',grant_id);_can_grant(actor,grant['resource_type'],grant.get('permissions') or [])
 if grant.get('revoked_at'):return grant
 if len(str(reason or '').strip())<12:raise AppError('Укажите причину отзыва доступа')
 revoked=now();db.update('access_grants',grant_id,{'revoked_at':revoked});db.insert('access_grant_events',{'grant_id':grant_id,'event':'revoked','actor_id':actor['id'],'reason':reason.strip(),'occurred_at':revoked},actor['id']);db.audit(actor['id'],'access_revoked',grant['resource_type'],grant['resource_id'],{'subject_type':grant['subject_type']});return db.one('access_grants',grant_id)
def link_client_person(db,actor,client_id,person_id,relation_type='traveler',is_primary=False,valid_to=None):
 _actor(actor)
 if actor['role'] not in {'manager','senior_manager','director','dpo'}:raise AppError('Недостаточно прав',403)
 client=db.one('clients',client_id);db.one('persons',person_id)
 if actor['role']=='manager' and client.get('manager_id')!=actor['id']:raise AppError('Нельзя связывать карточки другого менеджера',403)
 if relation_type not in {'customer','traveler','representative','family','payer'}:raise AppError('Неизвестный тип связи')
 existing=db.find('client_person_relations','client_id=? AND person_id=? AND relation_type=?',(client_id,person_id,relation_type))
 if is_primary:
  primary=db.find('client_person_relations','client_id=? AND is_primary=? AND status=?',(client_id,1,'active'))
  if primary and primary['person_id']!=person_id:raise AppError('Основное лицо уже задано. Сначала подтвердите его замену отдельным действием',409)
 verified=now()
 if existing:
  if is_primary and not existing.get('is_primary'):
   existing=db.update('client_person_relations',existing['id'],{'is_primary':1,'status':'active','source':'manual','verified_by':actor['id'],'verified_at':verified})
   db.update('clients',client_id,{'person_id':person_id})
   db.audit(actor['id'],'client_person_primary_confirmed','client_person_relations',existing['id'],{'relation_type':relation_type})
  return existing
 row=db.insert('client_person_relations',{'client_id':client_id,'person_id':person_id,'relation_type':relation_type,'is_primary':int(bool(is_primary)),'status':'active','valid_from':verified,'valid_to':valid_to,'source':'manual','verified_by':actor['id'],'verified_at':verified},actor['id'])
 if is_primary:db.update('clients',client_id,{'person_id':person_id})
 db.audit(actor['id'],'client_person_linked','client_person_relations',row['id'],{'relation_type':relation_type,'is_primary':bool(is_primary)});return row
