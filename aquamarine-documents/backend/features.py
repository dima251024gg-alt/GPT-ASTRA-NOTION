"""HTTP-маршруты документооборота: генерация, пакеты, OCR и выдача файлов."""
import base64,re
from . import docflow,render,rules,workflow
from . import templates as registry
from .security import AppError
DEAL_DOCUMENTS=re.compile(r"/api/deals/([^/]+)/documents");DEAL_STATUS=re.compile(r"/api/deals/([^/]+)/status");DEAL_VALIDATIONS=re.compile(r"/api/deals/([^/]+)/validations");DEAL_PREVIEW=re.compile(r"/api/deals/([^/]+)/documents/preview");DEAL_PACKAGES=re.compile(r"/api/deals/([^/]+)/packages");DOCUMENT_FILE=re.compile(r"/api/documents/([^/]+)/file");PACKAGE_FILE=re.compile(r"/api/packages/([^/]+)/file");PACKAGE_LINK=re.compile(r"/api/packages/([^/]+)/link");OCR_BATCH=re.compile(r"/api/ocr/batches/([^/]+)");OCR_CONFIRM=re.compile(r"/api/ocr/runs/([^/]+)/confirm");OCR_MANUAL_FROM=re.compile(r"/api/ocr/runs/([^/]+)/manual");MAX_LINK_DAYS=90
def only(method,*allowed):
 if method not in allowed:raise AppError('Метод '+str(method)+' для этого адреса не поддерживается',405,{'allowed':list(allowed)})
def flag(data,name):
 value=data.get(name);return value if isinstance(value,bool) else str(value or '').strip().lower() in ('1','true','yes','да')
def param(q,name,default=''):
 values=(q or {}).get(name) or [];return str(values[0]) if values else default
def guard(service,deal_id):
 deal=service.db.one('deals',deal_id)
 if not service.can('deals',deal):raise AppError('Сделка доступна только своему менеджеру и руководству',403)
 return deal
def file_payload(blob,meta,fallback):return {'name':str((meta or {}).get('name') or fallback),'mime':(meta or {}).get('mime') or 'application/octet-stream','size':len(blob),'sha256':(meta or {}).get('sha256') or '','content_base64':base64.b64encode(blob).decode('ascii')}
def registry_view():return {'templates':[{'type':item['type'],'name':item['name'],'format':item.get('format') or 'pdf+docx','required_fields':list(item.get('required_fields') or []),'condition':registry.CONDITIONAL.get(item['type'],''),'manual_only':item['type'] in docflow.MANUAL_ONLY,'event_driven':item['type'] in registry.EVENT_DRIVEN} for item in registry.TEMPLATES],'packages':[{'name':name,'title':package['title'],'templates':list(package.get('templates') or []),'files':list(package.get('files') or [])} for name,package in registry.PACKAGES.items()]}
def preview(service,deal_id,code,person_id,extra):
 guard(service,deal_id);template=docflow.template_row(service.db,code);context=docflow.build_context(service.db,deal_id,extra,person_id or None);missing=list(render.missing_fields(template,context));return {'template':code,'name':template['name'],'ready':not missing,'missing':missing,'text':'' if missing else render.render_template(template,context)}
