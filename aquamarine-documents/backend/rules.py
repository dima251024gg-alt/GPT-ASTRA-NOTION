"""Валидации сделки V1–V11 (спринт S4).

Проверки ничего не меняют в данных: каждая возвращает список замечаний вида
{code, title, level, message, person, details}. Решение — блокировать переход
по статусу, поставить задачу или просто показать предупреждение — принимает
вызывающий код (workflow, API, карточка сделки).

Нумерация закреплена так, чтобы V10 совпадала с уже реализованной в спринте S3
проверкой обязательных полей шаблона (render.missing_fields).
"""

import calendar
from datetime import date, timedelta

from .db import now
from .ocr import latin_normal, parse_mrz, transliterate
from .security import AppError
from .service import STATUSES, age, as_date, fio

BLOCK = "блокирующая"
WARN = "предупреждение"

TITLES = {
    "V1": "Срок действия загранпаспорта",
    "V2": "Латиница совпадает с MRZ",
    "V3": "Тариф по возрасту на дату вылета",
    "V4": "Согласие на обработку персональных данных",
    "V5": "Несовершеннолетний без законных представителей",
    "V6": "Дедлайны оплаты",
    "V7": "Нетто, комиссия и цена клиента",
    "V8": "Дубли туристов и документов",
    "V9": "Виза",
    "V10": "Обязательные поля шаблонов",
    "V11": "Трансграничная передача данных",
}

TARIFF_NAMES = {"adult": "взрослый", "child": "ребёнок", "infant": "младенец без места"}
CONSENT_WORDS = {
    "consent_pd": "обработк",
    "consent_transfer": "передач",
    "consent_cross_border": "трансгранич",
}

INFANT_AGE = 2
CHILD_AGE = 12
ADULT_AGE = 18
COMMISSION_MIN_BPS = 800
COMMISSION_MAX_BPS = 1200
REMINDER_HOURS = 24
ESCALATION_HOURS = 4


def ru_date(value):
    """Дата в привычном для договора виде ДД.ММ.ГГГГ."""
    if not value:
        return "—"
    day = as_date(value)
    return "%02d.%02d.%04d" % (day.day, day.month, day.year)


def rubles(kopecks):
    """Копейки во внутреннем формате — в рубли для текста замечания."""
    amount = int(kopecks or 0)
    whole = "{:,}".format(amount // 100).replace(",", "\u00a0")
    return whole + "," + "%02d" % (amount % 100) + "\u00a0₽"


def years_word(count):
    tail = count % 100
    if 11 <= tail <= 14:
        return "лет"
    tail = count % 10
    if tail == 1:
        return "год"
    if tail in (2, 3, 4):
        return "года"
    return "лет"


def percent(bps):
    return ("%.1f" % (bps / 100.0)).replace(".", ",")


def add_months(value, months=0, days=0):
    """Прибавляет месяцы по-человечески: 31 марта + 1 месяц = 30 апреля."""
    total = value.month - 1 + int(months or 0)
    year = value.year + total // 12
    month = total % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day) + timedelta(days=int(days or 0))


def today(on=None):
    return as_date(on) if on else as_date(now())


def issue(code, level, message, person=None, details=None):
    return {
        "code": code,
        "title": TITLES[code],
        "level": level,
        "message": message,
        "person": person,
        "details": details or {},
    }


def bundle(db, deal_id):
    """Один заход в базу за всем, что нужно проверкам."""
    deal = db.one("deals", deal_id)
    country = db.one("countries", deal["country_id"]) if deal.get("country_id") else {}
    travellers = []
    for tie in db.all("deal_persons", "deal_id=?", (deal_id,)):
        person = db.one("persons", tie["person_id"])
        travellers.append(
            {
                "tie": tie,
                "person": person,
                "name": fio(person) or "турист без имени",
                "documents": db.all("identity_documents", "person_id=?", (person["id"],)),
            }
        )
    documents = [row for row in db.all("documents", "deal_id=?", (deal_id,)) if not row.get("superseded")]
    return {"deal": deal, "country": country, "travellers": travellers, "documents": documents}


def abroad(ctx):
    if int(ctx["deal"].get("cross_border") or 0):
        return True
    return str(ctx["country"].get("code") or "").upper() not in ("", "RU")


def foreign_passport(traveller):
    """Самый «долгий» загранпаспорт туриста: по нему и считается запас в 6 месяцев."""
    best = None
    for row in traveller["documents"]:
        if "загранпаспорт" not in str(row.get("type") or ""):
            continue
        if best is None or str(row.get("expiry_date") or "") > str(best.get("expiry_date") or ""):
            best = row
    return best


