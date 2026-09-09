"""Безопасный пакетный OCR-конвейер R1: без фиктивных успехов и автослияния."""
import base64, hashlib, os, re, shutil, subprocess, tempfile, time
from dataclasses import dataclass
from pathlib import Path
from . import config, ocr, storage
from .db import now
from .security import AppError, blind
from .service import document_key, identity_key
MAX_FILES=20
INTEGRATION_STATES={'available','unavailable','demo'}
CRITICAL_FIELDS={'number','birth_date','expiry_date','last_name_latin','first_name_latin','type','issuing_country'}
ALLOWED_FIELDS=CRITICAL_FIELDS|{'series','issue_date','issued_by','division_code','citizenship','gender','last_name_ru','first_name_ru','patronymic_ru','birth_place','mrz_raw','last_name_latin_visual','first_name_latin_visual','possible_truncation'}
MRZ_FIELDS={'number','birth_date','expiry_date','last_name_latin','first_name_latin','issuing_country','citizenship','gender','mrz_raw'}
@dataclass(frozen=True)
class AdapterInfo:
    name:str; version:str; state:str
    def __post_init__(self):
        if self.state not in INTEGRATION_STATES: raise ValueError('Некорректное состояние интеграции')
class UnavailableAntivirus:
    info=AdapterInfo('antivirus','none','unavailable')
    def scan(self,raw,mime): raise AppError('Антивирус недоступен: файл остаётся в карантине',503)
class DemoAntivirus:
    info=AdapterInfo('antivirus-demo','1','demo')
    def scan(self,raw,mime):
        if config.PRODUCTION: raise AppError('Демонстрационный антивирус запрещён в production',503)
        return 'clean'
class ClamAntivirus:
    def __init__(self):
        self.binary=shutil.which('clamscan'); self.info=AdapterInfo('clamav','cli','available' if self.binary else 'unavailable')
    def scan(self,raw,mime):
        if not self.binary: raise AppError('Антивирус недоступен: файл остаётся в карантине',503)
        with tempfile.TemporaryDirectory(prefix='aq-av-') as folder:
            path=Path(folder)/'sample'; path.write_bytes(raw)
            result=subprocess.run([self.binary,'--no-summary',str(path)],capture_output=True,timeout=30,check=False,env={'PATH':os.environ.get('PATH','')})
        if result.returncode==0:return 'clean'
        if result.returncode==1:return 'infected'
        raise AppError('Антивирус не завершил проверку файла',503)
class UnavailableOCR:
    info=AdapterInfo('ocr','none','unavailable')
    def recognize(self,raw,mime): raise AppError('OCR недоступен. Используйте ручной ввод.',503)
class BuiltinOCR:
    def __init__(self):
        mode=os.getenv('OCR_PROVIDER','local'); demo=config.PROVIDER_MODE=='mock' and not config.PRODUCTION
        available=mode=='russian-api' or bool(shutil.which('tesseract')); state='demo' if demo else ('available' if available else 'unavailable')
        self.mode=mode; self.info=AdapterInfo('builtin-'+mode,'1',state)
    def recognize(self,raw,mime):
        if self.info.state=='unavailable': raise AppError('OCR недоступен. Используйте ручной ввод.',503)
        return ocr.recognizeDocument(raw,mime,self.mode)
def default_antivirus(): return DemoAntivirus() if config.PROVIDER_MODE=='mock' and not config.PRODUCTION else ClamAntivirus()
def clean_text(value,limit=500):
    text=str(value or '').strip()
    if len(text)>limit: raise AppError('Результат OCR содержит слишком длинное поле')
    return text
