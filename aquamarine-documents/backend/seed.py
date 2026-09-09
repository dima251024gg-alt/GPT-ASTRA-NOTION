"""Демонстрационный набор данных «Аквамарин.Документы».

Наполняет базу: 5 сотрудников (директор, старший менеджер, два менеджера,
бухгалтер), 5 стран с правилами по загранпаспорту, 3 туроператора, 10 заказчиков,
15 сделок на всех стадиях воронки, туристов с загранпаспортами и согласиями,
платежи и все шаблоны документов.

Запуск: python3 -m backend.seed

Повторный запуск не создаёт дублей: сверка идёт по e-mail сотрудника, коду
страны, ИНН туроператора, телефону заказчика и номеру сделки. Пароль всех
демо-сотрудников — Aquamarine2026; в production набор не запускается.
"""

from datetime import date, timedelta

from . import config, ocr
from .db import Database, now
from .security import AppError, blind, hash_password
from .service import STATUSES, document_key, identity_key

PASSWORD = "Aquamarine2026"
BASE = date.today()
YEAR = str(BASE.year)

# (ФИО, e-mail, телефон, роль)
USERS = (
    ("Ковалёва Ирина Сергеевна", "director@aquamarine.ru", "+7 916 100-10-01", "admin"),
    ("Морозов Андрей Викторович", "senior@aquamarine.ru", "+7 916 100-10-02", "senior"),
    ("Сергеев Павел Андреевич", "manager1@aquamarine.ru", "+7 916 100-10-03", "manager"),
    ("Никитина Ольга Дмитриевна", "manager2@aquamarine.ru", "+7 916 100-10-04", "manager"),
    ("Белова Татьяна Ивановна", "buh@aquamarine.ru", "+7 916 100-10-05", "accountant"),
)

# (код, название, месяцев после возвращения, дней, основание, виза, срок визы, памятка)
COUNTRIES = (
    ("TR", "Турция", 6, 0, "Правила въезда Республики Турция", 0, 0,
     "Загранпаспорт действителен не менее 6 месяцев после даты возвращения. Виза не требуется при пребывании до 60 суток."),
    ("AE", "ОАЭ", 6, 0, "Правила въезда Объединённых Арабских Эмиратов", 0, 0,
     "Загранпаспорт действителен не менее 6 месяцев с даты въезда. Безвизовый въезд для граждан РФ до 90 суток в году."),
    ("EG", "Египет", 6, 0, "Правила въезда Арабской Республики Египет", 0, 0,
     "Загранпаспорт действителен не менее 6 месяцев. Виза оформляется по прибытии, сбор оплачивается туристом самостоятельно."),
    ("TH", "Таиланд", 6, 0, "Правила въезда Королевства Таиланд", 0, 0,
     "Загранпаспорт действителен не менее 6 месяцев. Безвизовое пребывание до 60 суток, обратный билет обязателен."),
    ("IT", "Италия", 3, 90, "Визовый кодекс Европейского союза", 1, 21,
     "Шенгенская виза оформляется заранее, запись в визовый центр за 21 день. Паспорт действителен 3 месяца после возвращения."),
)

