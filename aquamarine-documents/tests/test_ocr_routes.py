"""Маршрутизация OCR API без обхода доменного сервиса."""
import sys,types,unittest
from types import SimpleNamespace
from unittest.mock import patch
import backend
if "backend.documents" not in sys.modules:
    engine=types.ModuleType("backend.documents");sys.modules["backend.documents"]=engine;backend.documents=engine
from backend import features
from backend.security import AppError
class RouteTest(unittest.TestCase):
    def setUp(self):self.service=SimpleNamespace(db=object())
    @patch("backend.ocr_pipeline.process_batch")
    def test_create_batch(self,process):
        process.return_value={"id":"batch","status":"completed"};data={"operation_key":"batch-key-1000","items":[{"person_id":"p"}]};result=features.dispatch_features(self.service,"POST","/api/ocr/batches",{},data);self.assertEqual(result["id"],"batch");process.assert_called_once_with(self.service,data["items"],data["operation_key"])
    @patch("backend.ocr_pipeline.batch_view")
    def test_get_batch(self,view):
        view.return_value={"id":"batch"};result=features.dispatch_features(self.service,"GET","/api/ocr/batches/batch",{},{});self.assertEqual(result,{"id":"batch"});view.assert_called_once_with(self.service,"batch")
    @patch("backend.ocr_pipeline.confirm_run")
    def test_confirm_fields(self,confirm):
        confirm.return_value={"id":"run","status":"confirmed"};values={"number":{"confirmed":True}};result=features.dispatch_features(self.service,"POST","/api/ocr/runs/run/confirm",{},{"confirmations":values});self.assertEqual(result["status"],"confirmed");confirm.assert_called_once_with(self.service,"run",values)
    def test_methods_are_rejected(self):
        for method,path in (("GET","/api/ocr/batches"),("POST","/api/ocr/batches/id"),("GET","/api/ocr/runs/id/confirm")):
            with self.subTest(method=method,path=path),self.assertRaises(AppError) as error:features.dispatch_features(self.service,method,path,{}, {})
            self.assertEqual(error.exception.status,405)
if __name__=="__main__":unittest.main()
