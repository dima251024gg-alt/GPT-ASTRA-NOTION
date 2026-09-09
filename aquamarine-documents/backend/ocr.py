"""Local OCR / Russian HTTP provider / manual fallback with mandatory human review."""
import re, io, shutil, tempfile, subprocess, os
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path
from typing import Protocol
from .security import AppError

TRANSLIT=dict(zip('АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ',['A','B','V','G','D','E','E','ZH','Z','I','I','K','L','M','N','O','P','R','S','T','U','F','KH','TS','CH','SH','SHCH','IE','Y','','E','IU','IA']))
def transliterate(text): return ''.join(TRANSLIT.get(c,c) for c in text.upper())
def latin_normal(text): return re.sub(r'[ <\-]+',' ',str(text or '').upper()).strip()
def check_digit(text):
    values={**{str(i):i for i in range(10)},**{chr(65+i):10+i for i in range(26)},'<':0}
    try: return str(sum(values[c]*(7,3,1)[i%3] for i,c in enumerate(text))%10)
    except KeyError: raise AppError('Недопустимый символ в MRZ')
def mrz_date(value,birth=False,today=None):
    today=today or date.today()
    if not re.fullmatch(r'\d{6}',value): raise AppError('Дата MRZ требует ручной проверки')
    yy,mm,dd=int(value[:2]),int(value[2:4]),int(value[4:])
    options=[]
    for century in (1900,2000,2100):
        try:
            d=date(century+yy,mm,dd)
            if birth and d<=today and today.year-d.year<=120: options.append(d)
            elif not birth: options.append(d)
        except ValueError: pass
    if not options: raise AppError('Недопустимая дата в MRZ')
    return (max(options) if birth else min(options,key=lambda d:abs((d-today).days))).isoformat()

def parse_mrz(raw,today=None):
    lines=[re.sub(r'\s','',x.upper()) for x in raw.splitlines() if x.strip()]
    if len(lines)==1 and len(lines[0])==88: lines=[lines[0][:44],lines[0][44:]]
    if len(lines)!=2 or any(len(x)!=44 for x in lines): raise AppError('MRZ должна содержать две строки по 44 символа')
    a,b=lines
    if not a.startswith('P') or not re.fullmatch(r'[A-Z0-9<]{44}',a) or not re.fullmatch(r'[A-Z0-9<]{44}',b): raise AppError('Недопустимый формат MRZ TD3')
    surname,given=(a[5:].split('<<',1)+[''])[:2]
    checks={'number':check_digit(b[:9])==b[9], 'birth_date':check_digit(b[13:19])==b[19], 'expiry_date':check_digit(b[21:27])==b[27], 'personal_number':check_digit(b[28:42])==b[42] or (set(b[28:42])=={'<'} and b[42]=='<'), 'composite':check_digit(b[:10]+b[13:20]+b[21:43])==b[43]}
    fields={'type':'international_passport','number':b[:9].replace('<',''),'issuing_country':a[2:5],'citizenship':b[10:13],'last_name_latin':latin_normal(surname),'first_name_latin':latin_normal(given),'gender':b[20] if b[20] in 'MF' else 'X','mrz_raw':a+'\n'+b}
    date_errors=[]
    for key,value,is_birth in [('birth_date',b[13:19],True),('expiry_date',b[21:27],False)]:
        try: fields[key]=mrz_date(value,is_birth,today)
        except AppError:
            fields[key]=''; date_errors.append(key); checks[key]=False
    valid=all(checks.values()) and not date_errors
    confidence={k:(0.99 if valid else 0.65) for k in fields}
    confidence['last_name_latin']=confidence['first_name_latin']=0.89
    return {'fields':fields,'confidence':confidence,'mrz_valid':valid,'checks':checks,'warnings':[] if valid else ['Контрольные цифры или даты MRZ не совпадают. Обязательна ручная проверка по оригиналу.']}