def has_document(traveller, keyword):
    return any(keyword in str(row.get("type") or "").lower() for row in traveller["documents"])


def has_consent(db, ctx, person_id, code):
    """Согласие засчитывается и по реестру согласий, и по выпущенному документу."""
    word = CONSENT_WORDS.get(code, code)
    for row in db.all("consents", "person_id=?", (person_id,)):
        if row.get("revoked_at"):
            continue
        kind = str(row.get("type") or "").lower()
        if kind == code or word in kind:
            return True
    for row in ctx["documents"]:
        if row.get("type") == code and str(row.get("person_id") or "") == str(person_id):
            return True
    return False


def v1(db, ctx, on):
    """V1: загранпаспорт действует ещё N месяцев после возвращения (правило страны)."""
    out = []
    if not abroad(ctx):
        return out
    deal = ctx["deal"]
    back = deal.get("date_to") or deal.get("date_from")
    if not back:
        return [issue("V1", BLOCK, "Не заданы даты тура — срок действия паспортов проверить нельзя.")]
    months = int(ctx["country"].get("passport_months") or 0)
    days = int(ctx["country"].get("passport_days") or 0)
    need = add_months(as_date(back), months, days)
    basis = str(ctx["country"].get("passport_rule_basis") or "правила въезда страны")
    for traveller in ctx["travellers"]:
        who = traveller["person"]["id"]
        passport = foreign_passport(traveller)
        if not passport:
            out.append(
                issue("V1", BLOCK, "У «" + traveller["name"] + "» нет загранпаспорта — тур за рубеж оформить нельзя.", who)
            )
            continue
        expiry = passport.get("expiry_date")
        if not expiry:
            out.append(
                issue(
                    "V1",
                    BLOCK,
                    "У «" + traveller["name"] + "» не заполнена дата окончания загранпаспорта — перенесите её из документа.",
                    who,
                )
            )
            continue
        if as_date(expiry) < need:
            out.append(
                issue(
                    "V1",
                    BLOCK,
                    "Загранпаспорт «"
                    + traveller["name"]
                    + "» действует до "
                    + ru_date(expiry)
                    + ", а нужен минимум до "
                    + ru_date(need)
                    + " ("
                    + str(months)
                    + " мес. после возвращения, "
                    + basis
                    + ").",
                    who,
                    {"expiry_date": str(expiry), "required_until": need.isoformat(), "return_date": str(back)},
                )
            )
    return out


def v2(db, ctx, on):
    """V2: латиница берётся из MRZ; транслитерация по приказу МВД — только сверка."""
    out = []
    for traveller in ctx["travellers"]:
        who = traveller["person"]["id"]
        for row in traveller["documents"]:
            if "загранпаспорт" not in str(row.get("type") or ""):
                continue
            last = latin_normal(row.get("last_name_latin"))
            first = latin_normal(row.get("first_name_latin"))
            raw = str(row.get("mrz_raw") or "")
            fields = {}
            if raw:
                try:
                    fields = parse_mrz(raw)["fields"]
                except AppError as error:
                    out.append(
                        issue(
                            "V2",
                            WARN,
                            "MRZ загранпаспорта «"
                            + traveller["name"]
                            + "» не разобрана ("
                            + str(error.message)
                            + ") — сверьте латиницу с оригиналом вручную.",
                            who,
                        )
                    )
            for key, value, label in (("last_name_latin", last, "фамилия"), ("first_name_latin", first, "имя")):
                from_mrz = latin_normal(fields.get(key)) if fields else ""
                if from_mrz and value and from_mrz != value:
                    out.append(
                        issue(
                            "V2",
                            BLOCK,
                            "У «"
                            + traveller["name"]
                            + "» в анкете "
                            + label
                            + " латиницей «"
                            + value
                            + "», а в MRZ — «"
                            + from_mrz
                            + "». Верным считается MRZ.",
                            who,
                            {"anketa": value, "mrz": from_mrz},
                        )
                    )
                elif from_mrz and not value:
                    out.append(
                        issue(
                            "V2",
                            BLOCK,
                            "У «" + traveller["name"] + "» не заполнена " + label + " латиницей — перенесите из MRZ: «" + from_mrz + "».",
                            who,
                        )
                    )
            if not raw and (not last or not first):
                out.append(
                    issue(
                        "V2",
                        BLOCK,
                        "У «" + traveller["name"] + "» не заполнена латиница загранпаспорта — без неё бронь не оформить.",
                        who,
                    )
                )
            expected = latin_normal(transliterate(str(traveller["person"].get("last_name_ru") or "")))
            if last and expected and last != expected:
                out.append(
                    issue(
                        "V2",
                        WARN,
                        "Фамилия «"
                        + last
                        + "» не совпадает с транслитерацией по приказу МВД № 889 («"
                        + expected
                        + "»). Это нормально, если так напечатано в паспорте, — проверьте оригинал.",
                        who,
                        {"passport": last, "order_889": expected},
                    )
                )
    return out


