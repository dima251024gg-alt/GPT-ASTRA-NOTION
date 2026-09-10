"""Карантин, криптографическое уничтожение и OCR-worker."""
import io,os,subprocess,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock,patch
from PIL import Image,ImageDraw
SANDBOX=tempfile.TemporaryDirectory();os.environ['DATA_DIR']=SANDBOX.name;os.environ['APP_ENV']='test';os.environ['PROVIDER_MODE']='mock'
from backend.db import Database
from backend.ocr_pipeline import AdapterInfo
from backend.secure_upload import destroy_staged,promote_file,purge_expired,read_staged,scan_event,stage_file
from backend.security import AppError
from backend.storage import read_file
from backend.worker_sandbox import isolated_recognize,worker_environment
class Scanner:info=AdapterInfo('test-av','1','available')
def image_bytes():
 image=Image.new('RGB',(640,420),'white');draw=ImageDraw.Draw(image)
 for y in range(40,390,24):draw.line((30,y,610,y),fill='black',width=3)
 output=io.BytesIO();image.save(output,'PNG');return output.getvalue()
class SecureUploadTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Database('sqlite:///'+str(Path(self.tmp.name)/'secure.sqlite3'));self.db.migrate();self.user=self.db.insert('users',{'full_name':'Синтетический Менеджер','email':'secure@example.test','role':'manager','active':1});client=self.db.insert('clients',{'type':'individual','full_name':'Синтетический Заказчик','phone':'+70000000000','manager_id':self.user['id']});self.person=self.db.insert('persons',{'client_id':client['id'],'last_name_ru':'Тестов','first_name_ru':'Иван','birth_date':'1990-01-01','processing_blocked':0},self.user['id']);batch=self.db.insert('upload_batches',{'operation_key':'q-key','requested_by':self.user['id'],'item_count':1,'status':'processing'},self.user['id']);self.item=self.db.insert('upload_items',{'batch_id':batch['id'],'ordinal':0,'name':'synthetic.png','status':'quarantine'},self.user['id']);self.raw=image_bytes()
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def stage(self):return stage_file(self.db,self.raw,'synthetic.png','image/jpeg',self.person['id'],self.item['id'],self.user['id'])
 def test_stage_contains_only_ciphertext_and_encrypted_name(self):
  row=self.stage();payload=Path(row['storage_path']).read_bytes();self.assertNotIn(self.raw[:32],payload);self.assertEqual(read_staged(row),self.raw);stored=self.db.execute('SELECT name FROM quarantine_files WHERE id=?',(row['id'],)).fetchone()[0];self.assertNotIn('synthetic',stored)
 def test_only_clean_file_can_be_promoted(self):
  row=self.stage()
  with self.assertRaises(AppError):promote_file(self.db,row,'persons',self.person['id'],self.user['id'])
  row=scan_event(self.db,row,Scanner(),'clean',12,actor=self.user['id']);final=promote_file(self.db,row,'persons',self.person['id'],self.user['id']);self.assertEqual(read_file(self.db,final['id'])[0],self.raw);current=self.db.one('quarantine_files',row['id']);self.assertEqual(current['status'],'promoted');self.assertIsNone(current['content_key']);self.assertFalse(Path(row['storage_path']).exists())
 def test_blocked_file_is_retained_then_expired(self):
  row=self.stage();scan_event(self.db,row,Scanner(),'unavailable',0,'offline',self.user['id']);self.db.update('quarantine_files',row['id'],{'expires_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()});self.assertEqual(purge_expired(self.db),[row['id']]);self.assertFalse(Path(row['storage_path']).exists());self.assertEqual(self.db.one('quarantine_files',row['id'])['status'],'expired')
 def test_corruption_and_rejection_never_promote(self):
  row=self.stage();Path(row['storage_path']).write_bytes(b'damaged')
  with self.assertRaises(AppError):read_staged(row)
  destroy_staged(self.db,row,'rejected');self.assertFalse(self.db.all('files'))
 def test_scan_events_are_immutable(self):
  row=self.stage();scan_event(self.db,row,Scanner(),'clean',1)
  with self.assertRaises(Exception):self.db.execute('DELETE FROM file_scan_events')
class WorkerTest(unittest.TestCase):
 def test_environment_is_allowlisted(self):
  os.environ['DATABASE_URL']='secret-db';os.environ['OCR_API_KEY']='secret-key';env=worker_environment('/tmp/isolated');self.assertNotIn('DATABASE_URL',env);self.assertNotIn('OCR_API_KEY',env);self.assertEqual(env['DATA_DIR'],'/tmp/isolated')
 def test_worker_returns_only_structured_result(self):
  result=isolated_recognize(image_bytes(),'image/png',timeout=30);self.assertIsInstance(result.get('fields'),dict);self.assertNotIn('boxes',result);self.assertEqual(result['confidence_origin'],'local-worker-v1')
 def test_bad_input_and_timeout_fail_closed(self):
  with self.assertRaises(AppError):isolated_recognize(b'not-an-image','application/octet-stream')
  process=Mock(pid=424242);process.communicate.side_effect=[subprocess.TimeoutExpired('ocr',1),(b'',None)]
  with patch('backend.worker_sandbox.subprocess.Popen',return_value=process),patch('backend.worker_sandbox.os.killpg') as killpg:
   with self.assertRaisesRegex(AppError,'время'):isolated_recognize(image_bytes(),'image/png',timeout=1)
  killpg.assert_called_once()
if __name__=='__main__':unittest.main()