OPERATORS = (
    {
        "full_name": "ООО «Санмарин Тур»",
        "short_name": "Санмарин",
        "inn": "7708123456",
        "ogrn": "1157746001111",
        "address": "115035, г. Москва, Садовническая ул., д. 12",
        "registry_number": "РТО 012345",
        "guarantee_type": "фонд персональной ответственности",
        "guarantee_amount": 5000000000,
        "guarantor": "Ассоциация «Турпомощь»",
        "guarantee_number": "ФПО-" + YEAR + "-114",
        "guarantee_address": "101000, г. Москва, ул. Мясницкая, д. 1",
        "contacts": {"phone": "+7 495 120-30-40", "emergency": "+7 800 100-20-30", "email": "agents@sunmarin.test"},
        "portal_url": "https://agents.sunmarin.test",
        "default_commission_bps": 1000,
        "payment_hours": 24,
        "agent_agreement": "АГ-" + YEAR + "-15 от 12.01." + YEAR,
    },
    {
        "full_name": "ООО «Азур Вояж»",
        "short_name": "Азур Вояж",
        "inn": "7710234567",
        "ogrn": "1167746002222",
        "address": "127006, г. Москва, ул. Долгоруковская, д. 7",
        "registry_number": "РТО 023456",
        "guarantee_type": "договор страхования ответственности",
        "guarantee_amount": 3000000000,
        "guarantor": "АО «Страховая компания Демо»",
        "guarantee_number": "ДС-" + YEAR + "-887",
        "guarantee_address": "125009, г. Москва, ул. Тверская, д. 22",
        "contacts": {"phone": "+7 495 130-40-50", "emergency": "+7 800 200-30-40", "email": "agents@azurvoyage.test"},
        "portal_url": "https://b2b.azurvoyage.test",
        "default_commission_bps": 900,
        "payment_hours": 48,
        "agent_agreement": "АГ-" + YEAR + "-22 от 03.02." + YEAR,
    },
    {
        "full_name": "ООО «Бриз Тревел»",
        "short_name": "Бриз",
        "inn": "7712345678",
        "ogrn": "1177746003333",
        "address": "197101, г. Санкт-Петербург, ул. Мира, д. 3",
        "registry_number": "РТО 034567",
        "guarantee_type": "фонд персональной ответственности",
        "guarantee_amount": 2500000000,
        "guarantor": "Ассоциация «Турпомощь»",
        "guarantee_number": "ФПО-" + YEAR + "-256",
        "guarantee_address": "101000, г. Москва, ул. Мясницкая, д. 1",
        "contacts": {"phone": "+7 812 140-50-60", "emergency": "+7 800 300-40-50", "email": "agents@briztravel.test"},
        "portal_url": "https://agents.briztravel.test",
        "default_commission_bps": 1200,
        "payment_hours": 24,
        "agent_agreement": "АГ-" + YEAR + "-31 от 19.02." + YEAR,
    },
)

# (фамилия, имя, отчество, пол, дата рождения, телефон, e-mail, источник, адрес регистрации)
CLIENTS = (
    ("Семёнов", "Илья", "Петрович", "м", "1985-03-12", "+7 916 200-10-01", "semenov@example.test", "рекомендация", "125009, г. Москва, ул. Тверская, д. 15, кв. 40"),
    ("Гаврилова", "Мария", "Андреевна", "ж", "1990-07-24", "+7 916 200-10-02", "gavrilova@example.test", "соцсети", "117312, г. Москва, ул. Вавилова, д. 4, кв. 112"),
    ("Ким", "Сергей", "Викторович", "м", "1978-11-05", "+7 916 200-10-03", "kim@example.test", "повторный клиент", "196143, г. Санкт-Петербург, Пулковское ш., д. 30, кв. 5"),
    ("Абрамова", "Ольга", "Николаевна", "ж", "1983-01-19", "+7 916 200-10-04", "abramova@example.test", "сайт", "143000, Московская обл., г. Одинцово, ул. Советская, д. 2, кв. 71"),
    ("Дроздов", "Николай", "Юрьевич", "м", "1971-05-30", "+7 916 200-10-05", "drozdov@example.test", "рекомендация", "420012, г. Казань, ул. Бутлерова, д. 9, кв. 18"),
    ("Королёва", "Анна", "Ильинична", "ж", "1994-09-02", "+7 916 200-10-06", "koroleva@example.test", "соцсети", "630099, г. Новосибирск, ул. Ленина, д. 12, кв. 33"),
    ("Петров-Водкин", "Артём", "Сергеевич", "м", "1988-12-15", "+7 916 200-10-07", "petrov-vodkin@example.test", "сайт", "344002, г. Ростов-на-Дону, ул. Большая Садовая, д. 40, кв. 9"),
    ("Ильина", "Дарья", "", "ж", "1996-04-08", "+7 916 200-10-08", "ilina@example.test", "соцсети", "620014, г. Екатеринбург, ул. Вайнера, д. 21, кв. 55"),
    ("Захаров", "Дмитрий", "Олегович", "м", "1980-06-21", "+7 916 200-10-09", "zaharov@example.test", "повторный клиент", "350000, г. Краснодар, ул. Красная, д. 8, кв. 24"),
    ("Мурадова", "Лейла", "Рустамовна", "ж", "1992-02-27", "+7 916 200-10-10", "muradova@example.test", "рекомендация", "367000, г. Махачкала, ул. Ленина, д. 5, кв. 3"),
)

