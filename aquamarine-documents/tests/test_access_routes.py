import os,sys,tempfile,unittest,uuid
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
SANDBOX=tempfile.TemporaryDirectory();os.environ['DATA_DIR']=SANDBOX.name;os.environ['APP_ENV']='test'
sys.modules['backend.portal']=SimpleNamespace(public_portal=lambda *args:None,portal_dispatch=lambda *args:None)
from backend.access_control import grant_allows
from backend.api import Application,Response
from backend.db import Database,now
from backend.security import AppError,blind
class AccessRoutesTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Database('sqlite:///'+str(Path(self.tmp.name)/'routes.sqlite3'));self.db.migrate();self.director=self.user('director','director-route@example.test');self.manager=self.user('manager','manager-route@example.test');self.tech=self.user('tech_admin','tech-route@example.test');self.client=self.db.insert('clients',{'type':'individual','full_name':'Синтетический API','phone':'+70000000222','manager_id':self.manager['id'],'status':'active'});self.app=Application()
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def user(self,role,email):return self.db.insert('users',{'full_name':'Синтетический Сотрудник','email':email,'role':role,'active':1,'two_fa_enabled':1})
 def headers(self,user):
  session_token='session-'+str(uuid.uuid4());csrf=blind(session_token+':csrf');self.db.insert('sessions',{'user_id':user['id'],'token_hash':blind(session_token),'csrf_hash':blind(csrf),'last_seen':now(),'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),'revoked_at':None});return {'cookie':'aq_session='+session_token,'x-csrf-token':csrf}
 def dispatch(self,user,path,data):return self.app.dispatch(self.db,'POST',path,{},self.headers(user),data,'127.0.0.1')
 def test_issue_and_revoke_grant_routes(self):
  body={'subject_type':'user','subject_id':self.tech['id'],'resource_type':'clients','resource_id':self.client['id'],'permissions':['read'],'expires_at':(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),'reason':'Диагностика обращения через API'};created=self.dispatch(self.director,'/api/access-grants',body);self.assertIsInstance(created,Response);grant=created.data;self.assertTrue(grant_allows(self.db,'user',self.tech['id'],'clients',self.client['id'],'read'));revoked=self.dispatch(self.director,'/api/access-grants/'+grant['id']+'/revoke',{'reason':'Диагностика через API завершена'});self.assertTrue(revoked['revoked_at']);self.assertFalse(grant_allows(self.db,'user',self.tech['id'],'clients',self.client['id'],'read'))
 def test_external_subject_route_creates_no_role(self):
  person=self.db.insert('persons',{'last_name_ru':'Внешний','first_name_ru':'Турист','birth_date':'1990-01-01','processing_blocked':0,'legal_hold':0});result=self.dispatch(self.manager,'/api/external-subjects',{'kind':'traveler','person_id':person['id'],'contact':'+70000000333'});self.assertIsInstance(result,Response);self.assertNotIn('role',result.data)
 def test_manager_cannot_issue_grant_route(self):
  body={'subject_type':'user','subject_id':self.tech['id'],'resource_type':'clients','resource_id':self.client['id'],'permissions':['read'],'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),'reason':'Попытка выдачи через API'}
  with self.assertRaises(AppError) as error:self.dispatch(self.manager,'/api/access-grants',body)
  self.assertEqual(error.exception.status,403)
 def test_user_route_rejects_legacy_role(self):
  with self.assertRaises(AppError):self.dispatch(self.director,'/api/users',{'full_name':'Старый Админ','email':'old@example.test','role':'admin','password':'SyntheticPassword123'})
if __name__=='__main__':unittest.main()
