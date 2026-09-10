import os,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
SANDBOX=tempfile.TemporaryDirectory();os.environ['DATA_DIR']=SANDBOX.name;os.environ['APP_ENV']='test'
from backend.access_control import create_grant,link_client_person
from backend.db import Database
from backend.security import AppError
from backend.service import Service
class ServiceAccessTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Database('sqlite:///'+str(Path(self.tmp.name)/'service.sqlite3'));self.db.migrate();self.director=self.user('director','director2@example.test');self.manager=self.user('manager','manager2@example.test');self.other=self.user('manager','other@example.test');self.tech=self.user('tech_admin','tech2@example.test');self.dpo=self.user('dpo','dpo@example.test');self.client=self.db.insert('clients',{'type':'individual','full_name':'Синтетический Клиент','phone':'+70000000111','manager_id':self.manager['id'],'status':'active','comment':'Исходное значение'});self.person=self.db.insert('persons',{'last_name_ru':'Синтетиков','first_name_ru':'Иван','birth_date':'1991-02-03','processing_blocked':0,'legal_hold':0});link_client_person(self.db,self.manager,self.client['id'],self.person['id'],'traveler',True)
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def user(self,role,email):return self.db.insert('users',{'full_name':'Синтетический Сотрудник','email':email,'role':role,'active':1})
 def expiry(self):return(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
 def test_person_access_comes_from_relation_not_person_column(self):
  self.assertIsNone(self.person.get('client_id'));self.assertEqual(Service(self.db,self.manager).get('persons',self.person['id'])['id'],self.person['id'])
  with self.assertRaises(AppError):Service(self.db,self.other).get('persons',self.person['id'])
 def test_tech_admin_is_denied_without_exact_grant(self):
  with self.assertRaises(AppError):Service(self.db,self.tech).get('clients',self.client['id'])
  create_grant(self.db,self.director,'user',self.tech['id'],'clients',self.client['id'],['read'],self.expiry(),'Диагностика конкретной заявки');self.assertEqual(Service(self.db,self.tech).get('clients',self.client['id'])['id'],self.client['id'])
 def test_write_requires_write_permission(self):
  create_grant(self.db,self.director,'user',self.tech['id'],'clients',self.client['id'],['read'],self.expiry(),'Диагностика только для чтения')
  with self.assertRaises(AppError):Service(self.db,self.tech).update('clients',self.client['id'],{'comment':'Нельзя'})
  create_grant(self.db,self.director,'user',self.tech['id'],'clients',self.client['id'],['read','write'],self.expiry(),'Исправление конкретной заявки');result=Service(self.db,self.tech).update('clients',self.client['id'],{'comment':'Исправлено'});self.assertEqual(result['comment'],'Исправлено')
 def test_dpo_can_read_privacy_data_but_accounting_mask_remains(self):self.assertEqual(Service(self.db,self.dpo).get('persons',self.person['id'])['id'],self.person['id'])
 def test_legacy_internal_role_is_normalized_not_exposed(self):
  legacy=self.user('admin','legacy@example.test');service=Service(self.db,legacy);self.assertEqual(service.user['role'],'director');self.assertEqual(service.public('users',legacy)['role'],'director')
if __name__=='__main__':unittest.main()