def dispatch_features(service,method,path,q,data):
 db=service.db
 from .person_merge import dispatch_person_merges
 merge_result=dispatch_person_merges(service,method,path,data)
 if merge_result is not None:return merge_result
 if path=='/api/ocr/batches':
  only(method,'POST');from .ocr_pipeline import process_batch
  return process_batch(service,data.get('items'),data.get('operation_key'))
 if path=='/api/ocr/manual':
  only(method,'POST');from .manual_ocr import create_manual_run
  return create_manual_run(service,data.get('person_id'),data.get('fields'),data.get('operation_key'))
 match=OCR_BATCH.fullmatch(path)
 if match:
  only(method,'GET');from .ocr_pipeline import batch_view
  return batch_view(service,match[1])
 match=OCR_CONFIRM.fullmatch(path)
 if match:
  only(method,'POST');from .ocr_pipeline import confirm_run
  return confirm_run(service,match[1],data.get('confirmations'))
 match=OCR_MANUAL_FROM.fullmatch(path)
 if match:
  only(method,'POST');from .manual_ocr import create_manual_run
  return create_manual_run(service,data.get('person_id'),data.get('fields'),data.get('operation_key'),match[1])
 if path=='/api/templates/registry':only(method,'GET');return registry_view()
 if path=='/api/templates/sync':only(method,'POST');service.require('admin','senior');return docflow.sync_templates(db,service.actor)
 if path=='/api/jobs/daily':only(method,'POST');return workflow.daily_run(service,str(data.get('date') or '') or None,str(data.get('name') or 'daily'))
 match=DEAL_STATUS.fullmatch(path)
 if match:
  only(method,'GET','POST');deal_id=match[1];deal=guard(service,deal_id)
  if method=='GET':return {'status':deal['status'],'next':workflow.statuses_after(deal['status']),'summary':rules.summary(rules.check_deal(db,deal_id))}
  status=str(data.get('status') or '').strip()
  if not status:raise AppError('Не указан новый статус сделки')
  return workflow.transition(service,deal_id,status,str(data.get('date') or '') or None,flag(data,'force'),str(data.get('reason') or ''))
 match=DEAL_VALIDATIONS.fullmatch(path)
 if match:
  only(method,'GET');deal_id=match[1];deal=guard(service,deal_id);on=param(q,'date') or None;issues=rules.check_deal(db,deal_id,on);stops={item['code'] for item in rules.blockers(issues)};return {'status':deal['status'],'date':rules.today(on),'issues':issues,'summary':rules.summary(issues),'next':workflow.statuses_after(deal['status']),'gates':[{'status':name,'ready':not(set(codes)&stops),'blocking':sorted(set(codes)&stops)} for name,codes in rules.STATUS_GATES.items()]}
 match=DEAL_PREVIEW.fullmatch(path)
 if match:
  only(method,'GET','POST');code=str(data.get('template') or param(q,'template'))
  if not code:raise AppError('Не указан шаблон документа')
  return preview(service,match[1],code,data.get('person_id') or param(q,'person_id'),data.get('extra'))
 match=DEAL_DOCUMENTS.fullmatch(path)
 if match:
  only(method,'GET','POST');deal_id=match[1]
  if method=='GET':return {'items':docflow.deal_documents(service,deal_id)}
  code=str(data.get('template') or data.get('type') or '')
  if not code:raise AppError('Не указан шаблон документа')
  document=docflow.generate(service,deal_id,code,data.get('extra'),data.get('person_id'),flag(data,'force'));return {'document':service.public('documents',document),'items':docflow.deal_documents(service,deal_id)}
 match=DEAL_PACKAGES.fullmatch(path)
 if match:
  only(method,'GET','POST');deal_id=match[1]
  if method=='GET':guard(service,deal_id);return {'items':docflow.packages_for_deal(db,deal_id)}
  name=str(data.get('name') or data.get('type') or '')
  if not name:raise AppError('Не указан пакет документов')
  package=docflow.build_package(service,deal_id,name,flag(data,'force'));return {'package':package,'items':docflow.packages_for_deal(db,deal_id)}
 match=DOCUMENT_FILE.fullmatch(path)
 if match:
  only(method,'GET');kind=param(q,'kind','pdf')
  if kind not in ('pdf','docx'):raise AppError('Документ отдаётся только в форматах pdf или docx')
  blob,meta=docflow.document_file(service,match[1],kind);return {'file':file_payload(blob,meta,match[1]+'.'+kind)}
 match=PACKAGE_FILE.fullmatch(path)
 if match:only(method,'GET');blob,meta=docflow.package_file(service,match[1]);return {'file':file_payload(blob,meta,match[1]+'.pdf')}
 match=PACKAGE_LINK.fullmatch(path)
 if match:
  only(method,'POST')
  try:days=int(data.get('days') or 30)
  except(TypeError,ValueError):raise AppError('Срок действия ссылки указывается в днях')
  if days<1 or days>MAX_LINK_DAYS:raise AppError('Срок действия ссылки — от 1 до '+str(MAX_LINK_DAYS)+' дней')
  link,url=docflow.issue_link(service,match[1],days,data.get('person_id'));return {'link':{'id':link['id'],'deal_id':link.get('deal_id'),'package_id':link.get('package_id'),'expires_at':link.get('expires_at')},'url':url}
 return None
