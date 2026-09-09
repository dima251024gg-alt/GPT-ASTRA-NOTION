"""Сервисный слой документооборота: версии шаблонов, контекст, PDF/DOCX, пакеты и ссылки.

Ключевые свойства:
- данные берутся только из карточек, вручную ничего не дописывается;
- повторная генерация без изменения данных возвращает тот же документ (идемпотентность);
- любое изменение сделки или шаблона даёт новую версию, старая помечается superseded;
- всё пишется в аудит-лог в одной транзакции с самим действием.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from . import config, documents, render, storage
from . import templates as registry
from .db import now
from .security import AppError, blind, canonical, token
from .service import age, fio

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TARIFFS = {"adult": "взрослый", "child": "ребёнок", "infant": "младенец без места"}
OPERATOR_SOURCES = ("operator", "upload")

AGENCY_DEFAULTS = {
    "NAME": "ООО «Аквамарин»",
    "CITY": "Москва",
    "INN": "7701234567",
    "KPP": "770101001",
    "OGRN": "1157746000000",
    "ADDRESS": "101000, г. Москва, ул. Мясницкая, д. 10, офис 3",
    "PHONE": "+7 495 000-00-00",
    "EMAIL": "docs@aquamarine.example",
    "SIGNER_ROLE": "Генеральный директор",
    "SIGNER_FIO": "Ковалёва Ирина Сергеевна",
    "BANK": "АО «Демо-Банк»",
    "ACCOUNT": "40702810100000000123",
    "BIC": "044525000",
}


def agency(db):
    """Реквизиты агентства: демо-значения, перекрываемые настройками agency.*."""
    data = dict(AGENCY_DEFAULTS)
    for row in db.all("settings"):
        name = str(row.get("name") or "")
        if name.startswith("agency.") and row.get("value") not in (None, ""):
            data[name.split(".", 1)[1].upper()] = row["value"]
    data["REQUISITES"] = (
        f"{data['NAME']}, ИНН {data['INN']}, КПП {data['KPP']}, ОГРН {data['OGRN']}, адрес: {data['ADDRESS']}, "
        f"тел. {data['PHONE']}, e-mail {data['EMAIL']}, р/с {data['ACCOUNT']} в {data['BANK']}, БИК {data['BIC']}"
    )
    data["SIGNER"] = f"{data['SIGNER_ROLE']} {data['SIGNER_FIO']}"
    return data


def contact(value, *keys):
    """Достаёт контакт из json-поля любой формы (строка, словарь, список)."""
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in keys:
            if value.get(key):
                return str(value[key])
        return "; ".join(f"{k}: {v}" for k, v in value.items() if v)
    if isinstance(value, (list, tuple)):
        return "; ".join(contact(item, *keys) for item in value if item)
    return str(value)


def doc_line(doc):
    """Строка документа для договора: тип, серия и номер, кем и когда выдан."""
    if not doc:
        return ""
    number = " ".join(str(x).strip() for x in (doc.get("series"), doc.get("number")) if x)
    parts = [" ".join(x for x in (str(doc.get("type") or "документ"), number) if x).strip()]
    if doc.get("issue_date"):
        parts.append("выдан " + render.date_ru(doc["issue_date"]) + ((" " + str(doc["issued_by"])) if doc.get("issued_by") else ""))
    if doc.get("division_code"):
        parts.append("код подразделения " + str(doc["division_code"]))
    if doc.get("expiry_date"):
        parts.append("действителен до " + render.date_ru(doc["expiry_date"]))
    return ", ".join(parts)


def latin(doc):
    if not doc:
        return ""
    return " ".join(str(x).strip() for x in (doc.get("last_name_latin"), doc.get("first_name_latin")) if x).upper()


def identity_list(db, person_id):
    """Документы туриста: сначала основной, затем с более далёким сроком действия."""
    rows = db.all("identity_documents", "person_id=?", (person_id,))
    return sorted(rows, key=lambda d: (0 if d.get("is_primary") else 1, str(d.get("expiry_date") or "")), reverse=False)


def passport(db, person_id, foreign=False):
    """Загранпаспорт определяется наличием MRZ/латиницы, а не названием типа."""
    rows = identity_list(db, person_id)
    if foreign:
        rows = [d for d in rows if d.get("mrz_raw") or d.get("last_name_latin")] or rows
    return rows[0] if rows else None


def person_context(db, person, foreign=False):
    if not person:
        return {}
    doc = passport(db, person["id"], foreign)
    address = db.find("addresses", "person_id=?", (person["id"],))
    return {
        "ID": person["id"],
        "FIO": fio(person),
        "LAST_NAME": person.get("last_name_ru"),
        "FIRST_NAME": person.get("first_name_ru"),
        "PATRONYMIC": person.get("patronymic_ru"),
        "BIRTH_DATE": person.get("birth_date"),
        "GENDER": person.get("gender"),
        "BIRTH_PLACE": person.get("birth_place"),
        "CITIZENSHIP": person.get("citizenship") or "RUS",
        "PHONE": person.get("phone"),
        "EMAIL": person.get("email"),
        "RELATION": person.get("relationship"),
        "PASSPORT": doc_line(doc),
        "PASSPORT_NUMBER": " ".join(str(x) for x in (doc.get("series"), doc.get("number")) if x) if doc else "",
        "PASSPORT_EXPIRY": (doc or {}).get("expiry_date") or "",
        "LATIN": latin(doc),
        "ADDRESS": (address or {}).get("full_address") or "",
        "NEEDS_VISA": person.get("needs_visa") or 0,
    }


def tourist_rows(db, deal_id):
    """Пары (связка со сделкой, турист) в порядке добавления."""
    out = []
    for link in db.all("deal_persons", "deal_id=?", (deal_id,)):
        try:
            out.append((link, db.one("persons", link["person_id"])))
        except AppError:
            continue
    return out


def tourists_context(db, deal_id, foreign=False):
    """Блок TOURISTS: список для циклов, готовая таблица и количество. Пусто — если туристов нет."""
    rows = tourist_rows(db, deal_id)
    if not rows:
        return {}
    items, lines = [], []
    for index, (link, person) in enumerate(rows, start=1):
        item = person_context(db, person, foreign)
        item.update(
            ROLE=link.get("role") or "турист",
            TARIFF=TARIFFS.get(link.get("tariff") or "adult", link.get("tariff") or "взрослый"),
            PLACEMENT=link.get("placement") or "",
            PARENTS_COUNT=link.get("parents_count") or 0,
        )
        items.append(item)
        lines.append(
            "{}. {} ({}) — {}, {}{}".format(
                index,
                item["FIO"],
                render.date_ru(item["BIRTH_DATE"]) or "дата рождения не указана",
                item["TARIFF"],
                item["PASSPORT"] or "документ не загружен",
                (", " + item["LATIN"]) if item["LATIN"] else "",
            )
        )
    return {"LIST": items, "TABLE": "\n".join(lines), "COUNT": len(items)}


MSK = timezone(timedelta(hours=3))
PER_PERSON = ("consent_pd", "consent_transfer", "consent_cross_border")
KINSHIP = ("мать", "отец", "родитель", "опекун", "попечитель", "усыновитель")
HANDOVER_METHOD = "электронно, по защищённой ссылке в кабинете туриста"
HANDOVER_ITEMS = (
    "договор о реализации туристского продукта, памятка туриста, "
    "электронные авиабилеты, ваучер на размещение, страховой полис"
)


def today():
    return datetime.now(MSK).date().isoformat()


def representative_evidence(db, person, link):
    """Чем подтверждаются полномочия: свидетельство о рождении ребёнка или загруженный документ."""
    for doc in (identity_list(db, person["id"]) if person else []):
        if "свидетельств" in str(doc.get("type") or "").lower():
            return doc_line(doc)
    if link and link.get("guardian_evidence_file_id"):
        try:
            name = db.one("files", link["guardian_evidence_file_id"]).get("name")
        except AppError:
            name = None
        if name:
            return "документ о полномочиях: " + str(name)
    return ""


def recipients_context(db, deal, operator, foreign):
    """Кому передаются персональные данные: туроператор, авиакомпания, отель, страховщик."""
    raw = deal.get("recipients") or []
    if isinstance(raw, dict):
        raw = [raw]
    inland, abroad = [], []
    if operator:
        inland.append(
            "{}, ИНН {}, {}".format(operator.get("full_name") or "", operator.get("inn") or "", operator.get("address") or "").strip(" ,")
        )
    for item in raw:
        if isinstance(item, str):
            text, code = item, ""
        else:
            code = str(item.get("country") or "").upper()
            text = ", ".join(str(value) for key, value in item.items() if key != "country" and value)
            if code:
                text = f"{text} ({code})"
        if not text:
            continue
        inland.append(text)
        if code and code != "RU":
            abroad.append(text)
    if foreign and deal.get("host_contacts"):
        abroad.append("принимающая сторона: " + str(deal["host_contacts"]))
    if foreign and deal.get("hotel"):
        abroad.append("отель: " + str(deal["hotel"]))
    return "; ".join(dict.fromkeys(x for x in inland if x)), "; ".join(dict.fromkeys(x for x in abroad if x))


def build_context(db, deal_id, extra=None, person_id=None):
    """Единый контекст сделки: данные вводятся один раз и подставляются во все шаблоны."""
    deal = db.one("deals", deal_id)
    client = db.one("clients", deal["client_id"])
    manager = db.find("users", "id=?", (deal.get("manager_id"),)) if deal.get("manager_id") else None
    operator = db.find("operators", "id=?", (deal.get("operator_id"),)) if deal.get("operator_id") else None
    country = db.find("countries", "id=?", (deal.get("country_id"),)) if deal.get("country_id") else None
    foreign = bool(deal.get("cross_border")) or str((country or {}).get("code") or "").upper() not in ("", "RU")
    rows = tourist_rows(db, deal_id)
    holder = next((person for item, person in rows if str(item.get("role") or "") == "заказчик"), None)
    if holder is None and rows:
        holder = rows[0][1]
    subject = db.one("persons", person_id) if person_id else holder
    holder_context = person_context(db, holder, foreign)
    representative, link = None, None
    if person_id:
        link = next((item for item, person in rows if person["id"] == person_id), None)
        if link and link.get("legal_representative_id"):
            representative = db.one("persons", link["legal_representative_id"])
    representative_context = person_context(db, representative, foreign)
    if representative_context:
        representative_context["EVIDENCE"] = representative_evidence(db, subject, link)
        if str(representative_context.get("RELATION") or "").strip().lower() not in KINSHIP:
            representative_context["RELATION"] = "законный представитель"
    subject_context = person_context(db, subject, foreign)
    if subject_context:
        # у ребёнка своих контактов нет — берём контакты представителя или заказчика
        for key in ("PHONE", "EMAIL", "ADDRESS"):
            if not subject_context.get(key):
                subject_context[key] = representative_context.get(key) or holder_context.get(key) or ""
    inland, abroad = recipients_context(db, deal, operator, foreign)
    contacts = (operator or {}).get("contacts")
    context = {
        "DEAL": {
            "ID": deal["id"],
            "NUMBER": deal.get("number"),
            "STATUS": deal.get("status"),
            "DATE": str(deal.get("created_at") or "")[:10] or today(),
            "BOOKING_NUMBER": deal.get("booking_number") or "",
            "CLIENT_PRICE": deal.get("client_price") or 0,
            "OPERATOR_NET": deal.get("operator_net") or 0,
            "COMMISSION": deal.get("commission") or 0,
            "PAID_CLIENT": deal.get("paid_client") or 0,
            "DEBT": (deal.get("client_price") or 0) - (deal.get("paid_client") or 0),
            "CLIENT_DEADLINE": deal.get("client_deadline") or "",
            "OPERATOR_DEADLINE": deal.get("operator_deadline") or "",
            "HANDOVER_DATE": str(deal.get("completed_at") or deal.get("date_to") or "")[:10],
            "COMMENTS": deal.get("comments") or "",
            "CROSS_BORDER": 1 if foreign else 0,
            "REVISION": deal.get("revision") or 1,
        },
        "CLIENT": {
            "ID": client["id"],
            "TYPE": client.get("type") or "individual",
            "FIO": client.get("full_name"),
            "BIRTH_DATE": client.get("birth_date") or holder_context.get("BIRTH_DATE") or "",
            "PHONE": client.get("phone") or holder_context.get("PHONE") or "",
            "EMAIL": client.get("email") or holder_context.get("EMAIL") or "",
            "ADDRESS": client.get("registration_address") or holder_context.get("ADDRESS") or "",
            "PASSPORT": holder_context.get("PASSPORT") or "",
            "PASSPORT_NUMBER": holder_context.get("PASSPORT_NUMBER") or "",
            "INN": client.get("inn") or "",
            "KPP": client.get("kpp") or "",
            "OGRN": client.get("ogrn") or "",
            "MESSENGER": " ".join(str(x) for x in (client.get("messenger"), client.get("messenger_username")) if x),
        },
        "PERSON": subject_context,
        "REPRESENTATIVE": representative_context,
        "TOUR": {
            "COUNTRY": (country or {}).get("name") or "",
            "CITY": deal.get("city") or "",
            "DATE_FROM": deal.get("date_from") or "",
            "DATE_TO": deal.get("date_to") or "",
            "NIGHTS": deal.get("nights") or 0,
            "HOTEL": deal.get("hotel") or "",
            "ROOM": deal.get("room") or "",
            "MEAL": deal.get("meal") or "",
            "TYPE": deal.get("tour_type") or "пакетный тур",
            "PRICE": deal.get("client_price") or 0,
            "CURRENCY": deal.get("currency") or "RUB",
            "SERVICES": deal.get("services") or "",
            "PROGRAM": deal.get("program") or "",
            "ROUTE": deal.get("route") or "",
            "GUIDE": deal.get("guide") or "",
            "FLIGHT_THERE": contact(deal.get("flight_there"), "summary", "number"),
            "FLIGHT_BACK": contact(deal.get("flight_back"), "summary", "number"),
            "HOST_CONTACTS": deal.get("host_contacts") or "",
            "ADULTS": deal.get("adults") or 0,
            "CHILDREN": deal.get("children") or 0,
            "INFANTS": deal.get("infants") or 0,
        },
        "OPERATOR": {
            "FULL_NAME": (operator or {}).get("full_name") or "",
            "SHORT_NAME": (operator or {}).get("short_name") or "",
            "INN": (operator or {}).get("inn") or "",
            "OGRN": (operator or {}).get("ogrn") or "",
            "ADDRESS": (operator or {}).get("address") or "",
            "REGISTRY_NUMBER": (operator or {}).get("registry_number") or "",
            "GUARANTEE_TYPE": (operator or {}).get("guarantee_type") or "",
            "GUARANTEE_AMOUNT": (operator or {}).get("guarantee_amount") or 0,
            "GUARANTOR": (operator or {}).get("guarantor") or "",
            "GUARANTEE_NUMBER": (operator or {}).get("guarantee_number") or "",
            "GUARANTEE_FROM": (operator or {}).get("guarantee_from") or "",
            "GUARANTEE_TO": (operator or {}).get("guarantee_to") or "",
            "GUARANTEE_ADDRESS": (operator or {}).get("guarantee_address") or (operator or {}).get("address") or "",
            "EMERGENCY_PHONE": contact(contacts, "emergency", "phone"),
            "PHONE": contact(contacts, "phone", "emergency"),
            "EMAIL": contact(contacts, "email"),
            "PORTAL": (operator or {}).get("portal_url") or "",
            "PAYMENT_HOURS": (operator or {}).get("payment_hours") or 24,
        },
        "COUNTRY": {
            "CODE": (country or {}).get("code") or "",
            "NAME": (country or {}).get("name") or "",
            "PASSPORT_MONTHS": (country or {}).get("passport_months") or 0,
            "PASSPORT_DAYS": (country or {}).get("passport_days") or 0,
            "PASSPORT_RULE": (country or {}).get("passport_rule_basis") or "",
            "VISA_REQUIRED": (country or {}).get("visa_required") or 0,
            "MEMO": (country or {}).get("memo") or "",
        },
        "MANAGER": {
            "FIO": (manager or {}).get("full_name") or "",
            "PHONE": (manager or {}).get("phone") or "",
            "EMAIL": (manager or {}).get("email") or "",
        },
        "HANDOVER": {
            "METHOD": HANDOVER_METHOD,
            "DOCUMENTS": HANDOVER_ITEMS,
            "DATE": today(),
        },
        "AGENCY": agency(db),
        "TOURISTS": tourists_context(db, deal_id, foreign),
        "RECIPIENTS": inland,
        "FOREIGN_RECIPIENTS": abroad,
        "RETENTION_YEARS": config.RETENTION_YEARS,
        "TODAY": today(),
        "PORTAL_URL": config.PUBLIC_URL,
    }
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(context.get(key), dict):
            merged = dict(context[key])
            merged.update(value)
            context[key] = merged
        else:
            context[key] = value
    return context


def sync_templates(db, actor=None):
    """Заливает реестр шаблонов в базу и ведёт историю версий: правка тела — новая версия."""
    created, updated = [], []
    for item in registry.TEMPLATES:
        code = item["type"]
        fields = list(item.get("required_fields") or registry.field_names(item["body"]))
        payload = {
            "name": item["name"],
            "type": code,
            "format": item.get("format") or "pdf+docx",
            "body": item["body"],
            "required_fields": fields,
            "active": 1,
            "changed_by": actor,
        }
        row = db.find("templates", "type=?", (code,))
        if row is None:
            payload["version"] = 1
            row = db.insert("templates", payload, actor)
            db.insert(
                "template_versions",
                {"template_id": row["id"], "version": 1, "body": item["body"], "required_fields": fields, "changed_by": actor},
                actor,
            )
            created.append(code)
            continue
        unchanged = str(row.get("body") or "") == item["body"] and list(row.get("required_fields") or []) == fields
        if unchanged and row.get("active"):
            continue
        payload["version"] = int(row.get("version") or 1) + 1
        db.update("templates", row["id"], payload)
        db.insert(
            "template_versions",
            {
                "template_id": row["id"],
                "version": payload["version"],
                "body": item["body"],
                "required_fields": fields,
                "changed_by": actor,
            },
            actor,
        )
        updated.append(code)
    return {"created": created, "updated": updated}


def template_row(db, code):
    """Активный шаблон по коду; если база пуста — подтягивает реестр."""
    if code not in registry.BY_TYPE:
        raise AppError("Неизвестный шаблон документа: " + str(code), 404)
    row = db.find("templates", "type=? AND active=1", (code,))
    if row is None:
        sync_templates(db)
        row = db.find("templates", "type=? AND active=1", (code,))
    if row is None:
        raise AppError("Шаблон «" + str(code) + "» отключён в настройках", 409)
    return row


def context_hash(context):
    return hashlib.sha256(canonical(context).encode("utf-8")).hexdigest()


def document_key(deal_id, code, person_id, template_version, revision, digest):
    """Ключ идемпотентности: те же данные и тот же шаблон — тот же документ."""
    parts = (str(deal_id), str(code), str(person_id or ""), str(template_version), str(revision), str(digest))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def bind_contract(service, deal, document, files, template):
    """Договор — отдельная сущность: номер, дата и хеш PDF, к которому привяжется ПЭП."""
    db = service.db
    data = {
        "deal_id": deal["id"],
        "number": deal.get("number"),
        "date": today(),
        "type": template["type"],
        "template_id": template["id"],
        "status": "черновик",
        "pdf_file_id": document["file_id"],
        "file_hash": files["sha256"],
        "document_id": document["id"],
    }
    row = db.find("contracts", "deal_id=? AND status=?", (deal["id"], "черновик"))
    if row is None:
        row = db.insert("contracts", data, service.actor)
        service.audit("create", "contracts", row["id"], {"deal": deal.get("number"), "hash": files["sha256"][:16]})
        return row
    db.update("contracts", row["id"], data)
    service.audit("update", "contracts", row["id"], {"deal": deal.get("number"), "hash": files["sha256"][:16]})
    return db.one("contracts", row["id"])


def generate(service, deal_id, code, extra=None, person_id=None, force=False):
    """Формирует документ (PDF + DOCX) с проверкой полноты данных — валидация V10."""
    db = service.db
    deal = db.one("deals", deal_id)
    if not service.can("deals", deal):
        raise AppError("Сделка доступна только своему менеджеру и руководству", 403)
    template = template_row(db, code)
    context = build_context(db, deal_id, extra, person_id)
    missing = render.missing_fields(template, context)
    if missing:
        raise AppError(
            "Документ «" + str(template["name"]) + "» нельзя сформировать: не заполнены обязательные поля",
            422,
            {"missing": missing, "template": code, "deal_id": deal_id, "person_id": person_id},
        )
    text = render.render_template(template, context)
    revision = int(deal.get("revision") or 1)
    template_version = int(template.get("version") or 1)
    key = document_key(deal_id, code, person_id, template_version, revision, context_hash(context))
    siblings = [
        row
        for row in db.all("documents", "deal_id=? AND type=?", (deal_id, code))
        if (row.get("person_id") or None) == (person_id or None)
    ]
    existing = db.find("documents", "idempotency_key=?", (key,))
    if existing is not None and not force:
        if not existing.get("superseded"):
            return existing
        live = [
            row
            for row in siblings
            if not row.get("superseded")
            and int(row.get("data_revision") or 0) == revision
            and int(row.get("template_version") or 0) == template_version
        ]
        if live:
            return max(live, key=lambda row: int(row.get("version") or 0))
        return existing
    version = max([int(row.get("version") or 0) for row in siblings] or [0]) + 1
    if existing is not None:
        key = key + ":" + str(version)
    meta = {
        "title": template["name"],
        "agency": context["AGENCY"]["NAME"],
        "deal_number": deal.get("number"),
        "document_code": code,
        "generated_at": now(),
        "verify_url": config.PUBLIC_URL + "/verify/" + key[:16],
        "qr_caption": "Проверка подлинности документа",
    }
    files = documents.build_files(text, meta)
    name = str(deal.get("number") or deal_id) + "-" + code + "-v" + str(version)
    pdf = storage.store_file(db, files["pdf"], name + ".pdf", PDF_MIME, "documents", deal_id, service.actor, True)
    docx = storage.store_file(db, files["docx"], name + ".docx", DOCX_MIME, "documents", deal_id, service.actor, True)
    with db.atomic():
        for row in siblings:
            if not row.get("superseded"):
                db.update("documents", row["id"], {"superseded": 1})
        created = db.insert(
            "documents",
            {
                "deal_id": deal_id,
                "person_id": person_id,
                "type": code,
                "source": "generated",
                "file_id": pdf["id"],
                "docx_file_id": docx["id"],
                "version": version,
                "issued": 0,
                "template_id": template["id"],
                "template_version": template_version,
                "idempotency_key": key,
                "data_revision": revision,
                "superseded": 0,
            },
            service.actor,
        )
        service.audit(
            "generate",
            "documents",
            created["id"],
            {"template": code, "version": version, "sha256": files["sha256"][:16], "pages": files.get("pages")},
        )
        if code == "contract_tour":
            bind_contract(service, deal, created, files, template)
    return db.one("documents", created["id"])


MANUAL_ONLY = ("power_of_attorney", "amendment", "cancellation")


def package_flags(db, deal_id):
    """Флаги для условных шаблонов: зарубежный тур, есть несовершеннолетние, есть представитель."""
    deal = db.one("deals", deal_id)
    rows = tourist_rows(db, deal_id)
    country = db.find("countries", "id=?", (deal.get("country_id"),)) if deal.get("country_id") else None
    abroad = bool(deal.get("cross_border")) or str((country or {}).get("code") or "").upper() not in ("", "RU")
    on_date = deal.get("date_from") or today()
    minors = []
    for link, person in rows:
        if not person.get("birth_date"):
            continue
        try:
            years = age(person["birth_date"], on_date)
        except (AppError, ValueError, TypeError):
            continue
        if years < 18:
            minors.append((link, person))
    return {
        "tour_abroad": abroad,
        "has_minor": bool(minors),
        "has_representative": any(link.get("legal_representative_id") for link, _ in rows),
        "minors": minors,
        "persons": rows,
    }


def package_plan(name, flags):
    """Состав пакета: пары (код шаблона, турист) плюс файлы, которые присылает туроператор.

    Согласия оформляются на каждого туриста, согласие за несовершеннолетнего — только на детей.
    Доверенность, допсоглашение и аннуляция в автопакеты не попадают — их выпускает менеджер вручную.
    """
    package = registry.PACKAGES.get(name)
    if package is None:
        raise AppError("Неизвестный пакет документов: " + str(name), 404)
    codes = [code for code in package["templates"] if code not in MANUAL_ONLY]
    for code, flag in registry.CONDITIONAL.items():
        if code in MANUAL_ONLY or code in codes:
            continue
        if flags.get(flag) and code in [item["type"] for item in registry.TEMPLATES]:
            codes.append(code)
    plan = []
    for code in codes:
        if code == "consent_minor":
            plan.extend((code, person["id"]) for _link, person in flags["minors"])
        elif code in PER_PERSON:
            plan.extend((code, person["id"]) for _link, person in flags["persons"])
        else:
            plan.append((code, None))
    return plan, [code for code in (package.get("files") or [])]


def current_documents(db, deal_id):
    """Актуальные документы сделки: по одному на пару (тип, турист) — последняя версия."""
    latest = {}
    for row in db.all("documents", "deal_id=?", (deal_id,)):
        if row.get("superseded"):
            continue
        key = (row.get("type"), row.get("person_id") or None)
        current = latest.get(key)
        if current is None or int(row.get("version") or 0) > int(current.get("version") or 0):
            latest[key] = row
    return latest


def task_for_missing(service, deal_id, title, kind, codes, due_hours=24):
    """Ставит задачу менеджеру один раз на один и тот же набор пробелов (идемпотентность)."""
    db = service.db
    deal = db.one("deals", deal_id)
    key = blind("|".join([kind, str(deal_id), canonical(sorted(codes))]))
    existing = db.find("tasks", "idempotency_key=?", (key,))
    if existing is not None:
        return existing
    row = db.insert(
        "tasks",
        {
            "deal_id": deal_id,
            "type": kind,
            "title": title,
            "due_at": (datetime.now(timezone.utc) + timedelta(hours=due_hours)).isoformat(timespec="seconds"),
            "assignee_id": deal.get("manager_id"),
            "status": "open",
            "automatic": 1,
            "idempotency_key": key,
        },
        service.actor,
    )
    service.audit("create", "tasks", row["id"], {"type": kind, "templates": sorted(codes)})
    return row


def build_package(service, deal_id, name, force=False):
    """Собирает пакет: генерирует документы, добавляет файлы туроператора и клеит единый PDF."""
    db = service.db
    deal = db.one("deals", deal_id)
    if not service.can("deals", deal):
        raise AppError("Сделка доступна только своему менеджеру и руководству", 403)
    flags = package_flags(db, deal_id)
    plan, expected = package_plan(name, flags)
    title = registry.PACKAGES[name]["title"]
    ids, blobs, missing = [], [], []
    for code, person_id in plan:
        try:
            document = generate(service, deal_id, code, None, person_id)
        except AppError as error:
            details = error.details or {}
            missing.append(
                {
                    "template": code,
                    "person_id": person_id,
                    "fields": list(details.get("missing") or []),
                    "message": error.message,
                }
            )
            continue
        blob, _meta = storage.read_file(db, document["file_id"])
        ids.append(document["id"])
        blobs.append(blob)
    for code in expected:
        row = next(
            (
                item
                for item in db.all("documents", "deal_id=? AND type=?", (deal_id, code))
                if item.get("source") in OPERATOR_SOURCES and not item.get("superseded") and item.get("file_id")
            ),
            None,
        )
        if row is None:
            missing.append({"template": code, "person_id": None, "fields": [], "message": "Файл от туроператора ещё не загружен"})
            continue
        blob, _meta = storage.read_file(db, row["file_id"])
        if not blob.startswith(b"%PDF"):
            missing.append({"template": code, "person_id": None, "fields": [], "message": "Файл не в формате PDF — в пакет не включён"})
            continue
        ids.append(row["id"])
        blobs.append(blob)
    if not blobs:
        raise AppError("Пакет пуст: ни один документ не сформирован", 409, {"missing": missing})
    merged = documents.merge_pdfs(blobs)
    key = blind("|".join([name, str(deal_id), str(deal.get("revision") or 1), documents.digest(merged)]))
    existing = db.find("packages", "idempotency_key=?", (key,))
    if existing is not None and not force:
        return existing
    if existing is not None:
        key = key + ":" + token()[:8]
    file_row = storage.store_file(
        db, merged, str(deal.get("number") or deal_id) + "-" + name + ".pdf", PDF_MIME, "packages", deal_id, service.actor, True
    )
    with db.atomic():
        row = db.insert(
            "packages",
            {
                "deal_id": deal_id,
                "type": name,
                "file_id": file_row["id"],
                "document_ids": ids,
                "status": "неполный" if missing else "готов",
                "idempotency_key": key,
                "missing": missing,
            },
            service.actor,
        )
        service.audit(
            "package",
            "packages",
            row["id"],
            {"type": name, "documents": len(ids), "pages": documents.page_count(merged), "missing": len(missing)},
        )
        if missing:
            codes = sorted({item["template"] for item in missing})
            task_for_missing(
                service,
                deal_id,
                "Пакет «" + title + "» неполный: " + ", ".join(codes),
                "package_incomplete",
                codes,
            )
    return db.one("packages", row["id"])


def issue_link(service, package_id, days=30, person_id=None):
    """Выдаёт защищённую ссылку на пакет: в базе хранится только хеш токена."""
    db = service.db
    package = db.one("packages", package_id)
    deal = db.one("deals", package["deal_id"])
    if not service.can("deals", deal):
        raise AppError("Сделка доступна только своему менеджеру и руководству", 403)
    raw = token()
    with db.atomic():
        for old in db.all("portal_links", "deal_id=?", (deal["id"],)):
            if old.get("package_id") and not old.get("revoked_at"):
                db.update("portal_links", old["id"], {"revoked_at": now()})
        row = db.insert(
            "portal_links",
            {
                "deal_id": deal["id"],
                "client_id": deal.get("client_id"),
                "person_id": person_id,
                "token_hash": blind(raw),
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds"),
                "package_id": package_id,
            },
            service.actor,
        )
        db.update("packages", package_id, {"issued_at": now()})
        service.audit("issue_link", "portal_links", row["id"], {"package": package_id, "days": days})
    return db.one("portal_links", row["id"]), config.PUBLIC_URL + "/portal/" + raw


def link_by_token(db, raw):
    """Находит ссылку по токену и объясняет клиенту по-русски, почему она не работает."""
    row = db.find("portal_links", "token_hash=?", (blind(str(raw or "")),))
    if row is None:
        raise AppError("Ссылка недействительна, запросите новую у менеджера", 404)
    if row.get("revoked_at"):
        raise AppError("Ссылка отозвана: документы изменились, запросите новую у менеджера", 410)
    if row.get("expires_at") and str(row["expires_at"]) < now():
        raise AppError("Срок действия ссылки истёк, запросите новую у менеджера", 410)
    return row


def mark_opened(db, link, actor=None):
    """Отмечает факт открытия пакета клиентом — по нему работает напоминание через 24 часа."""
    if not link.get("consumed_at"):
        db.update("portal_links", link["id"], {"consumed_at": now()})
    if link.get("package_id"):
        package = db.one("packages", link["package_id"])
        if not package.get("opened_at"):
            db.update("packages", package["id"], {"opened_at": now()})
    db.audit(actor, "portal_open", "portal_links", link["id"], {"package": link.get("package_id")})
    return db.one("portal_links", link["id"])


def document_file(service, document_id, kind="pdf"):
    """Отдаёт файл документа (pdf или docx) с проверкой прав и целостности."""
    db = service.db
    row = db.one("documents", document_id)
    deal = db.one("deals", row["deal_id"])
    if not service.can("deals", deal):
        raise AppError("Документ доступен только своему менеджеру и руководству", 403)
    file_id = row.get("docx_file_id") if kind == "docx" else row.get("file_id")
    if not file_id:
        raise AppError("Файл документа не найден", 404)
    blob, meta = storage.read_file(db, file_id)
    service.audit("download", "documents", document_id, {"kind": kind, "file": file_id})
    return blob, meta


def package_file(service, package_id):
    """Единый PDF пакета для скачивания сотрудником."""
    db = service.db
    package = db.one("packages", package_id)
    deal = db.one("deals", package["deal_id"])
    if not service.can("deals", deal):
        raise AppError("Пакет доступен только своему менеджеру и руководству", 403)
    blob, meta = storage.read_file(db, package["file_id"])
    service.audit("download", "packages", package_id, {"file": package["file_id"]})
    return blob, meta


def deal_documents(service, deal_id):
    """Список актуальных документов сделки для карточки и API."""
    db = service.db
    deal = db.one("deals", deal_id)
    if not service.can("deals", deal):
        raise AppError("Сделка доступна только своему менеджеру и руководству", 403)
    out = []
    for (code, person_id), row in current_documents(db, deal_id).items():
        meta = db.find("files", "id=?", (row.get("file_id"),)) if row.get("file_id") else None
        person = db.find("persons", "id=?", (person_id,)) if person_id else None
        out.append(
            {
                "id": row["id"],
                "type": code,
                "title": (registry.BY_TYPE.get(code) or {}).get("name") or code,
                "person": fio(person) if person else "",
                "version": row.get("version") or 1,
                "source": row.get("source") or "generated",
                "issued": bool(row.get("issued")),
                "created_at": row.get("created_at"),
                "size": (meta or {}).get("size") or 0,
                "sha256": (meta or {}).get("sha256") or "",
                "has_docx": bool(row.get("docx_file_id")),
            }
        )
    return sorted(out, key=lambda item: (str(item["title"]), str(item["person"])))


def packages_for_deal(db, deal_id):
    """История пакетов сделки: свежие сверху."""
    rows = db.all("packages", "deal_id=?", (deal_id,))
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)