def build_mrz(last,first,number,birth,expiry,gender='M',country='RUS'):
    country=str(country or '').upper()
    if not re.fullmatch(r'[A-Z]{3}',country): raise AppError('Код страны MRZ должен состоять из трёх латинских букв')
    gender=str(gender or '').upper()
    if gender not in {'M','F','X'}: raise AppError('Пол в MRZ должен быть M, F или X')
    gender='<' if gender=='X' else gender
    number=str(number or '').upper()
    if not re.fullmatch(r'[A-Z0-9]{1,9}',number): raise AppError('Номер документа MRZ должен содержать до 9 латинских букв и цифр')
    def compact_name(value):
        value=re.sub(r'[^A-Z0-9]+','<',transliterate(str(value or ''))).strip('<')
        if not value: raise AppError('Фамилия и имя обязательны для MRZ')
        return value
    def compact_date(value,label):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(value or '')): raise AppError(f'{label} должна быть в формате ГГГГ-ММ-ДД')
        try: parsed=date.fromisoformat(value)
        except ValueError: raise AppError(f'{label} недопустима')
        return parsed.strftime('%y%m%d')
    a=('P<'+country+compact_name(last)+'<<'+compact_name(first)).ljust(44,'<')[:44]
    number=number.ljust(9,'<'); bdate=compact_date(birth,'Дата рождения'); edate=compact_date(expiry,'Срок действия')
    b=number+check_digit(number)+country+bdate+check_digit(bdate)+gender+edate+check_digit(edate)+'<'*14+'0'
    b+=check_digit(b[:10]+b[13:20]+b[21:43]); return a+'\n'+b

@dataclass
class DocumentData:
    fields: dict=field(default_factory=dict)
    confidence: dict=field(default_factory=dict)
    warnings: list=field(default_factory=list)
    boxes: list=field(default_factory=list)
    provider: str='manual'
    mrz_valid: bool=False
    manual_required: bool=True

class OCRProvider(Protocol):
    def recognizeDocument(self,file:bytes,mime:str)->DocumentData: ...
class ManualOCR:
    def recognizeDocument(self,file,mime): return DocumentData(warnings=['Доступен ручной ввод. Сверьте все поля с оригиналом документа.'])

def preprocess(raw):
    import cv2, numpy as np
    from PIL import Image, ImageOps
    try:
        im=ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert('RGB')
        if im.width*im.height>40000000: raise AppError('Изображение слишком большое')
        if min(im.size)<350: raise AppError('Документ слишком мелкий. Снимите ближе целиком, без обрезанных краёв.')
        img=cv2.cvtColor(np.array(im),cv2.COLOR_RGB2BGR); gray=cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)
        if cv2.Laplacian(gray,cv2.CV_64F).var()<35: raise AppError('Фото размыто, переснимите при хорошем освещении')
        edges=cv2.Canny(cv2.GaussianBlur(gray,(5,5),0),50,160); contours,_=cv2.findContours(edges,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE); found=False
        for contour in sorted(contours,key=cv2.contourArea,reverse=True)[:8]:
            approx=cv2.approxPolyDP(contour,0.02*cv2.arcLength(contour,True),True)
            if len(approx)==4 and cv2.contourArea(approx)>0.3*gray.size:
                pts=approx.reshape(4,2).astype('float32'); sums=pts.sum(axis=1); dif=np.diff(pts,axis=1).ravel(); box=np.array([pts[sums.argmin()],pts[dif.argmin()],pts[sums.argmax()],pts[dif.argmax()]],dtype='float32')
                w=int(max(np.linalg.norm(box[1]-box[0]),np.linalg.norm(box[2]-box[3]))); h=int(max(np.linalg.norm(box[3]-box[0]),np.linalg.norm(box[2]-box[1])))
                if min(w,h)>100:
                    matrix=cv2.getPerspectiveTransform(box,np.array([[0,0],[w-1,0],[w-1,h-1],[0,h-1]],dtype='float32')); img=cv2.warpPerspective(img,matrix,(w,h)); found=True
                break
        gray=cv2.cvtColor(img,cv2.COLOR_BGR2GRAY); gray=cv2.createCLAHE(clipLimit=2.0,tileGridSize=(8,8)).apply(gray)
        return gray,([] if found else ['Границы документа не определены. Проверьте, что все края и строки попали в кадр.'])
    except AppError: raise
    except Exception: raise AppError('Не удалось прочитать изображение. Загрузите JPEG, PNG или PDF.')