# Сопровождающие туристы добавляются программно, чтобы не дублировать таблицы.
SPOUSES = (("Анна", "Сергеевна", "ж"), ("Игорь", "Петрович", "м"))
CHILDREN = (("Мия", "ж", "дочь"), ("Тимур", "м", "сын"))

# (заказчик, страна, город, отель, номер, питание, вылет через суток, ночей,
#  взрослых, детей, цена в рублях, индекс статуса, доля оплаты, состав)
DEALS = (
    (0, "TR", "Анталия", "Sunrise Beach Resort 5*", "Standard Sea View", "всё включено", 45, 7, 2, 0, 245000, 6, 0.0, "family"),
    (1, "AE", "Дубай", "Marina Bay Hotel 4*", "Deluxe", "завтраки", 30, 6, 2, 1, 318000, 7, 0.5, "child"),
    (2, "EG", "Хургада", "Coral Reef Resort 5*", "Family Room", "всё включено", 21, 10, 2, 0, 289000, 8, 1.0, "family"),
    (3, "TH", "Пхукет", "Andaman Pearl 4*", "Superior", "завтраки", 60, 12, 2, 0, 402000, 5, 0.0, "family"),
    (4, "IT", "Римини", "Adriatico Palace 4*", "Double", "полупансион", 90, 8, 2, 0, 356000, 4, 0.0, "family"),
    (5, "TR", "Кемер", "Olive Garden 5*", "Superior Land View", "всё включено", 14, 7, 1, 0, 132000, 9, 1.0, ""),
    (6, "AE", "Абу-Даби", "Corniche Grand 5*", "Club Room", "завтраки", 120, 5, 1, 0, 168000, 1, 0.0, ""),
    (7, "TR", "Белек", "Golf Resort Belek 5*", "Family Suite", "всё включено", 75, 9, 2, 1, 512000, 4, 0.0, "child"),
    (8, "EG", "Шарм-эль-Шейх", "Blue Lagoon 4*", "Standard", "всё включено", 10, 7, 2, 0, 214000, 11, 1.0, "family"),
    (9, "TH", "Паттая", "Siam Garden 3*", "Standard", "завтраки", 150, 14, 2, 0, 298000, 0, 0.0, ""),
    (0, "IT", "Рим", "Roma Centro 4*", "Twin", "завтраки", 200, 5, 2, 0, 276000, 0, 0.0, ""),
    (2, "TR", "Аланья", "Green Valley 4*", "Standard", "всё включено", 8, 7, 2, 0, 189000, 10, 1.0, "family"),
    (3, "AE", "Дубай", "Palm Jumeirah Resort 5*", "Ocean Suite", "полупансион", 35, 6, 2, 0, 470000, 2, 0.0, "family"),
    (4, "EG", "Марса-Алам", "Red Sea Bay 5*", "Standard", "всё включено", 28, 8, 2, 0, 232000, 12, 0.3, "family"),
    (5, "TR", "Сиде", "Antique Side 5*", "Standard", "всё включено", 55, 7, 2, 0, 205000, 3, 0.0, "expiring"),
)


def ensure(db, table, where, args, data, actor=None):
    """Идемпотентная вставка: повторный запуск сидов вернёт уже созданную запись."""
    row = db.find(table, where, args)
    return row if row else db.insert(table, data, actor)


def latin(text):
    """Латиница по Приказу МВД № 889 — так же, как её строит OCR-ядро."""
    return str(ocr.transliterate(str(text or ""))).upper()


def person_row(db, client, last, first, patronymic, gender, birth, relation, actor):
    data = {
        "client_id": client["id"],
        "last_name_ru": last,
        "first_name_ru": first,
        "patronymic_ru": patronymic,
        "gender": gender,
        "birth_date": birth,
        "citizenship": "Россия",
        "birth_place": "г. Москва",
        "phone": client["phone"] if relation == "заказчик" else "",
        "email": client["email"] if relation == "заказчик" else "",
        "relationship": relation,
        "needs_visa": 0,
        "processing_blocked": 0,
        "legal_hold": 0,
    }
    data["identity_key"] = identity_key(data)
    return ensure(db, "persons", "identity_key=?", (data["identity_key"],), data, actor)


