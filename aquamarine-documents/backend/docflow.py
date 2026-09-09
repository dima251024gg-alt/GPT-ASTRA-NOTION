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


def today():
    return datetime.now(MSK).date().isoformat()


def build_context(db, deal_id, extra=None, person_id=None):
    """Собирает контекст шаблона из карточек сделки, заказчика, туристов и справочников."""
    deal = db.one("deals", deal_id)
    client = db.one("clients", deal["client_id"])
    country = db.one("countries", deal["country_id"]) if deal.get("country_id") else {}
    operator = db.one("operators", deal["operator_id"]) if deal.get("operator_id") else {}
    manager = db.one("users", deal["manager_id"]) if deal.get("manager_id") else {}
    foreign = bool(deal.get("cross_border"))
    rows = tourist_rows(db, deal_id)
    holder = next((p for link, p in rows if (p.get("relationship") or "") == "заказчик"), None)
    if holder is None and rows:
        holder = rows[0][1]
    subject = db.one("persons", person_id) if person_id else holder
    representative = None
    if person_id:
        link = next((l for l, p in rows if p["id"] == person_id), None)
        if link and link.get("legal_representative_id"):
            representative = db.one("persons", link["legal_representative_id"])
    holder_context = person_context(db, holder, foreign)
    recipients = [contact(item, "name", "title") for item in (deal.get("recipients") or [])]
    recipients = [item for item in recipients if item]
    if not recipients:
        recipients = [x for x in (operator.get("full_name"), deal.get("hotel"), "перевозчик (авиакомпания)", "страховая компания") if x]
    foreign_recipients = ""
    if foreign:
        foreign_recipients = ", ".join(
            x for x in (deal.get("hotel"), contact(deal.get("host_contacts"), "name"), country.get("name")) if x
        )
    context = {
        "TODAY": today(),
        "RETENTION_YEARS": config.RETENTION_YEARS,
        "RECIPIENTS": ", ".join(recipients),
        "FOREIGN_RECIPIENTS": foreign_recipients,
        "AGENCY": agency(db),
        "DEAL": {
            "ID": deal["id"],
            "NUMBER": deal.get("number"),
            "DATE": (deal.get("created_at") or "")[:10],
            "STATUS": deal.get("status"),
            "BOOKING_NUMBER": deal.get("booking_number"),
            "CLIENT_DEADLINE": deal.get("client_deadline"),
            "OPERATOR_DEADLINE": deal.get("operator_deadline"),
            "PAID_CLIENT": deal.get("paid_client") or 0,
            "PAID_OPERATOR": deal.get("paid_operator") or 0,
            "DEBT": (deal.get("client_price") or 0) - (deal.get("paid_client") or 0),
            "COMMISSION": deal.get("commission") or 0,
            "NET": deal.get("operator_net") or 0,
            "REVISION": deal.get("revision") or 1,
            "COMMENTS": deal.get("comments") or "",
            "CROSS_BORDER": 1 if foreign else 0,
            "HANDOVER_DATE": "",
            "ACT_NOTES": "",
        },
        "CLIENT": {
            "ID": client["id"],
            "FIO": client.get("full_name"),
            "TYPE": client.get("type"),
            "BIRTH_DATE": client.get("birth_date") or holder_context.get("BIRTH_DATE"),
            "PHONE": client.get("phone"),
            "EMAIL": client.get("email"),
            "ADDRESS": client.get("registration_address") or holder_context.get("ADDRESS"),
            "PASSPORT": holder_context.get("PASSPORT"),
            "INN": client.get("inn"),
            "KPP": client.get("kpp"),
            "OGRN": client.get("ogrn"),
            "BANK": contact(client.get("bank_details"), "name", "bank"),
            "MESSENGER": " ".join(x for x in (client.get("messenger"), client.get("messenger_username")) if x),
        },
        "PERSON": person_context(db, subject, foreign),
        "REPRESENTATIVE": person_context(db, representative, foreign),
        "TOUR": {
            "COUNTRY": country.get("name"),
            "CITY": deal.get("city"),
            "DATE_FROM": deal.get("date_from"),
            "DATE_TO": deal.get("date_to"),
            "NIGHTS": deal.get("nights"),
            "HOTEL": deal.get("hotel"),
            "ROOM": deal.get("room"),
            "MEAL": deal.get("meal"),
            "TYPE": deal.get("tour_type"),
            "PRICE": deal.get("client_price") or 0,
            "CURRENCY": deal.get("currency") or "RUB",
            "SERVICES": deal.get("services") or "",
            "PROGRAM": deal.get("program") or "",
            "ROUTE": deal.get("route") or "",
            "GUIDE": deal.get("guide") or "",
            "FLIGHT_THERE": contact(deal.get("flight_there"), "number", "flight"),
            "FLIGHT_BACK": contact(deal.get("flight_back"), "number", "flight"),
            "HOST_CONTACTS": contact(deal.get("host_contacts"), "phone", "name"),
        },
        "COUNTRY": {
            "CODE": country.get("code"),
            "NAME": country.get("name"),
            "PASSPORT_MONTHS": country.get("passport_months"),
            "PASSPORT_DAYS": country.get("passport_days"),
            "PASSPORT_RULE_BASIS": country.get("passport_rule_basis"),
            "VISA_REQUIRED": country.get("visa_required") or 0,
            "VISA_LEAD_DAYS": country.get("visa_lead_days"),
            "MEMO": country.get("memo") or "",
        },
        "OPERATOR": {
            "FULL_NAME": operator.get("full_name"),
            "SHORT_NAME": operator.get("short_name"),
            "INN": operator.get("inn"),
            "OGRN": operator.get("ogrn"),
            "ADDRESS": operator.get("address"),
            "REGISTRY_NUMBER": operator.get("registry_number"),
            "GUARANTEE_TYPE": operator.get("guarantee_type"),
            "GUARANTEE_AMOUNT": operator.get("guarantee_amount") or 0,
            "GUARANTOR": operator.get("guarantor"),
            "GUARANTEE_NUMBER": operator.get("guarantee_number"),
            "GUARANTEE_FROM": operator.get("guarantee_from"),
            "GUARANTEE_TO": operator.get("guarantee_to"),
            "GUARANTEE_ADDRESS": operator.get("guarantee_address"),
            "EMERGENCY_PHONE": contact(operator.get("contacts"), "emergency", "emergency_phone", "phone"),
            "PHONE": contact(operator.get("contacts"), "phone"),
            "EMAIL": contact(operator.get("contacts"), "email"),
            "PORTAL_URL": operator.get("portal_url"),
            "PAYMENT_HOURS": operator.get("payment_hours"),
            "BANK_DETAILS": contact(operator.get("bank_details"), "account", "name"),
        },
        "MANAGER": {
            "FIO": manager.get("full_name"),
            "PHONE": manager.get("phone"),
            "EMAIL": manager.get("email"),
            "ROLE": manager.get("role"),
        },
        "TOURISTS": tourists_context(db, deal_id, foreign),
    }
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(context.get(key), dict):
            context[key] = {**context[key], **value}
        else:
            context[key] = value
    return context


