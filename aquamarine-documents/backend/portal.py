"""Кабинет туриста: доступ по одноразовой ссылке, код из СМС и подпись ПЭП.

Публичная часть (public_portal) работает до аутентификации: клиент открывает
ссылку из СМС, получает код и обменивает его на сессию портала. Дальше запросы
идут через portal_dispatch: сессия без user_id, но с link_id, поэтому клиент
видит только свою сделку и только документы своего пакета.

Номера документов в портале всегда маскируются: сверить данные клиент может,
а выгрузить их — нет (152-ФЗ, минимизация состава данных).
"""

import re
import secrets
from datetime import datetime, timedelta, timezone

from . import config, docflow, features, storage
from .db import now
from .security import AppError, blind, mask, token
from .security import phone as phone_number
from .service import dt, fio, rate_limit

CODE_TTL_MINUTES = 10
CODE_ATTEMPTS = 5
SESSION_HOURS = 4
SEND_LIMIT = 5
MESSAGE_LIMIT = 2000
PUBLIC_PATHS = ("/api/portal/open", "/api/portal/confirm")
DOCUMENT_FILE = re.compile(r"/api/portal/documents/([^/]+)/file(?:/(pdf|docx))?")


def fresh_code():
    """Шестизначный код: генерируется криптостойко, в базе хранится только хеш."""
    return str(secrets.randbelow(900000) + 100000)


