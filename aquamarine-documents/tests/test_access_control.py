import os,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
SANDBOX=tempfile.TemporaryDirectory();os.environ['DATA_DIR']=SANDBOX.name;os.environ['APP_ENV']='test'
from backend.access_control import STAFF_ROLES,create_external_subject,create_grant,grant_allows,link_client_person,revoke_grant,validate_staff_role
from backend.db import Database
from backend.security import AppError
class AccessControlTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Database('sqlite:///'+str(Path(self.tmp.name)/'access.sqlite3'));self.db.migrate();self.director=self.user('director','director@example.test');self.manager=self.user('manager','manager@example.test');self.tech=self.user('tech_admin','tech@example.test');self.client=self.db.insert('clients',{'type':'individual','full_name':'Синтетический Заказчик','phone':'+70000000000','manager_id':self.manager['id'],'status':'active'});self.person=self.db.insert('persons',{'last_name_ru':'Тестов','first_name_ru':'Иван','birth_date':'1990-01-01','processing_blocked':0,'legal_hold':0})
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def user(self,role,email):return self.db.insert('users',{'full_name':'Синтетический Сотрудник','email':email,'role':role,'active':1})
 def expiry(self,hours=1):return(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()
 def test_staff_roles_are_closed_set(self):
  self.assertEqual(STAFF_ROLES,{'manager','senior_manager','director','accountant','dpo','tech_admin'})
  for role in STAFF_ROLES:self.assertEqual(validate_staff_role(role),role)
  for legacy in('admin','senior','client','tourist'):
   with self.assertRaises(AppError):validate_staff_role(legacy)
 def test_tech_admin_requires_explicit_short_grant(self):
  self.assertFalse(grant_allows(self.db,'user',self.tech['id'],'clients',self.client['id'],'read'));grant=create_grant(self.db,self.director,'user',self.tech['id'],'clients',self.client['id'],['read'],self.expiry(4),'Диагностика обращения 12345');self.assertTrue(grant_allows(self.db,'user',self.tech['id'],'clients',self.client['id'],'read'));self.assertFalse(grant_allows(self.db,'user',self.tech['id'],'clients',self.client['id'],'write'));revoke_grant(self.db,self.director,grant['id'],'Диагностика завершена 12345');self.assertFalse(grant_allows(self.db,'user',self.tech['id'],'clients',self.client['id'],'read'))
 def test_tech_admin_grant_cannot_exceed_eight_hours(self):
  with self.assertRaises(AppError):create_grant(self.db,self.director,'user',self.tech['id'],'clients',self.client['id'],['read'],self.expiry(9),'Длительная диагностика 12345')
 def test_external_subject_has_no_role_and_only_grants(self):
  external=create_external_subject(self.db,self.manager,'traveler',person_id=self.person['id'],contact='+70000000001');self.assertNotIn('role',external);self.assertFalse(grant_allows(self.db,'external_subject',external['id'],'clients',self.client['id'],'read'));create_grant(self.db,self.director,'external_subject',external['id'],'clients',self.client['id'],['read','download'],self.expiry(24),'Кабинет по активной заявке');self.assertTrue(grant_allows(self.db,'external_subject',external['id'],'clients',self.client['id'],'download'))
  with self.assertRaises(AppError):create_grant(self.db,self.director,'external_subject',external['id'],'clients',self.client['id'],['write'],self.expiry(1),'Изменение внутренней карточки')
 def test_manager_cannot_issue_grants(self):
  with self.assertRaises(AppError):create_grant(self.db,self.manager,'user',self.tech['id'],'clients',self.client['id'],['read'],self.expiry(),'Попытка самовольного доступа')
 def test_grants_are_exact_and_events_immutable(self):
  other=self.db.insert('clients',{'type':'individual','full_name':'Другой Синтетический','phone':'+70000000002','manager_id':self.manager['id'],'status':'active'});create_grant(self.db,self.director,'user',self.tech['id'],'clients',self.client['id'],['read'],self.expiry(),'Разбор конкретного обращения');self.assertFalse(grant_allows(self.db,'user',self.tech['id'],'clients',other['id'],'read'))
  with self.assertRaises(Exception):self.db.execute('DELETE FROM access_grant_events')
 def test_client_person_relation_is_explicit(self):
  relation=link_client_person(self.db,self.manager,self.client['id'],self.person['id'],'traveler',True);self.assertEqual(relation['person_id'],self.person['id']);self.assertEqual(len(self.db.all('client_person_relations')),1);self.assertEqual(link_client_person(self.db,self.manager,self.client['id'],self.person['id'],'traveler')['id'],relation['id'])
if __name__=='__main__':unittest.main()