def sync_templates(db, actor=None):
    """Переносит реестр шаблонов в БД: новая запись или новая версия при изменении текста."""
    created, updated = [], []
    with db.atomic():
        for item in registry.TEMPLATES:
            payload = {
                "name": item["name"],
                "type": item["type"],
                "format": item.get("format", "pdf+docx"),
                "body": item["body"],
                "required_fields": list(item.get("required_fields") or []),
            }
            row = db.find("templates", "type=?", (item["type"],))
            if row is None:
                row = db.insert("templates", {**payload, "version": 1, "active": 1, "changed_by": actor}, actor)
                db.insert(
                    "template_versions",
                    {"template_id": row["id"], "version": 1, "body": payload["body"], "required_fields": payload["required_fields"], "changed_by": actor},
                    actor,
                )
                created.append(item["type"])
                continue
            unchanged = (
                row.get("body") == payload["body"]
                and list(row.get("required_fields") or []) == payload["required_fields"]
                and row.get("name") == payload["name"]
            )
            if unchanged:
                continue
            version = int(row.get("version") or 1) + 1
            db.update("templates", row["id"], {**payload, "version": version, "active": 1, "changed_by": actor})
            db.insert(
                "template_versions",
                {"template_id": row["id"], "version": version, "body": payload["body"], "required_fields": payload["required_fields"], "changed_by": actor},
                actor,
            )
            updated.append(item["type"])
        if created or updated:
            db.audit(actor, "sync", "templates", "*", {"created": created, "updated": updated})
    return {"created": created, "updated": updated}