def parse_text(text,quality=0.7):
    upper=text.upper(); fields={}; warnings=[]; lines=[re.sub(r'[^A-Z0-9<]','',x.upper().replace('«','<<')) for x in text.splitlines()]
    for i,line in enumerate(lines[:-1]):
        if line.startswith('P<') and len(line)==44 and len(lines[i+1])==44:
            result=parse_mrz(line+'\n'+lines[i+1]); result['confidence']={k:min(v,max(0.5,quality)) for k,v in result['confidence'].items()}
            return DocumentData(**{k:result[k] for k in ('fields','confidence','warnings','mrz_valid')},provider='local',manual_required=True)
    if 'СВИДЕТЕЛЬСТВО' in upper and 'РОЖДЕНИ' in upper:
        fields['type']='birth_certificate'; m=re.search(r'([IVXLCDMІХV]+[-– ]*[А-ЯЁ]{2})\s*(?:№\s*)?(\d{6})',upper)
        if m: fields.update(series=m[1],number=m[2])
    elif 'ПАСПОРТ' in upper or 'РОССИЙСКАЯ ФЕДЕРАЦИЯ' in upper: fields['type']='passport_rf'
    elif 'ЗАРЕГИСТРИРОВАН' in upper or 'МЕСТО ЖИТЕЛЬСТВА' in upper:
        fields.update(type='passport_rf',registration_address=re.sub(r'\s+',' ',text).strip()); warnings.append('Страница регистрации: привяжите к существующему паспорту, не создавайте новый документ.')
    else:
        fields['type']='international_passport' if 'PASSPORT' in upper else 'passport_rf'; warnings.append('Тип документа не определён надёжно. Для иностранного документа выберите тип и введите поля вручную.')
    for key,label in [('last_name_ru','ФАМИЛИЯ'),('first_name_ru','ИМЯ'),('patronymic_ru','ОТЧЕСТВО')]:
        m=re.search(label+r'\s*[:\n ]+([А-ЯЁ][А-ЯЁ \-]{1,45})',upper)
        if m: fields[key]=m[1].split('\n')[0].strip()
    number=re.search(r'\b(\d{2})\s*(\d{2})\s*(\d{6})\b',upper)
    if number and fields['type']=='passport_rf': fields.update(series=number[1]+number[2],number=number[3])
    division=re.search(r'\b\d{3}-\d{3}\b',upper)
    if division: fields['division_code']=division[0]
    for key,pattern in [('birth_date',r'(?:ДАТА РОЖДЕНИЯ|РОДИЛСЯ|РОДИЛАСЬ)'),('issue_date',r'(?:ДАТА ВЫДАЧИ|ВЫДАН)')]:
        m=re.search(pattern+r'[^\d]{0,25}(\d{2})[. /](\d{2})[. /](\d{4})',upper)
        if m:
            try: fields[key]=date(int(m[3]),int(m[2]),int(m[1])).isoformat()
            except ValueError: warnings.append('Проверьте дату по оригиналу')
    m=re.search(r'ПАСПОРТ ВЫДАН\s+(.{5,120})',upper)
    if m: fields['issued_by']=m[1].strip()
    if 'ЖЕН' in upper: fields['gender']='F'
    elif 'МУЖ' in upper: fields['gender']='M'
    return DocumentData(fields,{k:min(quality,0.85) for k in fields},warnings,provider='local')

