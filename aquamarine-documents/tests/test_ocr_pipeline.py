"""Негативные, аварийные и идемпотентные проверки OCR-конвейера."""
import base64,io,os,tempfile,unittest
from pathlib import Path
from PIL import Image
SANDBOX=tempfile.TemporaryDirectory();os.environ['DATA_DIR']=SANDBOX.name;os.environ['APP_ENV']='test';os.environ['PROVIDER_MODE']='mock'
from backend.db import Database
from backend.ocr_pipeline import AdapterInfo,UnavailableAntivirus,UnavailableOCR,confirm_run,process_batch
from backend.security import AppError
from backend.service import Service,document_key,identity_key
class FakeAV:
 info=AdapterInfo('test-av','1','available')
 def __init__(self,result='clean'):self.result=result;self.calls=0
 def scan(self,raw,mime):self.calls+=1;return self.result
class FakeOCR:
 info=AdapterInfo('test-ocr','2026.09','available')
 def __init__(self):self.calls=0
 def recognize(self,raw,mime):
  self.calls+=1;return {'provider':'test','mrz_valid':True,'confidence_origin':'provider:test-ocr:2026.09','fields':{'type':'international_passport','number':'700000001','issuing_country':'RUS','last_name_latin':'TESTOV','first_name_latin':'IVAN','birth_date':'1990-01-02','expiry_date':'2030-01-02'},'confidence':{'number':.97,'birth_date':None,'last_name_latin':.9},'warnings':[]}
def png():
 out=io.BytesIO();Image.new('RGB',(400,260),'white').save(out,'PNG');return out.getvalue()
class PipelineTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Database('sqlite:///'+str(Path(self.tmp.name)/'db.sqlite3'));self.db.migrate();self.user=self.db.insert('users',{'full_name':'Тестовый Менеджер','email':'manager@example.test','role':'manager','active':1,'two_fa_enabled':1});self.client=self.db.insert('clients',{'type':'individual','full_name':'Синтетический Заказчик','phone':'+70000000000','manager_id':self.user['id']});p={'client_id':self.client['id'],'last_name_ru':'Тестов','first_name_ru':'Иван','birth_date':'1990-01-02','processing_blocked':0};p['identity_key']=identity_key(p);self.person=self.db.insert('persons',p,self.user['id']);self.service=Service(self.db,self.user)
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def item(self,raw=None):return {'name':'synthetic.png','mime':'image/jpeg','person_id':self.person['id'],'content_base64':base64.b64encode(raw or png()).decode()}
 def test_batch_is_idempotent_and_real_type_wins(self):
  av=FakeAV();provider=FakeOCR();first=process_batch(self.service,[self.item()],'batch-key-0001',provider,av);second=process_batch(self.service,[self.item()],'batch-key-0001',provider,av);self.assertEqual(first['id'],second['id']);self.assertEqual(provider.calls,1);self.assertEqual(av.calls,1);self.assertEqual(first['items'][0]['mime'],'image/png');self.assertEqual(first['items'][0]['status'],'review_required')
 def test_partial_failure_does_not_discard_valid_file(self):
  bad={'name':'bad.exe','person_id':self.person['id'],'content_base64':base64.b64encode(b'MZbad').decode()};result=process_batch(self.service,[self.item(),bad],'batch-key-0002',FakeOCR(),FakeAV());self.assertEqual(result['status'],'partial');self.assertEqual([x['status'] for x in result['items']],['review_required','rejected'])
 def test_duplicate_file_is_not_recognized_twice(self):
  raw=png();provider=FakeOCR();result=process_batch(self.service,[self.item(raw),self.item(raw)],'batch-key-0003',provider,FakeAV());self.assertEqual(provider.calls,1);self.assertEqual(result['items'][1]['status'],'duplicate_in_batch');self.assertEqual(result['items'][1]['duplicate_of'],0)
 def test_unavailable_services_never_report_success(self):
  blocked=process_batch(self.service,[self.item()],'batch-key-0004',FakeOCR(),UnavailableAntivirus());self.assertEqual(blocked['items'][0]['status'],'quarantine_blocked');self.assertFalse(self.db.all('files'));manual=process_batch(self.service,[self.item()],'batch-key-0005',UnavailableOCR(),FakeAV());self.assertEqual(manual['items'][0]['status'],'manual_required');self.assertEqual(manual['items'][0]['ocr']['fields'],[])
 def test_critical_fields_require_individual_confirmation(self):
  result=process_batch(self.service,[self.item()],'batch-key-0006',FakeOCR(),FakeAV());run=result['items'][0]['ocr']
  with self.assertRaises(AppError):confirm_run(self.service,run['id'],{'number':{'confirmed':True}})
  confirmations={x['name']:{'confirmed':True} for x in run['fields'] if x['critical']};self.assertEqual(confirm_run(self.service,run['id'],confirmations)['status'],'confirmed')
 def test_limits_and_logs_contain_no_document_values(self):
  with self.assertRaises(AppError):process_batch(self.service,[self.item()]*21,'batch-key-0008',FakeOCR(),FakeAV())
  process_batch(self.service,[self.item()],'batch-key-0009',FakeOCR(),FakeAV());logs=str(self.db.all('integration_calls'))+str(self.db.all('audit_log'));self.assertNotIn('700000001',logs);self.assertNotIn('TESTOV',logs);self.assertNotIn('iVBOR',logs)
if __name__=='__main__':unittest.main()