def passport_row(db, person, series, number, expiry, actor):
    """Загранпаспорт с реальной MRZ TD3: латиница берётся именно из MRZ."""
    last, first = latin(person["last_name_ru"]), latin(person["first_name_ru"])
    sex = "M" if str(person.get("gender") or "").startswith("м") else "F"
    data = {
        "person_id": person["id"],
        "type": "загранпаспорт РФ",
        "series": series,
        "number": number,
        "issued_by": "ФМС 77001",
        "issue_date": (BASE - timedelta(days=900)).isoformat(),
        "expiry_date": expiry,
        "last_name_latin": last,
        "first_name_latin": first,
        "issuing_country": "RUS",
        "mrz_raw": ocr.build_mrz(last, first, series + number, person["birth_date"], expiry, sex),
        "verification_status": "подтверждён",
        "is_primary": 1,
        "manual_mrz_review": 0,
    }
    data["document_key"] = document_key(data)
    return ensure(db, "identity_documents", "document_key=?", (data["document_key"],), data, actor)


def consent_rows(db, person, deal_id, actor):
    """Согласия на ПД: обработка, передача туроператору и трансграничная передача."""
    out = []
    for kind, purpose in (("consent_pd", "обработка персональных данных для бронирования тура"),
                          ("consent_transfer", "передача данных туроператору и страховщику"),
                          ("consent_cross_border", "трансграничная передача данных принимающей стороне")):
        rows = db.find("consents", "person_id=? AND type=?", (person["id"], kind))
        if rows:
            out.append(rows)
            continue
        out.append(db.insert("consents", {
            "person_id": person["id"],
            "type": kind,
            "received_at": now(),
            "method": "подпись на бумаге",
            "purposes": [purpose],
            "recipients": ["туроператор", "страховщик"],
            "text_version": "v1",
            "deal_id": deal_id,
            "ip": "127.0.0.1",
            "user_agent": "seed",
        }, actor))
    return out


def staff(db):
    """Сотрудники агентства с ролями из §3 ТЗ. E-mail не шифруется — ищем по нему."""
    out = []
    for full_name, email, phone, role in USERS:
        out.append(ensure(db, "users", "email=?", (email,), {
            "full_name": full_name,
            "email": email,
            "phone": phone,
            "role": role,
            "active": 1,
            "two_fa_enabled": 0,
            "password_hash": hash_password(PASSWORD),
            "failed_attempts": 0,
            "last_totp_step": 0,
        }))
    return out


def countries(db, actor):
    out = {}
    for code, name, months, days, basis, visa, lead, memo in COUNTRIES:
        out[code] = ensure(db, "countries", "code=?", (code,), {
            "code": code,
            "name": name,
            "passport_months": months,
            "passport_days": days,
            "passport_rule_basis": basis,
            "visa_required": visa,
            "visa_lead_days": lead,
            "memo": memo,
            "rules_checked_at": now(),
        }, actor)
    return out


def operators(db, actor):
    out = []
    for spec in OPERATORS:
        data = dict(spec)
        data["guarantee_from"] = YEAR + "-01-01"
        data["guarantee_to"] = YEAR + "-12-31"
        out.append(ensure(db, "operators", "inn=?", (data["inn"],), data, actor))
    return out


def clients(db, managers, actor):
    """Заказчики делятся между менеджерами: видно разграничение доступа."""
    out = []
    for index, spec in enumerate(CLIENTS):
        last, first, patronymic, gender, birth, phone, email, source, address = spec
        manager = managers[index % len(managers)]
        full_name = " ".join(part for part in (last, first, patronymic) if part)
        client = ensure(db, "clients", "contact_key=?", (blind(phone),), {
            "type": "individual",
            "full_name": full_name,
            "phone": phone,
            "email": email,
            "messenger": "telegram",
            "messenger_username": "@" + email.split("@")[0],
            "source": source,
            "registration_address": address,
            "manager_id": manager["id"],
            "birth_date": birth,
            "status": "клиент",
            "contact_key": blind(phone),
            "tags": ["демо"],
        }, actor)
        out.append((client, (last, first, patronymic, gender, birth)))
    return out


def hotels(db, countries_by_code, actor):
    seen = {}
    for spec in DEALS:
        code, city, hotel = spec[1], spec[2], spec[3]
        if hotel in seen:
            continue
        slug = "".join(ch for ch in hotel.lower() if ch.isalnum())[:16]
        seen[hotel] = ensure(db, "hotels", "name=?", (hotel,), {
            "name": hotel,
            "country_id": countries_by_code[code]["id"],
            "city": city,
            "category": hotel.split()[-1],
            "address": city + ", " + hotel,
            "contacts": {"phone": "+90 242 000-00-00", "email": "reception@" + slug + ".test"},
        }, actor)
    return seen