def template_row(db, code):
    """Активный шаблон из БД; если синхронизация ещё не выполнялась — из реестра кода."""
    row = db.find("templates", "type=?", (code,))
    if row and row.get("active"):
        return row
    item = registry.BY_TYPE.get(code)
    if not item:
        raise AppError("Неизвестный шаблон документа: " + str(code), 404)
    return {**item, "id": (row or {}).get("id"), "version": int((row or {}).get("version") or 1)}


def context_hash(context):
    return hashlib.sha256(canonical(context).encode()).hexdigest()


def document_key(deal_id, code, person_id, template_version, revision, digest):
    """Ключ идемпотентности: сделка + шаблон + версия данных + содержимое контекста."""
    return blind("|".join([str(deal_id), str(code), str(person_id or ""), str(template_version), str(revision), digest]))


def bind_contract(service, deal, document, files, template):
    """Привязывает договор к PDF: именно этот хеш потом подписывается ПЭП."""
    db = service.db
    payload = {
        "number": deal.get("number"),
        "date": today(),
        "type": "tour",
        "template_id": template.get("id"),
        "status": "черновик",
        "pdf_file_id": document["file_id"],
        "file_hash": files["sha256"],
        "document_id": document["id"],
        "signature_method": "ПЭП по СМС",
    }
    contract = db.find("contracts", "deal_id=? AND status<>?", (deal["id"], "аннулирован"))
    if contract and contract.get("signed_at"):
        return contract
    if contract:
        return db.update("contracts", contract["id"], payload)
    return db.insert("contracts", {"deal_id": deal["id"], **payload}, service.actor)


def generate(service, deal_id, code, extra=None, person_id=None, force=False):
    """Формирует документ по шаблону. Повтор без изменений возвращает прежнюю версию."""
    db = service.db
    deal = service.get("deals", deal_id, True)
    template = template_row(db, code)
    context = build_context(db, deal_id, extra, person_id)
    text = render.render_template(template, context)
    template_version = int(template.get("version") or 1)
    key = document_key(deal_id, code, person_id, template_version, deal.get("revision") or 1, context_hash(context))
    existing = db.find("documents", "idempotency_key=?", (key,))
    if existing and not force:
        return {"document": existing, "reused": True, "text": text}
    previous = [d for d in db.all("documents", "deal_id=? AND type=?", (deal_id, code))]
    version = max([int(d.get("version") or 0) for d in previous] or [0]) + 1
    base = f"{deal.get('number') or 'deal'}-{code}-v{version}"
    meta = {
        "title": template["name"],
        "agency": agency(db)["NAME"],
        "deal_number": "Сделка " + str(deal.get("number") or ""),
        "document_code": f"{code} v{version}",
        "generated_at": datetime.now(MSK).strftime("%d.%m.%Y %H:%M"),
        "verify_url": f"{config.PUBLIC_URL}/verify/{key[:16]}",
    }
    files = documents.build_files(text, meta)
    with db.atomic():
        pdf = storage.store_file(db, files["pdf"], base + ".pdf", PDF_MIME, "document", deal_id, service.actor, generated=True)
        docx = storage.store_file(db, files["docx"], base + ".docx", DOCX_MIME, "document", deal_id, service.actor, generated=True)
        for old in previous:
            if not old.get("superseded"):
                db.update("documents", old["id"], {"superseded": 1})
        row = db.insert(
            "documents",
            {
                "deal_id": deal_id,
                "person_id": person_id,
                "type": code,
                "source": "template",
                "file_id": pdf["id"],
                "docx_file_id": docx["id"],
                "version": version,
                "issued": 0,
                "template_id": template.get("id"),
                "template_version": template_version,
                "idempotency_key": key,
                "data_revision": int(deal.get("revision") or 1),
                "superseded": 0,
            },
            service.actor,
        )
        if code == "contract_tour":
            bind_contract(service, deal, row, files, template)
        service.audit("generate", "documents", row["id"], {"type": code, "version": version, "pages": files["pages"], "sha256": files["sha256"]})
    return {"document": row, "reused": False, "text": text, "pages": files["pages"], "sha256": files["sha256"]}


