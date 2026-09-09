"""Маршрутизация OCR API без обхода доменного сервиса."""
import sys,types,unittest
from types import SimpleNamespace
from unittest.mock import patch
import backend
if 'backend.documents' not in sys.modules:
 engine=types.ModuleType('backend.documents');sys.modules['backend.documents']=engine;backend.documents=engine
from backend import features
from backend.security import AppError
class RouteTest(unittest.TestCase):
 def setUp(self):self.service=SimpleNamespace(db=object())
 @patch('backend.ocr_pipeline.process_batch')
 def test_create_batch(self,process):
  process.return_value={'id':'batch','status':'completed'};data={'operation_key':'batch-key-1000','items':[{'person_id':'p'}]};result=features.dispatch_features(self.service,'POST','/api/ocr/batches',{},data);self.assertEqual(result['id'],'batch');process.assert_called_once_with(self.service,data['items'],data['operation_key'])
 @patch('backend.ocr_pipeline.batch_view')
 def test_get_batch(self,view):
  view.return_value={'id':'batch'};self.assertEqual(features.dispatch_features(self.service,'GET','/api/ocr/batches/batch',{},{}),{'id':'batch'});view.assert_called_once_with(self.service,'batch')
 @patch('backend.ocr_pipeline.confirm_run')
 def test_confirm_fields(self,confirm):
  confirm.return_value={'id':'run','status':'confirmed'};values={'number':{'confirmed':True}};result=features.dispatch_features(self.service,'POST','/api/ocr/runs/run/confirm',{},{'confirmations':values});self.assertEqual(result['status'],'confirmed');confirm.assert_called_once_with(self.service,'run',values)
 @patch('backend.manual_ocr.create_manual_run')
 def test_create_standalone_manual_run(self,create):
  create.return_value={'id':'manual','status':'review_required'};data={'person_id':'person','fields':{'type':'other'},'operation_key':'manual-key-1000'};result=features.dispatch_features(self.service,'POST','/api/ocr/manual',{},data);self.assertEqual(result['id'],'manual');create.assert_called_once_with(self.service,'person',data['fields'],'manual-key-1000')
 @patch('backend.manual_ocr.create_manual_run')
 def test_manual_revision_keeps_source_run(self,create):
  create.return_value={'id':'manual-2'};data={'fields':{'type':'international_passport'},'operation_key':'manual-key-2000'};features.dispatch_features(self.service,'POST','/api/ocr/runs/source-run/manual',{},data);create.assert_called_once_with(self.service,None,data['fields'],'manual-key-2000','source-run')
 def test_methods_are_rejected(self):
  for method,path in (('GET','/api/ocr/batches'),('POST','/api/ocr/batches/id'),('GET','/api/ocr/runs/id/confirm'),('GET','/api/ocr/manual'),('GET','/api/ocr/runs/id/manual')):
   with self.subTest(method=method,path=path),self.assertRaises(AppError) as error:features.dispatch_features(self.service,method,path,{}, {})
   self.assertEqual(error.exception.status,405)
if __name__=='__main__':unittest.main()