def travellers(db, client, profile, deal, flag, numbers, actor):
    """Состав сделки: заказчик, супруг(а) и ребёнок — по последнему полю DEALS.

    При flag="expiring" у сопровождающего туриста загранпаспорт истекает через 100 дней —
    это готовая демонстрация блокирующего замечания V1 (правило шести месяцев).
    """
    last, first, patronymic, gender, birth = profile
    rows = [(person_row(db, client, last, first, patronymic, gender, birth, "заказчик", actor), "adult", 0)]
    if flag in ("family", "child", "expiring"):
        spouse_first, spouse_patronymic, spouse_gender = SPOUSES[0] if gender == "м" else SPOUSES[1]
        spouse_birth = (date.fromisoformat(birth) + timedelta(days=730)).isoformat()
        rows.append((person_row(db, client, last, spouse_first, spouse_patronymic, spouse_gender,
                                spouse_birth, "супруг(а)", actor), "adult", 0))
    if flag == "child":
        child_first, child_gender, relation = CHILDREN[0] if gender == "м" else CHILDREN[1]
        child_birth = (BASE - timedelta(days=8 * 365)).isoformat()
        rows.append((person_row(db, client, last, child_first, "", child_gender,
                                child_birth, relation, actor), "child", 2))
    for index, item in enumerate(rows):
        person, tariff, parents = item
        short = flag == "expiring" and index == 1
        expiry = (BASE + timedelta(days=100 if short else 1825)).isoformat()
        number = numbers.setdefault(person["id"], "%07d" % (1234500 + len(numbers) + 1))
        passport_row(db, person, "75", number, expiry, actor)
        consent_rows(db, person, deal["id"], actor)
        if not db.find("deal_persons", "deal_id=? AND person_id=?", (deal["id"], person["id"])):
            db.insert("deal_persons", {
                "deal_id": deal["id"],
                "person_id": person["id"],
                "role": "турист",
                "placement": "основное место" if tariff == "adult" else "дополнительное место",
                "tariff": tariff,
                "parents_count": parents,
            }, actor)
    return rows


def payment_rows(db, deal, number, index, client_price, commission, share, actor):
    """Приход от заказчика и расход туроператору — всё в копейках."""
    if share <= 0:
        return 0
    made = 0
    plan = (
        ("client", "приход", int(client_price * share), "карта", "ЧК-" + str(2000 + index), "acq-" + number),
        ("operator", "расход", int((client_price - commission) * share), "безналичный расчёт", "", "op-" + number),
    )
    for tag, direction, amount, method, receipt, external in plan:
        key = "seed:pay:" + number + ":" + tag
        if db.find("payments", "idempotency_key=?", (key,)):
            continue
        db.insert("payments", {
            "deal_id": deal["id"],
            "direction": direction,
            "amount": amount,
            "currency": "RUB",
            "date": now(),
            "method": method,
            "receipt_number": receipt,
            "status": "проведён",
            "external_id": external,
            "idempotency_key": key,
        }, actor)
        made += 1
    return made