PER_PERSON = ("consent_pd", "consent_transfer", "consent_cross_border")


def package_flags(db, deal_id):
    """Признаки для условных шаблонов: зарубежный тур, несовершеннолетние, законный представитель."""
    deal = db.one("deals", deal_id)
    rows = tourist_rows(db, deal_id)
    on = deal.get("date_from") or today()
    minors = [p["id"] for link, p in rows if p.get("birth_date") and age(p["birth_date"], on) < 18]
    return {
        "tour_abroad": bool(deal.get("cross_border")),
        "has_minor": bool(minors),
        "has_representative": any(link.get("legal_representative_id") for link, _ in rows),
        "minors": minors,
    }


def package_plan(db, deal_id, name):
    """Состав пакета: обязательные шаблоны, условные и ожидаемые файлы от оператора."""
    plan = registry.PACKAGES.get(name)
    if not plan:
        raise AppError("Неизвестный пакет документов: " + str(name), 404)
    flags = package_flags(db, deal_id)
    codes = list(plan["templates"])
    if name == "on_contract":
        for code, flag in registry.CONDITIONAL.items():
            if flags.get(flag) and code not in codes:
                codes.append(code)
    return codes, list(plan.get("files") or []), flags


def current_documents(db, deal_id, code):
    return [d for d in db.all("documents", "deal_id=? AND type=?", (deal_id, code)) if not d.get("superseded")]


def task_for_missing(service, deal, name, missing):
    """Неполный пакет — задача менеджеру, а не молчаливый пропуск."""
    db = service.db
    titles = ", ".join(sorted({item["document"] for item in missing}))
    key = blind("|".join(["package-missing", str(deal["id"]), name, titles, str(deal.get("revision") or 1)]))
    if db.find("tasks", "idempotency_key=?", (key,)):
        return None
    return db.insert(
        "tasks",
        {
            "deal_id": deal["id"],
            "type": "package_incomplete",
            "title": "Пакет «{}» неполный: {}".format(registry.PACKAGES[name]["title"], titles),
            "due_at": now(),
            "assignee_id": deal.get("manager_id"),
            "status": "open",
            "automatic": 1,
            "idempotency_key": key,
        },
        service.actor,
    )


def build_package(service, deal_id, name, force=False):
    """Собирает единый PDF пакета. Недостающее не молчим: фиксируем и ставим задачу."""
    db = service.db
    deal = service.get("deals", deal_id, True)
    codes, expected, flags = package_plan(db, deal_id, name)
    ids, parts, missing = [], [], []
    for code in codes:
        if code == "consent_minor":
            targets = list(flags["minors"])
        elif code in PER_PERSON:
            targets = [person["id"] for _, person in tourist_rows(db, deal_id)]
        else:
            targets = [None]
        if code in PER_PERSON and not targets:
            missing.append({"document": code, "person_id": None, "reason": "В сделке нет туристов", "fields": []})
            continue
        for person_id in targets:
            try:
                result = generate(service, deal_id, code, person_id=person_id)
            except AppError as error:
                missing.append(
                    {
                        "document": code,
                        "person_id": person_id,
                        "reason": error.message,
                        "fields": list((error.details or {}).get("missing") or []),
                    }
                )
                continue
            ids.append(result["document"]["id"])
            parts.append(storage.read_file(db, result["document"]["file_id"])[0])
    for code in expected:
        rows = current_documents(db, deal_id, code)
        row = next((r for r in rows if r.get("source") in OPERATOR_SOURCES or r.get("file_id")), None)
        if not row or not row.get("file_id"):
            missing.append({"document": code, "person_id": None, "reason": "Файл от туроператора ещё не загружен", "fields": []})
            continue
        raw, meta = storage.read_file(db, row["file_id"])
        if meta.get("mime") != PDF_MIME:
            missing.append({"document": code, "person_id": None, "reason": "Файл не в формате PDF — в пакет не включён", "fields": []})
            continue
        ids.append(row["id"])
        parts.append(raw)
    if not parts:
        raise AppError("Пакет пуст: ни один документ не сформирован", 409, {"missing": missing})
    bundle = documents.merge_pdfs(parts)
    key = blind("|".join([str(deal_id), name, str(deal.get("revision") or 1), documents.digest(bundle)]))
    existing = db.find("packages", "idempotency_key=?", (key,))
    if existing and not force:
        return {"package": existing, "reused": True, "missing": missing, "pages": documents.page_count(bundle)}
    with db.atomic():
        stored = storage.store_file(
            db, bundle, f"{deal.get('number') or 'deal'}-{name}.pdf", PDF_MIME, "package", deal_id, service.actor, generated=True
        )
        row = db.insert(
            "packages",
            {
                "deal_id": deal_id,
                "type": name,
                "file_id": stored["id"],
                "document_ids": ids,
                "status": "неполный" if missing else "готов",
                "idempotency_key": key,
                "missing": missing,
            },
            service.actor,
        )
        if missing:
            task_for_missing(service, deal, name, missing)
        service.audit(
            "package", "packages", row["id"], {"type": name, "documents": len(ids), "missing": len(missing), "pages": documents.page_count(bundle)}
        )
    return {"package": row, "reused": False, "missing": missing, "pages": documents.page_count(bundle)}


