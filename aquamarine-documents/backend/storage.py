import base64, hashlib, io, os, secrets
from pathlib import Path
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from . import config
from .security import AppError
from .db import uid

PDF='application/pdf'; DOCX='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
def validate_file(raw,declared=None):
    if not raw or len(raw)>config.MAX_UPLOAD: raise AppError('Файл пустой или превышает 20 МБ')
    if raw.startswith(b'%PDF-'):
        from pypdf import PdfReader
        try:
            pdf=PdfReader(io.BytesIO(raw))
            if pdf.is_encrypted or len(pdf.pages)>100: raise ValueError()
            root=pdf.trailer.get('/Root',{})
            if any(k in root for k in ('/OpenAction','/AA','/JavaScript','/EmbeddedFiles')): raise ValueError()
        except Exception: raise AppError('PDF повреждён, защищён паролем или содержит активное содержимое')
        return PDF
    if raw.startswith(b'\xff\xd8\xff') or raw.startswith(b'\x89PNG\r\n\x1a\n'):
        from PIL import Image
        try:
            with Image.open(io.BytesIO(raw)) as im:
                if im.width*im.height>40000000: raise ValueError()
                im.verify()
        except Exception: raise AppError('Изображение повреждено или слишком большое')
        return 'image/jpeg' if raw.startswith(b'\xff') else 'image/png'
    raise AppError('Допускаются только JPEG, PNG и PDF. Тип определяется по содержимому, а не имени.')

def store_file(db,raw,name,mime,entity_type,entity_id,actor=None,generated=False):
    if not generated: mime=validate_file(raw,mime)
    if len(raw)>80*1024*1024: raise AppError('Сформированный файл превышает 80 МБ')
    id=uid(); folder=config.VAR/'files';folder.mkdir(exist_ok=True,mode=0o700)
    nonce=secrets.token_bytes(12); ciphertext=nonce+AESGCM(config.DATA_KEY).encrypt(nonce,raw,('file:'+id).encode())
    target=folder/(id+'.enc'); fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f: f.write(ciphertext);f.flush();os.fsync(f.fileno())
    return db.insert('files',{'id':id,'name':Path(name).name[:180],'mime':mime,'size':len(raw),'storage_path':str(target),'encrypted':1,'retention_until':(datetime.now(timezone.utc)+timedelta(days=365*config.RETENTION_YEARS)).isoformat(),'uploaded_by':actor,'entity_type':entity_type,'entity_id':entity_id,'sha256':hashlib.sha256(raw).hexdigest()},actor)

def read_file(db,id):
    row=db.one('files',id); path=Path(row['storage_path']).resolve()
    if not path.is_relative_to((config.VAR/'files').resolve()): raise AppError('Недопустимый путь к файлу',403)
    try:
        raw=path.read_bytes(); plain=AESGCM(config.DATA_KEY).decrypt(raw[:12],raw[12:],('file:'+id).encode())
        if hashlib.sha256(plain).hexdigest()!=row['sha256']: raise ValueError()
        return plain,row
    except (OSError,ValueError): raise AppError('Файл недоступен или не прошёл проверку целостности',409)

def decode_upload(data):
    try: raw=base64.b64decode(data.get('content',''),validate=True)
    except Exception: raise AppError('Не удалось прочитать загруженный файл')
    return raw,validate_file(raw,data.get('mime'))

def gc_orphans(db):
    known={Path(f['storage_path']).name for f in db.all('files',include_deleted=True)}
    import time
    for path in (config.VAR/'files').glob('*.enc'):
        if path.name not in known and time.time()-path.stat().st_mtime>86400: path.unlink()
