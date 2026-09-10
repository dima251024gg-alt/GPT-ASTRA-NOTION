import os,tempfile,unittest
from pathlib import Path
SANDBOX=tempfile.TemporaryDirectory();os.environ['DATA_DIR']=SANDBOX.name;os.environ['APP_ENV']='test'
from backend.db import Database
from backend.person_merge import confirm_person_merge,dispatch_person_merges,propose_person_merge,revert_person_merge
from backend.security import AppError
class MergeService:
 def __init__(self,db,actor):self.db=db;self.user=actor;self.actor=actor['id']
 def require(self,*roles):
  if self.user['role'] not in roles:raise AppError('Недостаточно прав для этого действия',403)
 def can(self,table,row):return table=='persons' and self.user['active']==1
 def audit(self,action,table,row_id,details=None):return self.db.audit(self.actor,action,table,row_id,details or {})
class PersonMergeTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Database('sqlite:///'+str(Path(self.tmp.name)/'merge.sqlite3'));self.db.migrate();self.manager=self.user('manager','manager-merge@example.test');self.senior=self.user('senior_manager','senior-merge@example.test');self.director=self.user('director','director-merge@example.test');self.dpo=self.user('dpo','dpo-merge@example.test');self.source=self.person('Источник','Один');self.target=self.person('Цель','Два');self.client=self.db.insert('clients',{'type':'individual','full_name':'Синтетический Заказчик','phone':'+70000000444','manager_id':self.manager['id'],'person_id':self.source['id'],'status':'active'},self.manager['id']);self.relation=self.db.insert('client_person_relations',{'client_id':self.client['id'],'person_id':self.source['id'],'relation_type':'customer','is_primary':1,'status':'active','valid_from':'2026-01-01T00:00:00+00:00','source':'manual','verified_by':self.manager['id'],'verified_at':'2026-01-01T00:00:00+00:00'},self.manager['id']);self.address=self.db.insert('addresses',{'person_id':self.source['id'],'type':'registration','city':'Тестовый город'},self.manager['id'])
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def user(self,role,email):return self.db.insert('users',{'full_name':'Синтетический Сотрудник','email':email,'role':role,'active':1})
 def person(self,last_name,first_name):return self.db.insert('persons',{'last_name_ru':last_name,'first_name_ru':first_name,'birth_date':'1990-01-01','processing_blocked':0,'legal_hold':0,'record_state':'active'})
 def service(self,actor):return MergeService(self.db,actor)
 def proposal(self):return propose_person_merge(self.service(self.manager),self.source['id'],self.target['id'],'Карточки проверены по исходным документам')
 def test_proposal_does_not_change_person_references(self):
  proposal=self.proposal();self.assertEqual(proposal['status'],'proposed');self.assertEqual(self.db.one('addresses',self.address['id'])['person_id'],self.source['id']);self.assertEqual(self.db.one('persons',self.source['id'])['record_state'],'active');self.assertEqual(len(self.db.all('person_merge_events')),1)
 def test_confirm_moves_references_and_revert_restores_snapshot(self):
  proposal=self.proposal();confirmed=confirm_person_merge(self.service(self.senior),proposal['id'],'Вручную сопоставлены обязательные реквизиты');self.assertEqual(confirmed['status'],'confirmed');self.assertEqual(confirmed['reference_count'],3);self.assertEqual(self.db.one('clients',self.client['id'])['person_id'],self.target['id']);self.assertEqual(self.db.one('addresses',self.address['id'])['person_id'],self.target['id']);self.assertEqual(self.db.one('client_person_relations',self.relation['id'])['person_id'],self.target['id']);source=self.db.one('persons',self.source['id']);self.assertEqual((source['record_state'],source['merged_into_id']),('merged',self.target['id']));reverted=revert_person_merge(self.service(self.dpo),proposal['id'],'Выявлено ошибочное сопоставление карточек');self.assertEqual(reverted['status'],'reverted');self.assertEqual(self.db.one('clients',self.client['id'])['person_id'],self.source['id']);self.assertEqual(self.db.one('addresses',self.address['id'])['person_id'],self.source['id']);source=self.db.one('persons',self.source['id']);self.assertEqual((source['record_state'],source['merged_into_id']),('active',None));events=[event['event'] for event in self.db.all('person_merge_events')];self.assertEqual(len(events),3);self.assertEqual(set(events),{'proposed','confirmed','reverted'})
 def test_proposer_cannot_confirm_own_proposal(self):
  proposal=self.proposal()
  with self.assertRaises(AppError):confirm_person_merge(self.service(self.manager),proposal['id'],'Самостоятельное подтверждение запрещено')
  self.assertEqual(self.db.one('person_merges',proposal['id'])['status'],'proposed')
 def test_duplicate_relation_conflict_rolls_back_confirmation(self):
  self.db.insert('client_person_relations',{'client_id':self.client['id'],'person_id':self.target['id'],'relation_type':'customer','is_primary':0,'status':'active','valid_from':'2026-01-02T00:00:00+00:00','source':'manual'});proposal=self.proposal()
  with self.assertRaises(AppError) as error:confirm_person_merge(self.service(self.senior),proposal['id'],'Сотрудник подтверждает совпадение карточек')
  self.assertEqual(error.exception.status,409);self.assertEqual(error.exception.details['conflicts'][0]['code'],'duplicate_client_relation');self.assertEqual(self.db.one('client_person_relations',self.relation['id'])['person_id'],self.source['id']);self.assertEqual(self.db.one('person_merges',proposal['id'])['status'],'proposed')
 def test_revert_fails_closed_when_reference_changed_after_merge(self):
  proposal=self.proposal();confirm_person_merge(self.service(self.senior),proposal['id'],'Вручную сопоставлены обязательные реквизиты');third=self.person('Третий','Три');self.db.update('addresses',self.address['id'],{'person_id':third['id']})
  with self.assertRaises(AppError) as error:revert_person_merge(self.service(self.director),proposal['id'],'Отмена после выявленной ошибки оператора')
  self.assertEqual(error.exception.status,409);self.assertEqual(self.db.one('person_merges',proposal['id'])['status'],'confirmed');self.assertEqual(self.db.one('clients',self.client['id'])['person_id'],self.target['id'])
 def test_reasons_are_required_and_events_are_immutable(self):
  with self.assertRaises(AppError):propose_person_merge(self.service(self.manager),self.source['id'],self.target['id'],'коротко')
  proposal=self.proposal()
  with self.assertRaises(Exception):self.db.execute('DELETE FROM person_merge_events WHERE merge_id=?',(proposal['id'],))
 def test_api_dispatcher_exposes_only_explicit_lifecycle_actions(self):
  created=dispatch_person_merges(self.service(self.manager),'POST','/api/person-merges/proposals',{'source_person_id':self.source['id'],'target_person_id':self.target['id'],'reason':'Проверка предложения через серверный маршрут'})['merge'];confirmed=dispatch_person_merges(self.service(self.senior),'POST',f'/api/person-merges/{created["id"]}/confirm',{'reason':'Независимая ручная проверка через маршрут'})['merge'];self.assertEqual(confirmed['status'],'confirmed');viewed=dispatch_person_merges(self.service(self.director),'GET',f'/api/person-merges/{created["id"]}',{})['merge'];self.assertEqual(viewed['id'],created['id']);reverted=dispatch_person_merges(self.service(self.dpo),'POST',f'/api/person-merges/{created["id"]}/revert',{'reason':'Контрольная отмена через серверный маршрут'})['merge'];self.assertEqual(reverted['status'],'reverted');self.assertIsNone(dispatch_person_merges(self.service(self.manager),'GET','/api/unrelated',{}))
  with self.assertRaises(AppError) as error:dispatch_person_merges(self.service(self.manager),'GET','/api/person-merges/proposals',{})
  self.assertEqual(error.exception.status,405)
if __name__=='__main__':unittest.main()
