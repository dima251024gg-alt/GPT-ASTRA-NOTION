"""Ручной ввод: штатный путь, идемпотентность и отсутствие утечек."""
import os,tempfile,unittest
from pathlib import Path
SANDBOX=tempfile.TemporaryDirectory();os.environ['DATA_DIR']=SANDBOX.name;os.environ['APP_ENV']='test';os.environ['PROVIDER_MODE']='mock'
from backend.db import Database
from backend.manual_ocr import create_manual_run
from backend.ocr_pipeline import confirm_run
from backend.security import AppError
from backend.service import Service,identity_key
class ManualEntryTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Database('sqlite:///'+str(Path(self.tmp.name)/'manual.sqlite3'));self.db.migrate();self.user=self.db.insert('users',{'full_name':'Синтетический Менеджер','email':'manual@example.test','role':'manager','active':1,'two_fa_enabled':1});self.client=self.db.insert('clients',{'type':'individual','full_name':'Синтетический Заказчик','phone':'+70000000000','manager_id':self.user['id']});person={'client_id':self.client['id'],'last_name_ru':'Примеров','first_name_ru':'Алексей','birth_date':'1991-04-05','processing_blocked':0};person['identity_key']=identity_key(person);self.person=self.db.insert('persons',person,self.user['id']);self.service=Service(self.db,self.user)
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def fields(self):return {'type':'international_passport','number':'700000099','birth_date':'1991-04-05','expiry_date':'2031-04-05','issuing_country':'RUS','last_name_latin':'PRIMEROV','first_name_latin':'ALEKSEI'}
 def test_manual_values_have_no_confidence_and_need_review(self):
  run=create_manual_run(self.service,self.person['id'],self.fields(),'manual-key-0001');self.assertEqual(run['provider'],'manual');self.assertEqual(run['status'],'review_required');self.assertTrue(run['fields']);self.assertTrue(all(x['source']=='manual' for x in run['fields']));self.assertTrue(all(x['raw_confidence'] is None for x in run['fields']))
 def test_repeat_operation_is_idempotent(self):
  first=create_manual_run(self.service,self.person['id'],self.fields(),'manual-key-0002');second=create_manual_run(self.service,self.person['id'],{**self.fields(),'number':'DIFFERENT'},'manual-key-0002');self.assertEqual(first['id'],second['id']);self.assertEqual(len(self.db.all('ocr_runs')),1)
 def test_invalid_and_missing_values_are_rejected(self):
  cases=[({**self.fields(),'birth_date':'1991-02-30'},'календарная'),({k:v for k,v in self.fields().items() if k!='number'},'обязательные'),({**self.fields(),'number':'<script>'},'символы'),({**self.fields(),'secret':'value'},'Недопустимые')]
  for index,(fields,message) in enumerate(cases):
   with self.subTest(message=message),self.assertRaisesRegex(AppError,message):create_manual_run(self.service,self.person['id'],fields,'manual-bad-'+str(index)+'0000000')
 def test_each_critical_field_must_be_confirmed(self):
  run=create_manual_run(self.service,self.person['id'],self.fields(),'manual-key-0003')
  with self.assertRaises(AppError):confirm_run(self.service,run['id'],{'number':{'confirmed':True}})
  confirmations={row['name']:{'confirmed':True} for row in run['fields'] if row['critical']};self.assertEqual(confirm_run(self.service,run['id'],confirmations)['status'],'confirmed')
 def test_possible_truncation_needs_separate_acknowledgement(self):
  run=create_manual_run(self.service,self.person['id'],{**self.fields(),'possible_truncation':True},'manual-key-0004');confirmations={row['name']:{'confirmed':True} for row in run['fields'] if row['critical']}
  with self.assertRaises(AppError) as error:confirm_run(self.service,run['id'],confirmations)
  self.assertTrue(any('усечение' in item for item in error.exception.details['fields']))
  for row in run['fields']:
   if row['possible_truncation']:confirmations[row['name']]['truncation_ack']=True
  self.assertEqual(confirm_run(self.service,run['id'],confirmations)['status'],'confirmed')
 def test_audit_does_not_contain_entered_values(self):
  create_manual_run(self.service,self.person['id'],self.fields(),'manual-key-0005');audit=str(self.db.all('audit_log'));self.assertNotIn('700000099',audit);self.assertNotIn('PRIMEROV',audit);self.assertIn('field_names',audit)
if __name__=='__main__':unittest.main()
