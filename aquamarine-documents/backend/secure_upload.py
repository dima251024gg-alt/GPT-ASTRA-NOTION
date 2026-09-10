"""Зашифрованный карантин до антивирусной проверки и безопасное продвижение файла."""
import base64,os,secrets
from datetime import datetime,timedelta,timezone
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from . import config,storage
from .db import now,uid
from .security import AppError,decrypt,encrypt
QUARANTINE_HOURS=max(1,min(int(os.getenv('QUARANTINE_HOURS','24')),48))
def _folder():
 path=config.VAR/'quarantine';path.mkdir(parents=True,exist_ok=True,mode=0o700);return path.resolve()
def _path(row):
 path=Path(row['storage_path']).resolve()
 if not path.is_relative_to(_folder()):raise AppError('Недопустимый путь карантинного файла',403)
 return path
def _wipe(path):
 try:
  size=path.stat().st_size
  with path.open('r+b',buffering=0) as stream:
   chunk=b'\0'*min(1024*1024,max(size,1));left=size
   while left:part=chunk[:min(left,len(chunk))];stream.write(part);left-=len(part)
   stream.flush();os.fsync(stream.fileno())
  path.unlink(missing_ok=True)
 except FileNotFoundError:pass
def stage_file(db,raw,name,declared_mime,person_id,upload_item_id,actor=None):
 mime=storage.validate_file(raw,declared_mime);qid=uid();object_key=secrets.token_bytes(32);nonce=secrets.token_bytes(12);aad=('quarantine:'+qid).encode();ciphertext=nonce+AESGCM(object_key).encrypt(nonce,raw,aad);target=_folder()/(qid+'.qenc');fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:
  with os.fdopen(fd,'wb') as stream:stream.write(ciphertext);stream.flush();os.fsync(stream.fileno())
  return db.insert('quarantine_files',{'id':qid,'upload_item_id':upload_item_id,'person_id':person_id,'name':Path(str(name or 'document')).name[:180],'mime':mime,'size':len(raw),'storage_path':str(target),'content_key':encrypt(base64.b64encode(object_key).decode(),'quarantine.key:'+qid),'status':'awaiting_scan','scanner':None,'scanner_state':None,'scanned_at':None,'promoted_file_id':None,'expires_at':(datetime.now(timezone.utc)+timedelta(hours=QUARANTINE_HOURS)).isoformat(),'destroyed_at':None},actor)
 except Exception:_wipe(target);raise
def read_staged(row):
 if row.get('status') in ('destroyed','rejected','promoted'):raise AppError('Карантинный файл уже уничтожен',409)
 try:
  encoded=decrypt(row['content_key'],'quarantine.key:'+row['id']);key=base64.b64decode(encoded,validate=True);payload=_path(row).read_bytes();plain=AESGCM(key).decrypt(payload[:12],payload[12:],('quarantine:'+row['id']).encode())
  if len(plain)!=row['size']:raise ValueError()
  return plain
 except Exception:raise AppError('Карантинный файл повреждён или недоступен',409)
def scan_event(db,row,scanner,result,duration_ms,error_code=None,actor=None):
 checked=now();db.insert('file_scan_events',{'quarantine_id':row['id'],'scanner':scanner.info.name,'scanner_state':scanner.info.state,'result':result,'duration_ms':max(0,int(duration_ms)),'error_code':error_code,'checked_at':checked},actor);status={'clean':'scan_clean','infected':'rejected'}.get(result,'scan_blocked');return db.update('quarantine_files',row['id'],{'status':status,'scanner':scanner.info.name,'scanner_state':scanner.info.state,'scanned_at':checked})
def destroy_staged(db,row,status='destroyed'):
 _wipe(_path(row));return db.update('quarantine_files',row['id'],{'status':status,'content_key':None,'destroyed_at':now()})
def promote_file(db,row,entity_type,entity_id,actor=None):
 current=db.one('quarantine_files',row['id'])
 if current['status']!='scan_clean':raise AppError('Файл нельзя использовать до успешной антивирусной проверки',409)
 raw=read_staged(current);final=storage.store_file(db,raw,current['name'],current['mime'],entity_type,entity_id,actor);_wipe(_path(current));db.update('quarantine_files',current['id'],{'status':'promoted','content_key':None,'promoted_file_id':final['id'],'destroyed_at':now()});return final
def purge_expired(db,at=None):
 at=at or datetime.now(timezone.utc);purged=[]
 for row in db.all('quarantine_files'):
  if row.get('content_key') and datetime.fromisoformat(row['expires_at'])<=at:destroy_staged(db,row,'expired');purged.append(row['id'])
 return purged
