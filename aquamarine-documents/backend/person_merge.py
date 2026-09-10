"""Manual, reversible person deduplication with immutable evidence."""
import re
from .db import now
from .security import AppError
PROPOSE_ROLES=('manager','senior_manager','director','dpo');CONFIRM_ROLES=('senior_manager','director','dpo');REVERT_ROLES=('director','dpo');OPEN_STATUSES=('proposed','confirmed')
# Evidence references stay on the retained source card; evidence is never rewritten.
PERSON_REFERENCES=(('clients','person_id'),('client_person_relations','person_id'),('external_subjects','person_id'),('identity_documents','person_id'),('addresses','person_id'),('quarantine_files','person_id'),('deal_persons','person_id'),('deal_persons','legal_representative_id'),('documents','person_id'),('consents','person_id'),('consents','representative_id'),('portal_links','person_id'),('otp_challenges','person_id'))
MERGE_ACTION=re.compile(r'/api/person-merges/([^/]+)/(confirm|revert)');MERGE_ITEM=re.compile(r'/api/person-merges/([^/]+)')
def _reason(value,action):
 result=str(value or '').strip()
 if len(result)<12:raise AppError('Укажите подробное основание: '+action)
 return result
def _visible_person(service,person_id):
 person=service.db.one('persons',person_id)
 if not service.can('persons',person):raise AppError('Запись не найдена или недоступна',404)
 return person
def _event(db,merge_id,event,actor_id,reason,snapshot=None):return db.insert('person_merge_events',{'merge_id':merge_id,'event':event,'actor_id':actor_id,'reason':reason,'snapshot':snapshot or {},'occurred_at':now()},actor_id)
def public_merge(merge):
 snapshot=merge.get('snapshot') or {};return {key:merge.get(key) for key in('id','source_person_id','target_person_id','status','proposed_by','proposed_at','confirmed_by','confirmed_at','reverted_by','reverted_at')}|{'reference_count':len(snapshot.get('references') or [])}
def _open_merges(db,source_id,target_id,exclude_id=None):
 rows=db.all('person_merges','status IN (?,?) AND (source_person_id IN (?,?) OR target_person_id IN (?,?))',(*OPEN_STATUSES,source_id,target_id,source_id,target_id));return [row for row in rows if row['id']!=exclude_id]
def propose_person_merge(service,source_id,target_id,reason):
 service.require(*PROPOSE_ROLES);reason=_reason(reason,'почему карточки относятся к одному человеку')
 if not source_id or source_id==target_id:raise AppError('Для объединения выберите две разные карточки')
 db=service.db
 with db.atomic():
  source=_visible_person(service,source_id);target=_visible_person(service,target_id)
  if source.get('record_state')=='merged' or target.get('record_state')=='merged':raise AppError('Нельзя использовать уже объединённую карточку',409)
  if _open_merges(db,source_id,target_id):raise AppError('Для одной из карточек уже есть незавершённое объединение',409)
  proposed_at=now();merge=db.insert('person_merges',{'source_person_id':source_id,'target_person_id':target_id,'proposed_by':service.actor,'proposed_at':proposed_at,'proposal_reason':reason,'status':'proposed','confirmed_by':None,'confirmed_at':None,'confirmation_reason':None,'reverted_by':None,'reverted_at':None,'revert_reason':None,'snapshot':{}},service.actor);_event(db,merge['id'],'proposed',service.actor,reason);service.audit('person_merge_proposed','person_merges',merge['id'],{'source_person_id':source_id,'target_person_id':target_id});return public_merge(merge)
def _merge_conflicts(db,source_id,target_id):
 conflicts=[]
 for relation in db.all('client_person_relations','person_id=?',(source_id,)):
  duplicate=db.find('client_person_relations','client_id=? AND person_id=? AND relation_type=?',(relation['client_id'],target_id,relation['relation_type']))
  if duplicate:conflicts.append({'code':'duplicate_client_relation','table':'client_person_relations','row_id':relation['id']})
  if relation.get('is_primary'):
   primary=db.find('client_person_relations','client_id=? AND is_primary=? AND status=?',(relation['client_id'],1,'active'))
   if primary and primary['id']!=relation['id']:conflicts.append({'code':'primary_person_conflict','table':'client_person_relations','row_id':relation['id']})
 for participant in db.all('deal_persons','person_id=?',(source_id,)):
  if db.find('deal_persons','deal_id=? AND person_id=?',(participant['deal_id'],target_id)):conflicts.append({'code':'duplicate_deal_participant','table':'deal_persons','row_id':participant['id']})
 return conflicts
def _capture_references(db,source_id):
 references=[]
 for table,field in PERSON_REFERENCES:
  rows=db.execute(f'SELECT id FROM "{table}" WHERE "{field}"=?',(source_id,)).fetchall();references.extend({'table':table,'field':field,'row_id':dict(row)['id']} for row in rows)
 references.sort(key=lambda item:(item['table'],item['field'],item['row_id']));return references
def _move_reference(db,item,expected_person_id,new_person_id):
 cursor=db.execute(f'UPDATE "{item["table"]}" SET "{item["field"]}"=?, updated_at=? WHERE id=? AND "{item["field"]}"=?',(new_person_id,now(),item['row_id'],expected_person_id))
 if cursor.rowcount!=1:raise AppError('Связанные данные изменились параллельно. Обновите страницу и повторите проверку.',409,{'table':item['table'],'row_id':item['row_id'],'field':item['field']})
