"""Входная точка локального OCR-worker. Получает только байты документа через stdin."""
import base64,json,socket,sys
def disable_network():
 def denied(*args,**kwargs):raise OSError('network disabled in OCR worker')
 socket.socket=denied;socket.create_connection=denied;socket.getaddrinfo=denied
def main():
 try:
  request=json.loads(sys.stdin.buffer.read(30*1024*1024).decode('utf-8'));raw=base64.b64decode(request['content'],validate=True);mime=str(request['mime'])
  from .ocr import recognizeDocument
  disable_network();result=recognizeDocument(raw,mime,'local');safe={'fields':result.get('fields') or {},'confidence':result.get('confidence') or {},'warnings':['worker_review_required'] if result.get('warnings') else [],'provider':result.get('provider') or 'local','mrz_valid':bool(result.get('mrz_valid')),'manual_required':bool(result.get('manual_required',True)),'confidence_origin':'local-worker-v1'};print(json.dumps({'ok':True,'result':safe},ensure_ascii=False))
 except Exception as exc:print(json.dumps({'ok':False,'error':'recognition_failed','error_code':type(exc).__name__}))
if __name__=='__main__':main()