def v3(db, ctx, on):
    """V3: тариф считается по возрасту на дату вылета, а не «на сегодня»."""
    out = []
    start = ctx["deal"].get("date_from")
    if not start:
        return out
    departure = as_date(start)
    for traveller in ctx["travellers"]:
        who = traveller["person"]["id"]
        birth = traveller["person"].get("birth_date")
        if not birth:
            out.append(issue("V3", BLOCK, "У «" + traveller["name"] + "» не указана дата рождения — тариф не проверить.", who))
            continue
        years = age(birth, departure)
        expect = "infant" if years < INFANT_AGE else ("child" if years < CHILD_AGE else "adult")
        actual = str(traveller["tie"].get("tariff") or "")
        if actual != expect:
            out.append(
                issue(
                    "V3",
                    BLOCK,
                    "На дату вылета "
                    + ru_date(departure)
                    + " «"
                    + traveller["name"]
                    + "» будет "
                    + str(years)
                    + " "
                    + years_word(years)
                    + " — нужен тариф «"
                    + TARIFF_NAMES[expect]
                    + "», а в сделке «"
                    + (TARIFF_NAMES.get(actual) or actual or "не указан")
                    + "».",
                    who,
                    {"expected": expect, "actual": actual, "age": years},
                )
            )
    return out


def v4(db, ctx, on):
    """V4: без согласия на обработку ПД данные туриста обрабатывать нельзя."""
    out = []
    for traveller in ctx["travellers"]:
        who = traveller["person"]["id"]
        if int(traveller["person"].get("processing_blocked") or 0):
            out.append(
                issue("V4", BLOCK, "«" + traveller["name"] + "» отозвал согласие: обработка остановлена, документы не формируются.", who)
            )
            continue
        if not has_consent(db, ctx, who, "consent_pd"):
            out.append(
                issue(
                    "V4",
                    BLOCK,
                    "Нет согласия на обработку персональных данных от «" + traveller["name"] + "» — это требование 152-ФЗ.",
                    who,
                )
            )
    return out


def v5(db, ctx, on):
    """V5: несовершеннолетний без родителей — нотариальное согласие и свидетельство о рождении."""
    out = []
    start = ctx["deal"].get("date_from")
    if not start:
        return out
    departure = as_date(start)
    for traveller in ctx["travellers"]:
        who = traveller["person"]["id"]
        birth = traveller["person"].get("birth_date")
        if not birth or age(birth, departure) >= ADULT_AGE:
            continue
        parents = int(traveller["tie"].get("parents_count") or 0)
        if parents == 0:
            if not traveller["tie"].get("notarized_consent_file_id"):
                out.append(
                    issue(
                        "V5",
                        BLOCK,
                        "«" + traveller["name"] + "» едет без законных представителей — нужно нотариальное согласие на выезд.",
                        who,
                    )
                )
            if not has_document(traveller, "свидетельство о рождении"):
                out.append(
                    issue(
                        "V5",
                        BLOCK,
                        "Для «" + traveller["name"] + "» не приложено свидетельство о рождении — оно подтверждает родство.",
                        who,
                    )
                )
            if not traveller["tie"].get("legal_representative_id"):
                out.append(
                    issue(
                        "V5",
                        WARN,
                        "У «" + traveller["name"] + "» не указан законный представитель — без него согласие не на кого оформить.",
                        who,
                    )
                )
        elif parents == 1:
            out.append(
                issue(
                    "V5",
                    WARN,
                    "«"
                    + traveller["name"]
                    + "» едет с одним из родителей: уточните требования страны — может понадобиться согласие второго родителя.",
                    who,
                )
            )
    return out