def hide_phone(value):
    """Клиенту показываем только хвост номера — понять, куда ушло СМС, достаточно."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 4:
        return "—"
    return "+" + digits[0] + " ••• ••• " + digits[-4:-2] + " " + digits[-2:]


def rubles(value):
    """В базе суммы в копейках, клиенту отдаём рубли без потери копеек."""
    return round(int(value or 0) / 100, 2)


def recipient(db, link):
    """Кому отправлять код: указанному туристу, иначе заказчику сделки."""
    if link.get("person_id"):
        person = db.one("persons", link["person_id"])
        if person.get("phone"):
            return person, fio(person), person["phone"]
    client = db.one("clients", link["client_id"]) if link.get("client_id") else None
    if client and client.get("phone"):
        return None, str(client.get("full_name") or ""), client["phone"]
    raise AppError("У получателя не указан телефон — менеджер выдаст документы лично", 409)


def send_sms(db, link, challenge_id, destination, text, template):
    """Мок-адаптер СМС: сообщение ложится в очередь уведомлений с ключом идемпотентности."""
    mocked = config.PROVIDER_MODE == "mock"
    return db.insert(
        "notifications",
        {
            "client_id": link.get("client_id"),
            "deal_id": link.get("deal_id"),
            "channel": "sms",
            "template": template,
            "status": "доставлено" if mocked else "в очереди",
            "date": now(),
            "delivered_at": now() if mocked else None,
            "payload": {"text": text},
            "destination": destination,
            "idempotency_key": "sms:" + str(challenge_id),
            "attempts": 1,
            "provider_id": "mock-sms" if mocked else None,
        },
    )


def issue_code(db, link, purpose, destination, contract_id=None, binding=""):
    """Новый код гасит прежние: одновременно живёт только одна попытка."""
    rate_limit(db, "portal-send:" + str(link["id"]) + ":" + purpose, SEND_LIMIT, 600)
    for row in db.all("otp_challenges", "link_id=? AND purpose=? AND used_at IS NULL", (link["id"], purpose)):
        db.update("otp_challenges", row["id"], {"used_at": now(), "payload": {"reason": "заменён новым кодом"}})
    value = fresh_code()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)).isoformat()
    row = db.insert(
        "otp_challenges",
        {
            "link_id": link["id"],
            "contract_id": contract_id,
            "person_id": link.get("person_id"),
            "purpose": purpose,
            "phone": destination,
            "code_hash": blind(value),
            "expires_at": expires,
            "attempts": 0,
            "binding_hash": binding,
            "payload": {"deal_id": link.get("deal_id")},
        },
    )
    return row, value, expires


def check_code(db, link, purpose, value, ip):
    """Проверка кода: ограниченное число попыток, срок жизни и привязка к ссылке."""
    rate_limit(db, "portal-verify:" + str(ip), 30, 600)
    row = db.find("otp_challenges", "link_id=? AND purpose=? AND used_at IS NULL", (link["id"], purpose))
    if not row:
        raise AppError("Код не запрашивался или уже использован — запросите новый", 409)
    if dt(row["expires_at"]) < datetime.now(timezone.utc):
        raise AppError("Срок действия кода истёк — запросите новый", 410)
    attempts = int(row.get("attempts") or 0) + 1
    if attempts > CODE_ATTEMPTS:
        db.update("otp_challenges", row["id"], {"used_at": now()})
        raise AppError("Слишком много неверных попыток — запросите новый код", 429)
    if not secrets.compare_digest(blind(str(value or "").strip()), str(row["code_hash"])):
        db.update("otp_challenges", row["id"], {"attempts": attempts})
        raise AppError("Неверный код из СМС", 401, {"attempts_left": CODE_ATTEMPTS - attempts})
    db.update("otp_challenges", row["id"], {"attempts": attempts, "used_at": now()})
    return row


def public_portal(db, method, path, data, ip, headers):
    """Единственные адреса без сессии: запрос кода и обмен кода на сессию портала."""
    if path not in PUBLIC_PATHS:
        return None
    if method != "POST":
        raise AppError("Метод " + str(method) + " для этого адреса не поддерживается", 405)
    body = data or {}
    raw = str(body.get("token") or "").strip()
    if not raw:
        raise AppError("Ссылка неполная — откройте её из сообщения целиком")
    rate_limit(db, "portal-open:" + str(ip), 30, 600)
    agent = (headers or {}).get("user-agent", "")
    link = docflow.link_by_token(db, raw)
    deal = db.one("deals", link["deal_id"])
    _person, name, contact = recipient(db, link)
    destination = phone_number(contact)
    if path == "/api/portal/open":
        row, value, expires = issue_code(db, link, "portal_access", destination)
        text = "Аквамарин: код доступа к документам по сделке " + str(deal.get("number")) + ": " + value
        send_sms(db, link, row["id"], destination, text, "portal_access")
        db.audit(None, "portal_code_sent", "portal_links", link["id"], {"purpose": "portal_access"}, ip, agent)
        answer = {
            "deal": deal.get("number"),
            "name": name,
            "phone": hide_phone(destination),
            "expires_at": expires,
            "attempts": CODE_ATTEMPTS,
        }
        if not config.PRODUCTION:
            answer["demo_code"] = value
        return answer
    check_code(db, link, "portal_access", body.get("code"), ip)
    raw_session = token()
    csrf = blind(raw_session + ":csrf")
    db.insert(
        "sessions",
        {
            "link_id": link["id"],
            "token_hash": blind(raw_session),
            "csrf_hash": blind(csrf),
            "last_seen": now(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat(),
            "ip": str(ip),
            "user_agent": agent,
        },
    )
    docflow.mark_opened(db, link)
    db.audit(None, "portal_opened", "deals", link["deal_id"], {"link_id": link["id"]}, ip, agent)
    from .api import Response, cookie

    payload = {"ok": True, "csrf": csrf, "deal": deal.get("number"), "name": name}
    return Response(payload, 200, {"Set-Cookie": cookie(raw_session, SESSION_HOURS * 3600)})


def link_of(db, session):
    """Портальная сессия обязана ссылаться на живую ссылку — иначе доступ закрыт."""
    if not session or not session.get("link_id"):
        raise AppError("Войдите в систему", 401)
    link = db.one("portal_links", session["link_id"])
    if link.get("revoked_at"):
        db.update("sessions", session["id"], {"revoked_at": now()})
        raise AppError("Ссылка отозвана: документы изменились, запросите новую у менеджера", 410)
    if link.get("expires_at") and dt(link["expires_at"]) < datetime.now(timezone.utc):
        raise AppError("Срок действия ссылки истёк, запросите новую у менеджера", 410)
    return link


def template_name(db, row):
    """Клиенту нужно человеческое название документа, а не код шаблона."""
    if row.get("template_id"):
        try:
            return db.one("templates", row["template_id"]).get("name")
        except AppError:
            pass
    return str(row.get("type") or "")


def tourists(db, deal_id):
    """Состав туристов для сверки: латиница и сроки видны, номера — только маской."""
    rows = []
    for tie in db.all("deal_persons", "deal_id=?", (deal_id,)):
        person = db.one("persons", tie["person_id"])
        documents = [
            {
                "type": doc.get("type"),
                "number": mask(doc.get("number")),
                "expiry_date": doc.get("expiry_date"),
                "latin": " ".join(x for x in (doc.get("last_name_latin"), doc.get("first_name_latin")) if x),
            }
            for doc in db.all("identity_documents", "person_id=?", (person["id"],))
        ]
        tariff = str(tie.get("tariff") or "")
        rows.append(
            {
                "fio": fio(person),
                "birth_date": person.get("birth_date"),
                "tariff": docflow.TARIFFS.get(tariff, tariff),
                "placement": tie.get("placement"),
                "documents": documents,
            }
        )
    return rows


def signable_contract(db, deal_id, strict=True):
    """Подписывается последний неаннулированный договор с готовым PDF."""
    rows = [row for row in db.all("contracts", "deal_id=?", (deal_id,)) if row.get("status") != "аннулирован"]
    rows.sort(key=lambda row: str(row.get("created_at") or ""))
    contract = rows[-1] if rows else None
    if not strict:
        return contract
    if not contract:
        raise AppError("Договор ещё не сформирован — обратитесь к менеджеру", 409)
    if not contract.get("file_hash"):
        raise AppError("Договор ещё не готов к подписанию — обратитесь к менеджеру", 409)
    return contract


def deal_view(db, link):
    """Главный экран кабинета: что за тур, сколько осталось оплатить и к кому обращаться."""
    deal = db.one("deals", link["deal_id"])
    country = db.one("countries", deal["country_id"]) if deal.get("country_id") else None
    manager = db.one("users", deal["manager_id"]) if deal.get("manager_id") else None
    operator = db.one("operators", deal["operator_id"]) if deal.get("operator_id") else None
    contract = signable_contract(db, deal["id"], strict=False)
    price = int(deal.get("client_price") or 0)
    paid = int(deal.get("paid_client") or 0)
    return {
        "number": deal.get("number"),
        "status": deal.get("status"),
        "country": (country or {}).get("name"),
        "city": deal.get("city"),
        "hotel": deal.get("hotel"),
        "room": deal.get("room"),
        "meal": deal.get("meal"),
        "date_from": deal.get("date_from"),
        "date_to": deal.get("date_to"),
        "nights": deal.get("nights"),
        "flight_there": (deal.get("flight_there") or {}).get("summary"),
        "flight_back": (deal.get("flight_back") or {}).get("summary"),
        "operator": (operator or {}).get("full_name"),
        "price": rubles(price),
        "paid": rubles(paid),
        "due": rubles(price - paid),
        "client_deadline": deal.get("client_deadline"),
        "memo": (country or {}).get("memo"),
        "manager": {"fio": (manager or {}).get("full_name"), "phone": (manager or {}).get("phone")},
        "tourists": tourists(db, deal["id"]),
        "contract": None
        if not contract
        else {
            "number": contract.get("number"),
            "status": contract.get("status"),
            "signed_at": contract.get("signed_at"),
            "needs_signature": not contract.get("signed_at"),
        },
    }


def allowed_documents(db, link):
    """Клиент видит документы своего пакета, а без пакета — актуальные версии по сделке."""
    if link.get("package_id"):
        package = db.one("packages", link["package_id"])
        rows = []
        for document_id in list(package.get("document_ids") or []):
            try:
                rows.append(db.one("documents", document_id))
            except AppError:
                continue
        return package, rows
    rows = [row for row in db.all("documents", "deal_id=?", (link["deal_id"],)) if not row.get("superseded")]
    return None, rows


def documents_view(db, link):
    """Список документов для кабинета вместе со состоянием пакета."""
    package, rows = allowed_documents(db, link)
    items = []
    for row in rows:
        person = db.one("persons", row["person_id"]) if row.get("person_id") else None
        items.append(
            {
                "id": row["id"],
                "type": row.get("type"),
                "name": template_name(db, row),
                "version": row.get("version"),
                "person": fio(person) if person else "",
                "has_docx": bool(row.get("docx_file_id")),
            }
        )
    return {
        "items": items,
        "package": None
        if not package
        else {
            "id": package["id"],
            "type": package.get("type"),
            "status": package.get("status"),
            "opened_at": package.get("opened_at"),
            "has_file": bool(package.get("file_id")),
        },
    }


def file_answer(db, file_id, fallback):
    """Файл отдаётся в base64 с хешем — тот же формат, что и в кабинете сотрудника."""
    if not file_id:
        raise AppError("Файл документа не найден", 404)
    blob, meta = storage.read_file(db, file_id)
    return {"file": features.file_payload(blob, meta, fallback)}


def sign_request(db, link, ip, agent):
    """Запрос кода ПЭП: код жёстко привязан к хешу именно того PDF, который видел клиент."""
    contract = signable_contract(db, link["deal_id"])
    if contract.get("signed_at"):
        raise AppError("Договор уже подписан", 409)
    _person, name, contact = recipient(db, link)
    destination = phone_number(contact)
    row, value, expires = issue_code(db, link, "contract_sign", destination, contract["id"], contract["file_hash"])
    text = (
        "Аквамарин: код подписания договора "
        + str(contract.get("number"))
        + ": "
        + value
        + ". Никому не сообщайте код."
    )
    send_sms(db, link, row["id"], destination, text, "contract_sign")
    db.audit(None, "sign_code_sent", "contracts", contract["id"], {"file_hash": contract["file_hash"]}, ip, agent)
    answer = {
        "contract": contract.get("number"),
        "signer": name,
        "phone": hide_phone(destination),
        "expires_at": expires,
        "file_hash": contract["file_hash"],
    }
    if not config.PRODUCTION:
        answer["demo_code"] = value
    return answer


def sign_confirm(db, link, data, ip, agent):
    """Подтверждение ПЭП: фиксируем телефон, IP, время и хеш файла как доказательство."""
    contract = signable_contract(db, link["deal_id"])
    if contract.get("signed_at"):
        raise AppError("Договор уже подписан", 409)
    row = check_code(db, link, "contract_sign", (data or {}).get("code"), ip)
    if str(row.get("binding_hash") or "") != str(contract.get("file_hash") or ""):
        raise AppError("Договор изменился после запроса кода — запросите код заново", 409)
    _person, name, contact = recipient(db, link)
    destination = phone_number(contact)
    signed_at = now()
    evidence = {
        "method": "простая электронная подпись (код из СМС)",
        "signer": name,
        "phone": hide_phone(destination),
        "code_requested_at": row.get("created_at"),
        "confirmed_at": signed_at,
        "ip": str(ip),
        "user_agent": agent,
        "file_hash": contract.get("file_hash"),
        "link_id": link["id"],
        "challenge_id": row["id"],
    }
    db.update(
        "contracts",
        contract["id"],
        {
            "status": "подписан",
            "signature_method": "ПЭП: код из СМС",
            "signer_ip": str(ip),
            "signer_phone": destination,
            "sms_code_hash": row.get("code_hash"),
            "signed_at": signed_at,
            "signed_file_hash": contract.get("file_hash"),
            "evidence": evidence,
        },
    )
    deal = db.one("deals", link["deal_id"])
    db.insert(
        "notifications",
        {
            "user_id": deal.get("manager_id"),
            "deal_id": deal["id"],
            "channel": "in-app",
            "template": "contract_signed",
            "status": "новое",
            "date": signed_at,
            "payload": {"contract": contract.get("number"), "signer": name},
            "idempotency_key": "signed:" + contract["id"],
            "attempts": 0,
        },
    )
    db.audit(None, "contract_signed", "contracts", contract["id"], {"method": "ПЭП", "link_id": link["id"]}, ip, agent)
    return {"ok": True, "contract": contract.get("number"), "status": "подписан", "signed_at": signed_at}


def portal_dispatch(db, session, method, path, data, ip, headers):
    """Адреса кабинета туриста. Сессия без user_id, доступ ограничен одной сделкой."""
    link = link_of(db, session)
    agent = (headers or {}).get("user-agent", "")
    if path == "/api/portal/deal" and method == "GET":
        return deal_view(db, link)
    if path == "/api/portal/documents" and method == "GET":
        return documents_view(db, link)
    match = DOCUMENT_FILE.fullmatch(path)
    if match and method == "GET":
        kind = match[2] or "pdf"
        _package, rows = allowed_documents(db, link)
        row = next((item for item in rows if item["id"] == match[1]), None)
        if not row:
            raise AppError("Документ не найден в вашем пакете", 404)
        db.audit(None, "portal_download", "documents", row["id"], {"kind": kind, "link_id": link["id"]}, ip, agent)
        file_id = row.get("docx_file_id") if kind == "docx" else row.get("file_id")
        return file_answer(db, file_id, str(row.get("type") or "document") + "." + kind)
    if path == "/api/portal/package/file" and method == "GET":
        package, _rows = allowed_documents(db, link)
        if not package:
            raise AppError("Пакет документов по ссылке не передавался", 404)
        db.audit(None, "portal_download", "packages", package["id"], {"link_id": link["id"]}, ip, agent)
        return file_answer(db, package.get("file_id"), str(package.get("type") or "package") + ".pdf")
    if path == "/api/portal/sign/request" and method == "POST":
        return sign_request(db, link, ip, agent)
    if path == "/api/portal/sign/confirm" and method == "POST":
        return sign_confirm(db, link, data, ip, agent)
    if path == "/api/portal/messages" and method == "POST":
        body = str((data or {}).get("body") or "").strip()
        if not body:
            raise AppError("Сообщение пустое")
        if len(body) > MESSAGE_LIMIT:
            raise AppError("Сообщение длиннее " + str(MESSAGE_LIMIT) + " символов — сократите текст")
        rate_limit(db, "portal-message:" + str(link["id"]), 20, 3600)
        db.insert(
            "messages",
            {
                "deal_id": link["deal_id"],
                "sender_id": link.get("person_id") or link.get("client_id"),
                "sender_type": "клиент",
                "body": body,
            },
        )
        return {"ok": True}
    raise AppError("Страница не найдена", 404)
