"""Запуск доверенного локального OCR в отдельном процессе с лимитами ресурсов."""
import base64,json,os,resource,signal,subprocess,sys,tempfile
from pathlib import Path
from . import config
from .security import AppError
CPU_SECONDS=45;MEMORY_BYTES=1536*1024*1024;OUTPUT_BYTES=2*1024*1024
def _limits():
 resource.setrlimit(resource.RLIMIT_CPU,(CPU_SECONDS,CPU_SECONDS));resource.setrlimit(resource.RLIMIT_AS,(MEMORY_BYTES,MEMORY_BYTES));resource.setrlimit(resource.RLIMIT_FSIZE,(config.MAX_UPLOAD*2,config.MAX_UPLOAD*2));resource.setrlimit(resource.RLIMIT_NOFILE,(64,64));resource.setrlimit(resource.RLIMIT_CORE,(0,0))
def worker_environment(data_dir):return {'PATH':os.environ.get('PATH','/usr/bin:/bin'),'LANG':'C.UTF-8','LC_ALL':'C.UTF-8','HOME':str(data_dir),'TMPDIR':str(data_dir),'DATA_DIR':str(data_dir),'APP_ENV':'ocr-worker','PROVIDER_MODE':'local','OCR_PROVIDER':'local'}
def isolated_recognize(raw,mime,timeout=55):
 if not raw or len(raw)>config.MAX_UPLOAD:raise AppError('Файл превышает лимит изолированного OCR')
 if mime not in ('image/jpeg','image/png','application/pdf'):raise AppError('Формат не поддерживается изолированным OCR')
 root=str(Path(__file__).resolve().parents[1]);dependency_paths=[path for path in sys.path if path and 'site-packages' in path and Path(path).is_dir()];bootstrap='import runpy,sys;sys.path[:0]='+repr([root,*dependency_paths])+";runpy.run_module('backend.ocr_worker',run_name='__main__')";request=json.dumps({'mime':mime,'content':base64.b64encode(raw).decode('ascii')},separators=(',',':')).encode()
 with tempfile.TemporaryDirectory(prefix='aq-ocr-worker-') as folder:
  process=subprocess.Popen([sys.executable,'-I','-c',bootstrap],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,cwd=folder,env=worker_environment(folder),preexec_fn=_limits if os.name=='posix' else None,start_new_session=True)
  try:stdout,_=process.communicate(request,timeout=timeout)
  except subprocess.TimeoutExpired:
   if os.name=='posix':os.killpg(process.pid,signal.SIGKILL)
   else:process.kill()
   process.communicate();raise AppError('OCR превысил допустимое время обработки',503)
 if process.returncode!=0 or len(stdout)>OUTPUT_BYTES:raise AppError('Изолированный OCR завершился с ошибкой',503)
 try:payload=json.loads(stdout.decode('utf-8'))
 except(ValueError,UnicodeDecodeError):raise AppError('Изолированный OCR вернул некорректный ответ',503)
 if not payload.get('ok') or not isinstance(payload.get('result'),dict):raise AppError('OCR не распознал документ. Используйте ручной ввод.',422)
 return payload['result']