def v6(db, ctx, on):
    """V6: дедлайны оплаты клиента и туроператора — самая частая причина аннуляции брони."""
    deal = ctx["deal"]
    out = []
    if str(deal.get("status") or "") in ("Аннулировано", "Завершено"):
        return out
    plans = (
        ("клиента", deal.get("client_deadline"), int(deal.get("paid_client") or 0), int(deal.get("client_price") or 0)),
        ("туроператору", deal.get("operator_deadline"), int(deal.get("paid_operator") or 0), int(deal.get("operator_net") or 0)),
    )
    for who, deadline, paid, total in plans:
        if not deadline or total <= 0 or paid >= total:
            continue
        left = (as_date(deadline) - on).days
        rest = total - paid
        details = {"deadline": str(deadline), "days_left": left, "rest": rest, "escalation_hours": ESCALATION_HOURS}
        if left < 0:
            out.append(
                issue(
                    "V6",
                    BLOCK,
                    "Оплата " + who + " просрочена на " + str(-left) + " дн.: не хватает " + rubles(rest) + " (срок был " + ru_date(deadline) + ").",
                    None,
                    details,
                )
            )
        elif left == 0:
            out.append(
                issue(
                    "V6",
                    WARN,
                    "Сегодня последний день оплаты "
                    + who
                    + ": "
                    + rubles(rest)
                    + ". За "
                    + str(ESCALATION_HOURS)
                    + " ч до конца срока напоминание уйдёт руководителю.",
                    None,
                    details,
                )
            )
        elif left == 1:
            out.append(
                issue(
                    "V6",
                    WARN,
                    "Завтра дедлайн оплаты " + who + ": " + rubles(rest) + " (срок " + ru_date(deadline) + ").",
                    None,
                    details,
                )
            )
    return out


def v7(db, ctx, on):
    """V7: нетто плюс комиссия должны давать цену клиента до копейки."""
    deal = ctx["deal"]
    price = int(deal.get("client_price") or 0)
    net = int(deal.get("operator_net") or 0)
    fee = int(deal.get("commission") or 0)
    if price <= 0:
        return [issue("V7", BLOCK, "Не указана цена для клиента — договор и счёт сформировать нельзя.")]
    out = []
    if net + fee != price:
        out.append(
            issue(
                "V7",
                BLOCK,
                "Цифры не сходятся: нетто "
                + rubles(net)
                + " + комиссия "
                + rubles(fee)
                + " = "
                + rubles(net + fee)
                + ", а цена клиента "
                + rubles(price)
                + ".",
                None,
                {"difference": price - net - fee},
            )
        )
    if net > 0 and fee >= 0:
        bps = int(round(fee * 10000.0 / price))
        if bps < COMMISSION_MIN_BPS or bps > COMMISSION_MAX_BPS:
            out.append(
                issue(
                    "V7",
                    WARN,
                    "Комиссия " + percent(bps) + "\u00a0% выходит за обычные 8–12\u00a0% — проверьте условия туроператора.",
                    None,
                    {"commission_bps": bps},
                )
            )
    return out


def v8(db, ctx, on):
    """V8: один человек дважды в сделке — ошибка, два однофамильца — повод перепроверить."""
    out = []
    by_name = {}
    by_document = {}
    for traveller in ctx["travellers"]:
        person = traveller["person"]
        key = (
            str(person.get("last_name_ru") or "").strip().lower(),
            str(person.get("first_name_ru") or "").strip().lower(),
            str(person.get("patronymic_ru") or "").strip().lower(),
            str(person.get("birth_date") or ""),
        )
        by_name.setdefault(key, []).append(traveller)
        for row in traveller["documents"]:
            document_key = str(row.get("document_key") or "")
            if document_key:
                by_document.setdefault(document_key, set()).add(person["id"])
    for group in by_name.values():
        if len(group) < 2:
            continue
        identifiers = {item["person"]["id"] for item in group}
        name = group[0]["name"]
        if len(identifiers) == 1:
            out.append(
                issue("V8", BLOCK, "«" + name + "» добавлен в сделку дважды — удалите лишнюю строку.", group[0]["person"]["id"])
            )
        else:
            out.append(
                issue(
                    "V8",
                    WARN,
                    "В сделке два туриста с одинаковыми ФИО и датой рождения («"
                    + name
                    + "»). Убедитесь, что это разные люди, и различайте их по номеру паспорта.",
                    group[0]["person"]["id"],
                    {"persons": sorted(identifiers)},
                )
            )
    for document_key, owners in by_document.items():
        if len(owners) > 1:
            out.append(
                issue(
                    "V8",
                    BLOCK,
                    "Один и тот же документ привязан к разным туристам — объедините карточки или исправьте номер.",
                    None,
                    {"document_key": document_key, "persons": sorted(owners)},
                )
            )
    return out