def issue_link(service, package_id, days=30, person_id=None):
    """Защищённая ссылка на пакет: в базе хранится только хеш токена."""
    db = service.db
    package = db.one("packages", package_id)
    deal = service.get("deals", package["deal_id"], True)
    raw = token()
    expires = (datetime.now(timezone.utc) + timedelta(days=int(days))).isoformat(timespec="seconds")
    with db.atomic():
        for old in db.all("portal_links", "package_id=?", (package_id,)):
            if not old.get("revoked_at"):
                db.update("portal_links", old["id"], {"revoked_at": now()})
        link = db.insert(
            "portal_links",
            {
                "deal_id": deal["id"],
                "client_id": deal["client_id"],
                "person_id": person_id,
                "token_hash": blind(raw),
                "expires_at": expires,
                "package_id": package_id,
            },
            service.actor,
        )
        db.update("packages", package_id, {"issued_at": now()})
        service.audit("issue_link", "packages", package_id, {"link": link["id"], "expires_at": expires})
    return {"link": link, "url": f"{config.PUBLIC_URL}/p/{raw}", "token": raw, "expires_at": expires}


def link_by_token(db, raw):
    link = db.find("portal_links", "token_hash=?", (blind(str(raw or "")),))
    if not link:
        raise AppError("Ссылка недействительна", 404)
    if link.get("revoked_at"):
        raise AppError("Ссылка отозвана: документы изменились, запросите новую у менеджера", 410)
    if link.get("expires_at") and str(link["expires_at"]) < now():
        raise AppError("Срок действия ссылки истёк, запросите новую у менеджера", 410)
    return link


def mark_opened(db, link, actor=None):
    """Отметка открытия пакета — нужна для напоминания через 24 часа."""
    with db.atomic():
        if link.get("package_id"):
            package = db.one("packages", link["package_id"])
            if not package.get("opened_at"):
                db.update("packages", package["id"], {"opened_at": now()})
        if not link.get("consumed_at"):
            db.update("portal_links", link["id"], {"consumed_at": now()})
        db.audit(actor, "portal_open", "portal_links", link["id"], {"package_id": link.get("package_id")})
    return True


def document_file(service, document_id, kind="pdf"):
    """Выдаёт содержимое документа с проверкой доступа к сделке и записью в аудит."""
    db = service.db
    document = db.one("documents", document_id)
    service.get("deals", document["deal_id"])
    file_id = document.get("docx_file_id") if kind == "docx" else document.get("file_id")
    if not file_id:
        raise AppError("Файл документа не найден", 404)
    raw, meta = storage.read_file(db, file_id)
    service.audit("download", "documents", document_id, {"kind": kind})
    return raw, meta
