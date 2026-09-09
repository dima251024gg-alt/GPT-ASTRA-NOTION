"""Штатный ручной ввод документа без имитации OCR-успеха.
Ручная редакция создаёт новую попытку и не изменяет исходный OCR-результат.
"""
import re
from datetime import date
from .ocr_pipeline import ALLOWED_FIELDS,CRITICAL_FIELDS,clean_text,duplicate_hints
from .security import AppError,blind
DOCUMENT_TYPES={'passport_rf','international_passport','birth_certificate','visa','residence_permit','other'}
BASE_REQUIRED={'type','number','birth_date'}
PASSPORT_REQUIRED={'type','number','birth_date','expiry_date','issuing_country','last_name_latin','first_name_latin'}
def _operation_key(value):
 value=str(value or '')
 if not re.fullmatch(r'[A-Za-z0-9._:-]{8,120}',value):raise AppError('Укажите корректный ключ идемпотентности ручного ввода')
 return blind('manual-ocr:'+value)
def _validate(fields):
 if not isinstance(fields,dict):raise AppError('Поля ручного ввода должны быть объектом')
 unknown=sorted(set(fields)-ALLOWED_FIELDS)
 if unknown:raise AppError('Недопустимые поля ручного ввода',422,{'fields':unknown})
 doc_type=clean_text(fields.get('type'),80)
 if doc_type not in DOCUMENT_TYPES:raise AppError('Выберите допустимый тип документа')
 required=PASSPORT_REQUIRED if doc_type=='international_passport' else BASE_REQUIRED
 missing=sorted(name for name in required if not clean_text(fields.get(name)))
 if missing:raise AppError('Заполните обязательные поля ручного ввода',422,{'fields':missing})
 result={}
 for name,value in fields.items():
  if value not in (None,'') and name!='possible_truncation':result[name]=clean_text(value,4000 if name=='mrz_raw' else 500)
 for name in ('birth_date','issue_date','expiry_date'):
  if result.get(name):
   try:parsed=date.fromisoformat(result[name])
   except ValueError:raise AppError('Некорректная календарная дата',422,{'field':name})
   if name=='birth_date' and parsed>date.today():raise AppError('Дата рождения не может быть в будущем',422)
 if not re.fullmatch(r'[A-Za-zА-Яа-яЁё0-9 /.-]{1,40}',result['number']):raise AppError('Номер документа содержит недопустимые символы',422)
 return result,bool(fields.get('possible_truncation'))
def run_view(service,run):
 service.get('persons',run['person_id'],True);rows=service.db.all('ocr_fields','ocr_run_id=?',(run['id'],));confirmed={row['field_name'] for row in service.db.all('ocr_confirmations','ocr_run_id=?',(run['id'],))}
 return {'id':run['id'],'person_id':run['person_id'],'provider':'manual','provider_version':run['provider_version'],'integration_state':run['integration_state'],'status':'confirmed' if all(not row['critical'] or row['name'] in confirmed for row in rows) else run['status'],'dedupe_hints':run.get('dedupe_hints') or [],'warning_codes':run.get('warnings') or [],'fields':[{'name':row['name'],'value':row['value'],'source':'manual','raw_confidence':None,'confidence_origin':'manual','calibrated_confidence':None,'critical':bool(row['critical']),'confirmed':row['name'] in confirmed,'conflict':bool(row['conflict']),'possible_truncation':bool(row['possible_truncation'])} for row in rows]}
def create_manual_run(service,person_id,fields,operation_key,source_run_id=None):
 service.staff();key=_operation_key(operation_key);existing=service.db.find('ocr_runs','idempotency_key=?',(key,))
 if existing:return run_view(service,existing)
 source=None;file_id=None
 if source_run_id:
  source=service.db.one('ocr_runs',str(source_run_id));service.get('persons',source['person_id'],True)
  if person_id and str(person_id)!=source['person_id']:raise AppError('Исходная попытка относится к другому туристу',409)
  person_id=source['person_id'];file_id=source.get('file_id')
 person_id=str(person_id or '');service.get('persons',person_id,True);values,possible_truncation=_validate(fields);hints=duplicate_hints(service,values,person_id)
 run=service.db.insert('ocr_runs',{'file_id':file_id,'person_id':person_id,'provider':'manual','provider_version':'1','provider_request_id':source['id'] if source else None,'integration_state':'available','confidence_origin':'manual','raw_confidence':{},'calibrated_confidence':None,'status':'review_required','warnings':['manual_values_require_confirmation'],'dedupe_hints':hints,'idempotency_key':key},service.actor)
 for name,value in values.items():service.db.insert('ocr_fields',{'ocr_run_id':run['id'],'name':name,'value':value,'source':'manual','raw_confidence':None,'confidence_origin':'manual','calibrated_confidence':None,'critical':int(name in CRITICAL_FIELDS),'confirmed':0,'confirmed_by':None,'confirmed_at':None,'conflict':0,'possible_truncation':int(possible_truncation and name in ('last_name_latin','first_name_latin'))},service.actor)
 if source:
  for item in service.db.all('upload_items','ocr_run_id=?',(source['id'],)):service.db.update('upload_items',item['id'],{'ocr_run_id':run['id'],'status':'review_required'})
 service.audit('manual_document_entered','ocr_runs',run['id'],{'field_names':sorted(values),'source_run':bool(source)});return run_view(service,run)