class LocalOCR:
    def recognizeDocument(self,file,mime):
        if mime=='application/pdf':
            from pypdf import PdfReader
            reader=PdfReader(io.BytesIO(file))
            if len(reader.pages)>10: raise AppError('В одном PDF допускается не более 10 страниц')
            text='\n'.join(p.extract_text() or '' for p in reader.pages)
            if len(text.strip())>30: return parse_text(text,0.89)
            if not shutil.which('pdftoppm'): return ManualOCR().recognizeDocument(file,mime)
            with tempfile.TemporaryDirectory() as temp:
                path=Path(temp)/'scan.pdf';path.write_bytes(file); subprocess.run(['pdftoppm','-f','1','-singlefile','-r','180','-png',str(path),str(Path(temp)/'page')],check=True,timeout=30,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); file=(Path(temp)/'page.png').read_bytes()
        image,warnings=preprocess(file)
        if not shutil.which('tesseract'):
            result=ManualOCR().recognizeDocument(file,mime); result.warnings.insert(0,'Локальный движок OCR не установлен. Можно ввести данные вручную.');return result
        import pytesseract
        try:
            try:
                osd=pytesseract.image_to_osd(image,output_type=pytesseract.Output.DICT,timeout=10); rotate=int(osd.get('rotate',0))
                if rotate:
                    import numpy as np
                    image=np.rot90(image,k=-(rotate//90))
            except Exception: pass
            output=pytesseract.image_to_data(image,lang='rus+eng',config='--psm 6',output_type=pytesseract.Output.DICT,timeout=40); lines={}; boxes=[]; confidences=[]
            for i,text in enumerate(output['text']):
                if not text.strip(): continue
                key=(output['block_num'][i],output['par_num'][i],output['line_num'][i]); lines.setdefault(key,[]).append(text); conf=max(0,min(1,float(output['conf'][i])/100));confidences.append(conf); boxes.append({'x':output['left'][i]/image.shape[1],'y':output['top'][i]/image.shape[0],'w':output['width'][i]/image.shape[1],'h':output['height'][i]/image.shape[0],'text':text,'confidence':conf})
            text='\n'.join(' '.join(words) for words in lines.values()); mrz_text=pytesseract.image_to_string(image[int(image.shape[0]*0.60):],lang='eng',config='--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<',timeout=20); text+='\n'+mrz_text
            result=parse_text(text,sum(confidences)/len(confidences) if confidences else 0.3);result.boxes=boxes;result.warnings+=warnings; return result
        except Exception:
            result=ManualOCR().recognizeDocument(file,mime);result.warnings.insert(0,'Распознавание недоступно. Данные можно ввести вручную.');return result

class RussianApiOCR:
    def recognizeDocument(self,file,mime):
        import requests
        url=os.environ['OCR_API_URL']
        if not url.startswith('https://'): raise AppError('Внешний OCR должен использовать HTTPS')
        if os.getenv('OCR_DATA_REGION')!='RU': raise AppError('Внешний OCR должен обрабатывать данные в РФ')
        result=requests.post(url,headers={'Authorization':'Bearer '+os.environ['OCR_API_KEY']},files={'file':('document',file,mime)},timeout=40); result.raise_for_status();data=result.json(); parsed=DocumentData(fields=data.get('fields',{}),confidence={k:max(0,min(1,float(v))) for k,v in data.get('confidence',{}).items()},warnings=data.get('warnings',[]),provider='russian-api')
        if parsed.fields.get('mrz_raw'):
            verified=parse_mrz(parsed.fields['mrz_raw']);parsed.fields.update(verified['fields']);parsed.mrz_valid=verified['mrz_valid'];parsed.warnings+=verified['warnings']
        return parsed

def recognizeDocument(file,mime,mode=None):
    mode=mode or os.getenv('OCR_PROVIDER','local')
    if mode=='manual': return asdict(ManualOCR().recognizeDocument(file,mime))
    if mode=='russian-api':
        try: return asdict(RussianApiOCR().recognizeDocument(file,mime))
        except Exception:
            result=LocalOCR().recognizeDocument(file,mime); result.warnings.insert(0,'Внешний OCR недоступен; использован локальный режим.');return asdict(result)
    return asdict(LocalOCR().recognizeDocument(file,mime))
