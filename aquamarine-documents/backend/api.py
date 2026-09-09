"""HTTP-independent API. FastAPI and the offline server call the same dispatcher."""
import base64, json, os, traceback, re
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from urllib.parse import urlsplit, parse_qs
from pathlib import Path
from .db import Database, now
from .security import AppError, canonical, blind, totp, hash_password, new_totp_secret
from .service import Service, login, authenticate, safe_user, CRUD, STATUSES
from . import config

@dataclass
class Response:
    data: object = field(default_factory=dict)
    status: int = 200
    headers: dict = field(default_factory=dict)

def cookie(value,max_age=43200):
    return 'aq_session='+value+'; HttpOnly; SameSite=Strict; Path=/; Max-Age='+str(max_age)+('; Secure' if config.PRODUCTION else '')

class Application:
    def __init__(self,url=None): self.url=url
    def handle(self,method,target,headers=None,body=b'',ip='127.0.0.1'):
        headers={k.lower():v for k,v in (headers or {}).items()}
        path=urlsplit(target).path; query=parse_qs(urlsplit(target).query)
        if len(body)>30*1024*1024: return Response({'message':'Файл слишком большой'},413)
        try: data=json.loads(body) if body else {}
        except (ValueError,UnicodeDecodeError): return Response({'message':'Некорректный запрос'},400)
        if not isinstance(data,dict): return Response({'message':'Ожидается объект запроса'},400)
        db=Database(self.url)
        try:
            with db.atomic():
                try: result=self.dispatch(db,method,path,query,headers,data,ip)
                except AppError as e:
                    # Failed authentication, denied access and OTP attempts MUST survive rollback.
                    if e.status not in (401,403,429) and not getattr(e,'persist',False): raise
                    result=Response({'message':e.message,'details':e.details},e.status)
            return result if isinstance(result,Response) else Response(result)
        except AppError as e: return Response({'message':e.message,'details':e.details},e.status)
        except (ValueError,TypeError,KeyError) as e:
            if not config.PRODUCTION: traceback.print_exc()
            return Response({'message':'Проверьте обязательные поля и формат значений'},422)
        except Exception as e:
            if not config.PRODUCTION: traceback.print_exc()
            if 'UNIQUE' in str(e) or 'unique constraint' in str(e): return Response({'message':'Такая запись уже существует. Обновите страницу.'},409)
            return Response({'message':'Не удалось выполнить действие. Повторите позже или обратитесь к администратору.'},500)
        finally: db.close()
    def dispatch(self,db,method,path,q,h,data,ip):
        if path=='/api/health': return {'ok':True,'mode':config.MODE,'region':config.REGION,'demo':os.getenv('DEMO_MODE')=='1' and not config.PRODUCTION}
        if path=='/api/demo/access' and method=='GET':
            if config.PRODUCTION or os.getenv('DEMO_MODE')!='1' or ip not in ('127.0.0.1','::1'): raise AppError('Страница не найдена',404)
            credentials=json.loads((config.VAR/'demo-credentials.json').read_text()); u=db.find('users','email=?',(credentials['email'],))
            return {**credentials,'code':totp(u['totp_secret'])}
        if path=='/api/auth/login' and method=='POST':
            result,t=login(db,data.get('email',''),data.get('password',''),data.get('code',''),ip,h.get('user-agent',''))
            return Response(result,200,{'Set-Cookie':cookie(t)})
        from .portal import public_portal
        public=public_portal(db,method,path,data,ip,h)
        if public is not None: return public
        cookies=SimpleCookie(); cookies.load(h.get('cookie','')); session_token=cookies['aq_session'].value if 'aq_session' in cookies else ''
        user,session=authenticate(db,session_token,h.get('x-csrf-token'),method not in ('GET','HEAD'))
        if method not in ('GET','HEAD') and h.get('origin') and h['origin']!=config.PUBLIC_URL: raise AppError('Запрос с другого сайта запрещён',403)
        if path=='/api/auth/me': return {'user':safe_user(user) if user else None,'portal':not bool(user),'csrf':blind(session_token+':csrf')}
        if path=='/api/auth/logout' and method=='POST':
            db.update('sessions',session['id'],{'revoked_at':now()}); return Response({'ok':True},200,{'Set-Cookie':cookie('',0)})
        if not user:
            from .portal import portal_dispatch
            return portal_dispatch(db,session,method,path,data,ip,h)
        s=Service(db,user,ip,h.get('user-agent',''))
        if path=='/api/bootstrap':
            return {'user':safe_user(user),'statuses':STATUSES,'countries':s.list('countries',limit=100)['items'],'operators':s.list('operators',limit=100)['items'],'templates':s.list('templates',limit=100)['items']}
        if path=='/api/users' and method=='POST':
            s.require('admin')
            if data.get('role') not in ('admin','senior','manager','accountant'): raise AppError('Неизвестная роль')
            secret=new_totp_secret(); u=db.insert('users',{'full_name':data['full_name'],'email':data['email'].lower(),'phone':data.get('phone'),'role':data['role'],'active':1,'two_fa_enabled':1,'password_hash':hash_password(data['password']),'totp_secret':secret,'last_totp_step':-1},s.actor)
            s.audit('user_created','users',u['id']); return {'user':safe_user(u),'enrollment_secret':secret,'enrollment_uri':f"otpauth://totp/Aquamarine:{u['email']}?secret={secret}&issuer=Aquamarine"}
        m=re.fullmatch(r'/api/users/([^/]+)/disable',path)
        if m and method=='POST':
            s.require('admin'); id=m[1]
            if id==s.actor: raise AppError('Нельзя отключить собственную учётную запись')
            db.update('users',id,{'active':0})
            for sess in db.all('sessions','user_id=?',(id,)): db.update('sessions',sess['id'],{'revoked_at':now()})
            s.audit('user_disabled','users',id); return {'ok':True}
        if path=='/api/persons/separate' and method=='POST': return s.separate_person(data['person'],data.get('reason',''))
        if path=='/api/audit/verify':
            s.require('admin','senior'); return {'valid':db.verify_audit()}
        # Modules are imported here to keep the domain independently testable.
        from .features import dispatch_features
        result=dispatch_features(s,method,path,q,data)
        if result is not None: return result
        parts=path.removeprefix('/api/').split('/'); table=parts[0]
        if len(parts)==1:
            if method=='GET': return s.list(table,q.get('q',[''])[0],int(q.get('limit',['50'])[0]),int(q.get('offset',['0'])[0]))
            if method=='POST': return Response(s.create(table,data),201)
        if len(parts)==2:
            id=parts[1]
            if method=='GET': return s.public(table,s.get(table,id),q.get('reveal',['0'])[0]=='1')
            if method=='PATCH': return s.update(table,id,data)
            if method=='DELETE': return s.remove(table,id)
        raise AppError('Страница не найдена',404)