def deals(db, client_pairs, countries_by_code, operator_rows, actor):
    """15 сделок по всей воронке: от лида до аннуляции, с туристами и оплатами."""
    numbers = {}
    made = []
    for index, spec in enumerate(DEALS, start=1):
        client_index, code, city, hotel, room, meal, offset, nights = spec[:8]
        adults, children, price, status_index, share, flag = spec[8:]
        client, profile = client_pairs[client_index]
        country = countries_by_code[code]
        operator = operator_rows[index % len(operator_rows)]
        number = "АКВ-" + YEAR + "-" + "%05d" % index
        start = BASE + timedelta(days=offset)
        finish = start + timedelta(days=nights)
        bps = int(operator["default_commission_bps"] or 1000)
        client_price = price * 100
        commission = client_price * bps // 10000
        status = STATUSES[status_index]
        booked = status_index >= 6
        row = db.find("deals", "number=?", (number,))
        if not row:
            row = db.insert("deals", {
                "number": number,
                "client_id": client["id"],
                "manager_id": client["manager_id"],
                "status": status,
                "country_id": country["id"],
                "city": city,
                "date_from": start.isoformat(),
                "date_to": finish.isoformat(),
                "nights": nights,
                "hotel": hotel,
                "room": room,
                "meal": meal,
                "adults": adults,
                "children": children,
                "infants": 0,
                "flight_there": {
                    "number": "TK 41" + str(index % 10),
                    "from": "SVO",
                    "to": code,
                    "departure": start.isoformat() + "T08:40:00+03:00",
                    "arrival": start.isoformat() + "T13:10:00+03:00",
                },
                "flight_back": {
                    "number": "TK 42" + str(index % 10),
                    "from": code,
                    "to": "SVO",
                    "departure": finish.isoformat() + "T14:20:00+03:00",
                    "arrival": finish.isoformat() + "T19:05:00+03:00",
                },
                "tour_type": "package",
                "booking_number": ("BK-" + YEAR + "-" + str(1000 + index)) if booked else "",
                "operator_id": operator["id"],
                "currency": "RUB",
                "exchange_rate": "1",
                "client_price": client_price,
                "operator_net": client_price - commission,
                "commission": commission,
                "paid_client": int(client_price * share),
                "paid_operator": int((client_price - commission) * share),
                "client_deadline": (BASE + timedelta(days=1)).isoformat() + "T18:00:00+03:00",
                "operator_deadline": (BASE + timedelta(days=2)).isoformat() + "T12:00:00+03:00",
                "cross_border": 1,
                "comments": "Демонстрационная сделка набора сидов.",
                "program": "Пляжный отдых, " + str(nights) + " ночей, город " + city + ".",
                "route": "Москва — " + city + " — Москва",
                "guide": "русскоговорящий представитель принимающей стороны",
                "services": "авиаперелёт Москва — " + city + " — Москва; групповой трансфер аэропорт — отель — аэропорт; проживание в " + hotel + " (" + room + "); питание: " + meal + "; медицинская страховка на весь период поездки",
                "host_contacts": "Принимающая сторона: " + operator["short_name"] + " Destination, тел. +90 242 000-11-22, круглосуточно",
                "recipients": ["туроператор", "страховая компания", "принимающая сторона"],
                "last_contact_at": now(),
                "completed_at": finish.isoformat() if status_index == 11 else "",
                "revision": 1,
            }, actor)
        # На стадиях лида и подбора туристов ещё не вносят — так же работают менеджеры.
        if status_index >= 2:
            travellers(db, client, profile, row, flag, numbers, actor)
        payment_rows(db, row, number, index, client_price, commission, share, actor)
        made.append(row)
    return made


def run(url=None, with_templates=True):
    """Заливает демо-набор и возвращает сводку. В production не работает."""
    if config.PRODUCTION:
        raise AppError("Демонстрационные данные нельзя загружать в production", 403)
    db = Database(url)
    db.migrate()
    try:
        users = staff(db)
        admin = users[0]
        managers = [item for item in users if item["role"] == "manager"]
        countries_by_code = countries(db, admin["id"])
        operator_rows = operators(db, admin["id"])
        hotels(db, countries_by_code, admin["id"])
        client_pairs = clients(db, managers, admin["id"])
        made = deals(db, client_pairs, countries_by_code, operator_rows, admin["id"])
        # Счётчик номеров догоняем до демо-сделок, иначе боевая сделка получит занятый номер.
        counter = db.find("counters", "name=?", (YEAR,))
        if not counter:
            db.insert("counters", {"name": YEAR, "value": len(DEALS)}, admin["id"])
        elif int(counter["value"] or 0) < len(DEALS):
            db.update("counters", counter["id"], {"value": len(DEALS)})
        if with_templates:
            from . import docflow  # ленивый импорт: движок PDF нужен только здесь

            docflow.sync_templates(db, admin["id"])
        return {
            "сотрудники": len(users),
            "страны": len(countries_by_code),
            "туроператоры": len(operator_rows),
            "заказчики": len(client_pairs),
            "туристы": len(db.all("persons")),
            "загранпаспорта": len(db.all("identity_documents")),
            "согласия": len(db.all("consents")),
            "сделки": len(made),
            "платежи": len(db.all("payments")),
            "шаблоны": len(db.all("templates")),
            "пароль": PASSWORD,
        }
    finally:
        db.close()


if __name__ == "__main__":
    report = run()
    print("Демо-данные «Аквамарин.Документы» загружены:")
    for key, value in report.items():
        print("  " + key + ": " + str(value))