def confirm_person_merge(service,merge_id,reason):
 service.require(*CONFIRM_ROLES);reason=_reason(reason,'почему объединение подтверждено вручную');db=service.db
 with db.atomic():
  merge=db.one('person_merges',merge_id)
  if merge['status']!='proposed':raise AppError('Подтвердить можно только ожидающее предложение',409)
  if merge['proposed_by']==service.actor:raise AppError('Предложение должен подтвердить другой сотрудник',409)
  source=_visible_person(service,merge['source_person_id']);target=_visible_person(service,merge['target_person_id'])
  if source.get('record_state')=='merged' or target.get('record_state')=='merged':raise AppError('Одна из карточек уже была объединена',409)
  if _open_merges(db,source['id'],target['id'],merge_id):raise AppError('Появилось другое незавершённое объединение',409)
  conflicts=_merge_conflicts(db,source['id'],target['id'])
  if conflicts:raise AppError('Объединение требует ручного разрешения конфликтов',409,{'conflicts':conflicts})
  snapshot={'version':1,'references':_capture_references(db,source['id']),'source_state':{'record_state':source.get('record_state'),'merged_into_id':source.get('merged_into_id')}}
  for item in snapshot['references']:_move_reference(db,item,source['id'],target['id'])
  db.update('persons',source['id'],{'record_state':'merged','merged_into_id':target['id']});confirmed_at=now();merge=db.update('person_merges',merge_id,{'status':'confirmed','confirmed_by':service.actor,'confirmed_at':confirmed_at,'confirmation_reason':reason,'snapshot':snapshot});_event(db,merge_id,'confirmed',service.actor,reason,snapshot);service.audit('person_merge_confirmed','person_merges',merge_id,{'reference_count':len(snapshot['references'])});return public_merge(merge)
def _verify_revert_snapshot(db,merge,snapshot):
 conflicts=[]
 for item in snapshot.get('references') or []:
  row=db.execute(f'SELECT "{item["field"]}" AS person_value FROM "{item["table"]}" WHERE id=?',(item['row_id'],)).fetchone();current=None if row is None else dict(row)['person_value']
  if row is None or current!=merge['target_person_id']:conflicts.append({'code':'reference_changed',**item})
 source=db.one('persons',merge['source_person_id'])
 if source.get('record_state')!='merged' or source.get('merged_into_id')!=merge['target_person_id']:conflicts.append({'code':'source_state_changed','table':'persons','row_id':source['id']})
 return conflicts
def revert_person_merge(service,merge_id,reason):
 service.require(*REVERT_ROLES);reason=_reason(reason,'почему подтверждённое объединение отменяется');db=service.db
 with db.atomic():
  merge=db.one('person_merges',merge_id)
  if merge['status']!='confirmed':raise AppError('Отменить можно только подтверждённое объединение',409)
  _visible_person(service,merge['source_person_id']);target=_visible_person(service,merge['target_person_id'])
  if target.get('record_state')=='merged':raise AppError('Целевая карточка уже участвовала в другом объединении',409)
  snapshot=merge.get('snapshot') or {}
  if snapshot.get('version')!=1:raise AppError('Неизвестная версия снимка объединения; требуется ручная проверка',409)
  conflicts=_verify_revert_snapshot(db,merge,snapshot)
  if conflicts:raise AppError('Отмена остановлена: связанные данные изменились',409,{'conflicts':conflicts})
  for item in reversed(snapshot.get('references') or []):_move_reference(db,item,merge['target_person_id'],merge['source_person_id'])
  source_state=snapshot.get('source_state') or {};db.update('persons',merge['source_person_id'],{'record_state':source_state.get('record_state'),'merged_into_id':source_state.get('merged_into_id')});reverted_at=now();merge=db.update('person_merges',merge_id,{'status':'reverted','reverted_by':service.actor,'reverted_at':reverted_at,'revert_reason':reason});_event(db,merge_id,'reverted',service.actor,reason,snapshot);service.audit('person_merge_reverted','person_merges',merge_id,{'reference_count':len(snapshot.get('references') or [])});return public_merge(merge)
def dispatch_person_merges(service,method,path,data):
 if path=='/api/person-merges/proposals':
  if method!='POST':raise AppError('Метод для этого адреса не поддерживается',405,{'allowed':['POST']})
  return {'merge':propose_person_merge(service,data.get('source_person_id'),data.get('target_person_id'),data.get('reason'))}
 match=MERGE_ACTION.fullmatch(path)
 if match:
  if method!='POST':raise AppError('Метод для этого адреса не поддерживается',405,{'allowed':['POST']})
  action=confirm_person_merge if match[2]=='confirm' else revert_person_merge;return {'merge':action(service,match[1],data.get('reason'))}
 match=MERGE_ITEM.fullmatch(path)
 if match:
  if method!='GET':raise AppError('Метод для этого адреса не поддерживается',405,{'allowed':['GET']})
  service.require(*PROPOSE_ROLES);merge=service.db.one('person_merges',match[1]);_visible_person(service,merge['source_person_id']);_visible_person(service,merge['target_person_id']);return {'merge':public_merge(merge)}
 return None