def v9(db, ctx, on):
    """V9: виза туда, где она нужна, с запасом на оформление."""
    country = ctx["country"]
    if not abroad(ctx) or not int(country.get("visa_required") or 0):
        return []
    start = ctx["deal"].get("date_from")
    if not start:
        return []
    departure = as_date(start)
    lead = int(country.get("visa_lead_days") or 0)
    left = (departure - on).days
    out = []
    for traveller in ctx["travellers"]:
        if not int(traveller["person"].get("needs_visa") or 0) or has_document(traveller, "виза"):
            continue
        out.append(
            issue(
                "V9",
                BLOCK if left <= lead else WARN,
                "Для страны «"
                + str(country.get("name") or "")
                + "» нужна виза, а у «"
                + traveller["name"]
                + "» её нет: до вылета "
                + str(left)
                + " дн., оформление занимает "
                + str(lead)
                + " дн.",
                traveller["person"]["id"],
                {"days_left": left, "visa_lead_days": lead},
            )
        )
    return out


def v10(db, ctx, on):
    """V10: обязательные поля ключевых шаблонов заполнены ещё до выпуска PDF."""
    from . import docflow  # лениво: docflow тянет движок PDF, нужный не всегда
    from . import render

    try:
        context = docflow.build_context(db, ctx["deal"]["id"])
    except AppError as error:
        return [issue("V10", BLOCK, str(error.message), None, error.details or {})]
    out = []
    for code in ("contract_tour", "application", "memo"):
        row = docflow.template_row(db, code)
        absent = render.missing_fields(row, context)
        if absent:
            out.append(
                issue(
                    "V10",
                    BLOCK,
                    "Документ «" + str(row.get("name") or code) + "» не сформировать: не заполнены " + ", ".join(absent) + ".",
                    None,
                    {"template": code, "missing": absent},
                )
            )
    return out


def v11(db, ctx, on):
    """V11: для зарубежных туров нужно отдельное согласие на трансграничную передачу."""
    deal = ctx["deal"]
    if not int(deal.get("cross_border") or 0):
        return []
    out = []
    if not (deal.get("recipients") or []):
        out.append(
            issue(
                "V11",
                BLOCK,
                "Тур за рубеж: не перечислены иностранные получатели данных — без списка согласие недействительно.",
            )
        )
    for traveller in ctx["travellers"]:
        who = traveller["person"]["id"]
        if not has_consent(db, ctx, who, "consent_cross_border"):
            out.append(
                issue(
                    "V11",
                    BLOCK,
                    "Нет отдельного согласия на трансграничную передачу данных от «"
                    + traveller["name"]
                    + "» — с 01.09.2025 оно оформляется отдельным документом.",
                    who,
                )
            )
    return out


CHECKS = (
    ("V1", v1),
    ("V2", v2),
    ("V3", v3),
    ("V4", v4),
    ("V5", v5),
    ("V6", v6),
    ("V7", v7),
    ("V8", v8),
    ("V9", v9),
    ("V10", v10),
    ("V11", v11),
)

ALL_CODES = tuple(code for code, _check in CHECKS)

# Какие проверки обязательны для перехода в статус (список статусов — service.STATUSES).
STATUS_GATES = {
    "Собираем документы": ("V4",),
    "Заявка отправлена оператору": ("V1", "V3", "V4", "V8"),
    "Забронировано (ожидает оплаты)": ("V1", "V2", "V3", "V4", "V5", "V7", "V8", "V10", "V11"),
    "Оплачено частично": ("V7",),
    "Оплачено полностью": ("V7",),
    "Документы на вылет выданы": ALL_CODES,
    "В поездке": ("V1", "V6"),
}


def check_deal(db, deal_id, on=None, codes=None):
    """Возвращает все замечания по сделке; codes ограничивает набор проверок."""
    ctx = bundle(db, deal_id)
    day = today(on)
    found = []
    for code, check in CHECKS:
        if codes and code not in codes:
            continue
        found.extend(check(db, ctx, day))
    return found


def blockers(issues):
    return [item for item in issues if item["level"] == BLOCK]


def summary(issues):
    stops = blockers(issues)
    return {
        "blocking": len(stops),
        "warnings": len(issues) - len(stops),
        "codes": sorted({item["code"] for item in issues}),
        "ready": not stops,
    }


def gate(db, deal_id, status, on=None):
    """Проверяет сделку перед переходом в статус и блокирует его при замечаниях."""
    if status not in STATUSES:
        raise AppError("Неизвестный статус сделки: «" + str(status) + "»", 422)
    issues = check_deal(db, deal_id, on, STATUS_GATES.get(status) or ())
    stops = blockers(issues)
    if stops:
        raise AppError(
            "Статус «" + status + "» пока недоступен: " + stops[0]["message"],
            422,
            {"status": status, "issues": stops},
        )
    return issues