def safe_confidence(values):
    result={}
    for name,value in (values or {}).items():
        if name not in ALLOWED_FIELDS: continue
        result[name]=str(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and 0<=value<=1 else None
    return result
def normalize_name(value): return re.sub(r'[^A-ZА-Я0-9]','',str(value or '').upper().replace('Ё','Е'))
def record_call(db,adapter,operation,key,attempt,status,started,error_code=None,actor=None):
    return db.insert('integration_calls',{'adapter':adapter.info.name,'operation':operation,'idempotency_key':blind(key),'attempt':attempt,'status':status,'duration_ms':max(0,int((time.monotonic()-started)*1000)),'error_code':error_code},actor)
def duplicate_hints(service,fields,current_person_id):
    db=service.db; hints=[]; number=clean_text(fields.get('number')); doc_type=clean_text(fields.get('type'))
    if number and doc_type:
        candidate={'number':number,'series':clean_text(fields.get('series')),'type':doc_type,'issuing_country':clean_text(fields.get('issuing_country')) or 'RUS'}
        match=db.find('identity_documents','document_key=?',(document_key(candidate),))
        if match:
            person=db.one('persons',match['person_id'])
            if service.can('persons',person): hints.append({'kind':'document_exact','candidate_person_id':person['id'],'same_person':person['id']==current_person_id,'selectable':True})
            else: hints.append({'kind':'document_exact_restricted','selectable':False})
    if all(fields.get(k) for k in ('last_name_ru','first_name_ru','birth_date')):
        probe={k:fields.get(k) for k in ('last_name_ru','first_name_ru','patronymic_ru','birth_date')}
        for person in db.all('persons','identity_key=?',(identity_key(probe),)):
            if person['id']!=current_person_id and service.can('persons',person): hints.append({'kind':'name_birth_possible','candidate_person_id':person['id'],'same_person':False,'selectable':True})
    return hints
def field_rows(result):
    fields=result.get('fields') if isinstance(result,dict) else None
    if not isinstance(fields,dict): raise AppError('OCR вернул ответ неизвестного формата',502)
    confidence=safe_confidence(result.get('confidence')); provenance=result.get('provenance') if isinstance(result.get('provenance'),dict) else {}; has_mrz=bool(fields.get('mrz_raw')); truncation=bool(fields.get('possible_truncation')); rows=[]
    for name,value in fields.items():
        if name not in ALLOWED_FIELDS or name=='possible_truncation' or value in (None,''): continue
        value=clean_text(value,4000 if name=='mrz_raw' else 500); source=provenance.get(name)
        if source not in ('mrz','visual','manual'): source='mrz' if has_mrz and name in MRZ_FIELDS else 'visual'
        visual=fields.get(name+'_visual') if name in ('last_name_latin','first_name_latin') else None
        rows.append({'name':name,'value':value,'source':source,'raw_confidence':confidence.get(name),'confidence_origin':clean_text(result.get('confidence_origin') or 'provider',80),'calibrated_confidence':None,'critical':int(name in CRITICAL_FIELDS),'confirmed':0,'conflict':int(bool(visual and normalize_name(visual)!=normalize_name(value))),'possible_truncation':int(truncation and name in ('last_name_latin','first_name_latin'))})
    return rows,confidence
def warning_codes(result):
    codes=[]; fields=result.get('fields') or {}
    if not result.get('mrz_valid') and fields.get('mrz_raw'): codes.append('mrz_check_failed')
    if fields.get('possible_truncation'): codes.append('possible_truncation')
    if result.get('warnings'): codes.append('provider_warning_review_required')
    return sorted(set(codes))
def ensure_batch_access(service,batch):
    if batch.get('requested_by')!=service.actor and service.user.get('role') not in {'admin','senior','director','senior_manager'}: raise AppError('Пакет загрузки не найден или недоступен',404)
def batch_view(service,batch_id):
    batch=service.db.one('upload_batches',batch_id); ensure_batch_access(service,batch); items=[]
    for item in sorted(service.db.all('upload_items','batch_id=?',(batch_id,)),key=lambda row:row['ordinal']):
        data={k:item.get(k) for k in ('id','ordinal','name','file_id','mime','status','error_code','ocr_run_id','duplicate_of')}
        if item.get('ocr_run_id'):
            run=service.db.one('ocr_runs',item['ocr_run_id']); rows=service.db.all('ocr_fields','ocr_run_id=?',(run['id'],)); confirmed={x['field_name'] for x in service.db.all('ocr_confirmations','ocr_run_id=?',(run['id'],))}
            data['ocr']={'id':run['id'],'provider':run['provider'],'provider_version':run['provider_version'],'integration_state':run['integration_state'],'status':'confirmed' if all(not r['critical'] or r['name'] in confirmed for r in rows) else run['status'],'warning_codes':run.get('warnings') or [],'dedupe_hints':run.get('dedupe_hints') or [],'fields':[{'name':r['name'],'value':r['value'],'source':r['source'],'raw_confidence':r['raw_confidence'],'confidence_origin':r['confidence_origin'],'calibrated_confidence':r['calibrated_confidence'],'critical':bool(r['critical']),'confirmed':r['name'] in confirmed,'conflict':bool(r['conflict']),'possible_truncation':bool(r['possible_truncation'])} for r in rows]}
        items.append(data)
    return {'id':batch['id'],'status':batch['status'],'item_count':batch['item_count'],'finished_at':batch['finished_at'],'items':items}
def _run_ocr(service,item_row,raw,mime,person_id,adapter,operation_key):
    db=service.db; state=adapter.info.state; run_key=f"{operation_key}:ocr:{item_row['ordinal']}"; common={'file_id':item_row['file_id'],'person_id':person_id,'provider':adapter.info.name,'provider_version':adapter.info.version,'provider_request_id':None,'integration_state':state,'confidence_origin':'provider','calibrated_confidence':None,'idempotency_key':blind(run_key)}
    if state=='unavailable': return db.insert('ocr_runs',{**common,'raw_confidence':{},'status':'manual_required','warnings':['ocr_unavailable'],'dedupe_hints':[]},service.actor)
    if state=='demo' and config.PRODUCTION: return db.insert('ocr_runs',{**common,'raw_confidence':{},'status':'manual_required','warnings':['demo_forbidden'],'dedupe_hints':[]},service.actor)
    started=time.monotonic()
    try:
        result=adapter.recognize(raw,mime); rows,confidence=field_rows(result); status='manual_required' if result.get('provider')=='manual' or not rows else 'review_required'; record_call(db,adapter,'recognize',run_key,1,'succeeded',started,actor=service.actor)
        run=db.insert('ocr_runs',{**common,'raw_confidence':confidence,'status':status,'warnings':warning_codes(result),'dedupe_hints':duplicate_hints(service,result.get('fields') or {},person_id)},service.actor)
        for row in rows: db.insert('ocr_fields',{'ocr_run_id':run['id'],**row},service.actor)
        return run
    except Exception as exc:
        record_call(db,adapter,'recognize',run_key,1,'failed',started,type(exc).__name__[:80],service.actor)
        return db.insert('ocr_runs',{**common,'raw_confidence':{},'status':'manual_required','warnings':['ocr_failed'],'dedupe_hints':[]},service.actor)
def process_batch(service,items,operation_key,adapter=None,antivirus=None):
    service.staff()
    if not isinstance(items,list) or not 1<=len(items)<=MAX_FILES: raise AppError('За одну операцию можно загрузить от 1 до 20 файлов')
    if not re.fullmatch(r'[A-Za-z0-9._:-]{8,120}',str(operation_key or '')): raise AppError('Укажите корректный ключ идемпотентности операции')
    key=blind('ocr-batch:'+operation_key); existing=service.db.find('upload_batches','operation_key=?',(key,))
    if existing: ensure_batch_access(service,existing); return batch_view(service,existing['id'])
    adapter=adapter or BuiltinOCR(); antivirus=antivirus or default_antivirus(); batch=service.db.insert('upload_batches',{'operation_key':key,'requested_by':service.actor,'item_count':len(items),'status':'processing','finished_at':None},service.actor); seen={}; statuses=[]
    for ordinal,item in enumerate(items):
        item_row=None
        try:
            if not isinstance(item,dict): raise AppError('Элемент загрузки должен быть объектом')
            person_id=str(item.get('person_id') or ''); service.get('persons',person_id,True)
            try: raw=base64.b64decode(item.get('content_base64',''),validate=True)
            except Exception: raise AppError('Не удалось прочитать содержимое файла')
            mime=storage.validate_file(raw,item.get('mime')); digest=hashlib.sha256(raw).hexdigest()
            if digest in seen:
                service.db.insert('upload_items',{'batch_id':batch['id'],'ordinal':ordinal,'name':clean_text(item.get('name') or 'document',180),'file_id':None,'content_hash':blind(digest),'mime':mime,'status':'duplicate_in_batch','error_code':None,'ocr_run_id':None,'duplicate_of':seen[digest]},service.actor); statuses.append('duplicate_in_batch'); continue
            seen[digest]=ordinal; item_row=service.db.insert('upload_items',{'batch_id':batch['id'],'ordinal':ordinal,'name':clean_text(item.get('name') or 'document',180),'file_id':None,'content_hash':blind(digest),'mime':mime,'status':'quarantine','error_code':None,'ocr_run_id':None,'duplicate_of':None},service.actor)
            if antivirus.info.state=='unavailable': raise AppError('Антивирус недоступен: файл остаётся в карантине',503)
            if antivirus.info.state=='demo' and config.PRODUCTION: raise AppError('Демонстрационный антивирус запрещён в production',503)
            started=time.monotonic()
            try: scan=antivirus.scan(raw,mime); record_call(service.db,antivirus,'scan',f'{operation_key}:av:{ordinal}',1,'succeeded',started,actor=service.actor)
            except Exception as exc: record_call(service.db,antivirus,'scan',f'{operation_key}:av:{ordinal}',1,'failed',started,type(exc).__name__[:80],service.actor); raise
            if scan!='clean': raise AppError('Файл отклонён антивирусной проверкой',422)
            file_row=storage.store_file(service.db,raw,item_row['name'],mime,'persons',person_id,service.actor); service.db.update('upload_items',item_row['id'],{'file_id':file_row['id'],'status':'scanned'}); item_row=service.db.one('upload_items',item_row['id']); run=_run_ocr(service,item_row,raw,mime,person_id,adapter,operation_key); status=run['status']; service.db.update('upload_items',item_row['id'],{'ocr_run_id':run['id'],'status':status}); statuses.append(status)
        except AppError as exc:
            status='quarantine_blocked' if exc.status==503 else 'rejected'; statuses.append(status)
            if item_row: service.db.update('upload_items',item_row['id'],{'status':status,'error_code':type(exc).__name__})
            else: service.db.insert('upload_items',{'batch_id':batch['id'],'ordinal':ordinal,'name':'Недоступный файл','file_id':None,'content_hash':None,'mime':None,'status':status,'error_code':type(exc).__name__,'ocr_run_id':None,'duplicate_of':None},service.actor)
    final='completed' if all(x in ('review_required','manual_required','duplicate_in_batch') for x in statuses) else 'partial'; service.db.update('upload_batches',batch['id'],{'status':final,'finished_at':now()}); service.audit('ocr_batch_processed','upload_batches',batch['id'],{'item_count':len(items),'status_counts':{x:statuses.count(x) for x in set(statuses)}}); return batch_view(service,batch['id'])
def confirm_run(service,run_id,confirmations):
    service.staff(); run=service.db.one('ocr_runs',run_id); person=service.get('persons',run['person_id'],True)
    if not isinstance(confirmations,dict): raise AppError('Подтверждения критических полей передаются поштучно')
    rows=service.db.all('ocr_fields','ocr_run_id=?',(run_id,)); critical=[row for row in rows if row['critical']]; missing=[]
    for row in critical:
        answer=confirmations.get(row['name'])
        if not isinstance(answer,dict) or answer.get('confirmed') is not True: missing.append(row['name']); continue
        if row['conflict'] and answer.get('conflict_ack') is not True: missing.append(row['name']+':расхождение')
        if row['possible_truncation'] and answer.get('truncation_ack') is not True: missing.append(row['name']+':усечение')
    if missing: raise AppError('Подтвердите критические поля по отдельности',422,{'fields':missing})
    existing={x['field_name'] for x in service.db.all('ocr_confirmations','ocr_run_id=?',(run_id,))}
    for row in critical:
        if row['name'] in existing: continue
        answer=confirmations[row['name']]; service.db.insert('ocr_confirmations',{'ocr_run_id':run_id,'field_name':row['name'],'value_hash':blind(row['value']),'confirmed_by':service.actor,'confirmed_at':now(),'conflict_ack':int(bool(answer.get('conflict_ack'))),'truncation_ack':int(bool(answer.get('truncation_ack')))},service.actor)
    service.audit('ocr_fields_confirmed','ocr_runs',run_id,{'field_names':sorted(row['name'] for row in critical)}); return {'id':run_id,'status':'confirmed','person_id':person['id']}
