import base64, hashlib, hmac, json, secrets, struct, time, re
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .config import DATA_KEY, TOKEN_KEY

class AppError(Exception):
    def __init__(self, message, status=400, details=None):
        super().__init__(message); self.message=message; self.status=status; self.details=details

def canonical(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
def encrypt(value, context):
    nonce=secrets.token_bytes(12)
    return "v1:"+base64.b64encode(nonce+AESGCM(DATA_KEY).encrypt(nonce,str(value).encode(),context.encode())).decode()
def decrypt(value, context):
    if value is None: return None
    if not isinstance(value,str) or not value.startswith("v1:"): raise AppError("Повреждены зашифрованные данные",500)
    b=base64.b64decode(value[3:]); return AESGCM(DATA_KEY).decrypt(b[:12],b[12:],context.encode()).decode()
def blind(value): return hmac.new(TOKEN_KEY,str(value).encode(),hashlib.sha256).hexdigest()
def token(): return secrets.token_urlsafe(32)
def hash_password(password):
    if len(password)<12 or not re.search(r"[A-Za-zА-Яа-я]",password) or not re.search(r"\d",password):
        raise AppError("Пароль: минимум 12 символов, буквы и цифры")
    salt=secrets.token_bytes(16)
    digest=hashlib.scrypt(password.encode(),salt=salt,n=32768,r=8,p=1,maxmem=64*1024*1024)
    return base64.b64encode(salt+digest).decode()
def verify_password(password, stored):
    try:
        data=base64.b64decode(stored)
        value=hashlib.scrypt(password.encode(),salt=data[:16],n=32768,r=8,p=1,maxmem=64*1024*1024)
        return hmac.compare_digest(value,data[16:])
    except (ValueError,TypeError): return False

def new_totp_secret(): return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
def totp(secret, counter=None):
    counter=int(time.time()//30) if counter is None else counter
    key=base64.b32decode(secret+"="*((8-len(secret)%8)%8))
    d=hmac.new(key,struct.pack(">Q",counter),hashlib.sha1).digest(); off=d[-1]&15
    return str((struct.unpack(">I",d[off:off+4])[0]&0x7fffffff)%1000000).zfill(6)
def check_totp(secret, code, last_step=-1):
    current=int(time.time()//30)
    for step in (current-1,current,current+1):
        if step>last_step and hmac.compare_digest(totp(secret,step),str(code)): return step
    raise AppError("Неверный или уже использованный код аутентификатора",401)
def mask(value):
    s=str(value or "")
    return "•"*max(4,len(s)-2)+s[-2:] if s else "—"
def phone(value):
    p=re.sub(r"\D","",str(value or ""))
    if len(p)==11 and p.startswith("8"): p="7"+p[1:]
    if not 10<=len(p)<=15: raise AppError("Укажите телефон в международном формате")
    return "+"+p
