"""Движок статусов сделки: разрешённые переходы, автодействия, задачи и планировщик 09:00.

Свойства:
- статус меняется только через этот модуль (в сервисе поле status закрыто на запись);
- переход блокируется, если не пройдены валидации из rules.STATUS_GATES (V1–V11);
- любое автодействие идемпотентно: повторный запуск не создаёт дублей задач и уведомлений;
- каждый переход и каждый прогон планировщика попадают в аудит-лог.
"""

from datetime import datetime, timedelta, timezone

from . import config, docflow, rules
from .db import now
from .security import AppError, blind
from .service import STATUSES, as_date

MSK = timezone(timedelta(hours=3))
RUN_HOUR = 9  # планировщик запускается в 09:00 по Москве

(
    NEW_LEAD,
    PICKING,
    SENT,
    AGREED,
    COLLECTING,
    APPLICATION,
    BOOKED,
    PAID_PART,
    PAID_FULL,
    ISSUED,
    TRAVELLING,
    DONE,
    CANCELLED,
) = STATUSES

# Куда можно перейти из каждого статуса. Аннуляция доступна почти везде — так живёт турагентство.
TRANSITIONS = {
    NEW_LEAD: (PICKING, CANCELLED),
    PICKING: (SENT, CANCELLED),
    SENT: (AGREED, PICKING, CANCELLED),
    AGREED: (COLLECTING, PICKING, CANCELLED),
    COLLECTING: (APPLICATION, AGREED, CANCELLED),
    APPLICATION: (BOOKED, COLLECTING, CANCELLED),
    BOOKED: (PAID_PART, PAID_FULL, CANCELLED),
    PAID_PART: (PAID_FULL, CANCELLED),
    PAID_FULL: (ISSUED, CANCELLED),
    ISSUED: (TRAVELLING, CANCELLED),
    TRAVELLING: (DONE,),
    DONE: (),
    CANCELLED: (),
}

# Кому разрешён нетиповый переход в обход карты (с обязательным основанием в аудите).
OVERRIDE_ROLES = ("admin", "senior")


def statuses_after(status):
    """Список доступных следующих статусов — им кормится карточка сделки и канбан."""
    if status not in TRANSITIONS:
        raise AppError("Неизвестный статус сделки: «" + str(status) + "»", 422)
    return list(TRANSITIONS[status])


def ensure_task(service, deal, kind, title, due_hours=24, key=None, assignee=None):
    """Ставит задачу один раз: повторный вызов возвращает уже существующую запись."""
    db = service.db
    token = key or "|".join([kind, str(deal["id"])])
    stamp = blind(token)
    existing = db.find("tasks", "idempotency_key=?", (stamp,))
    if existing is not None:
        return existing
    row = db.insert(
        "tasks",
        {
            "deal_id": deal["id"],
            "type": kind,
            "title": title,
            "due_at": (datetime.now(timezone.utc) + timedelta(hours=due_hours)).isoformat(timespec="seconds"),
            "assignee_id": assignee or deal.get("manager_id"),
            "status": "open",
            "automatic": 1,
            "idempotency_key": stamp,
        },
        service.actor,
    )
    service.audit("create", "tasks", row["id"], {"type": kind, "auto": True})
    return row


def notify(service, deal, template, channel="in-app", to="manager", payload=None, key=None):
    """Кладёт уведомление в очередь. Фактическая отправка — задача outbox (спринт S6)."""
    db = service.db
    stamp = blind(key or "|".join([template, str(deal["id"]), to]))
    existing = db.find("notifications", "idempotency_key=?", (stamp,))
    if existing is not None:
        return existing
    client = db.one("clients", deal["client_id"]) if deal.get("client_id") else None
    destination = ""
    if to == "client" and client:
        destination = client.get("phone") if channel == "sms" else (client.get("email") or client.get("phone") or "")
    elif deal.get("manager_id"):
        manager = db.one("users", deal["manager_id"])
        destination = manager.get("email") or manager.get("phone") or ""
    row = db.insert(
        "notifications",
        {
            "user_id": deal.get("manager_id") if to != "client" else None,
            "client_id": deal.get("client_id") if to == "client" else None,
            "deal_id": deal["id"],
            "channel": channel,
            "template": template,
            "status": "в очереди",
            "date": now(),
            "payload": {"deal": deal.get("number"), **(payload or {})},
            "destination": destination,
            "idempotency_key": stamp,
            "attempts": 0,
        },
        service.actor,
    )
    service.audit("create", "notifications", row["id"], {"template": template, "channel": channel, "to": to})
    return row


# --- автодействия при входе в статус -------------------------------------


def _collecting(service, deal, day):
    ensure_task(service, deal, "documents", "Собрать паспорта, согласия и данные туристов", 48)
    return ["поставлена задача на сбор документов"]


def _application(service, deal, day):
    ensure_task(service, deal, "booking", "Проверить подтверждение брони у туроператора", 24)
    notify(service, deal, "application_sent", "in-app", "manager", {"operator": deal.get("booking_number")})
    return ["задача на контроль брони", "уведомление менеджеру"]


def _booked(service, deal, day):
    notes = []
    try:
        package = docflow.build_package(service, deal["id"], "on_contract")
        notes.append("пакет при заключении: " + str(package.get("status")))
    except AppError as error:
        ensure_task(service, deal, "package", "Собрать пакет при заключении: " + error.message, 8)
        notes.append("пакет не собран: " + error.message)
    if deal.get("client_deadline"):
        ensure_task(
            service,
            deal,
            "payment",
            "Проконтролировать оплату клиента до " + rules.ru_date(deal["client_deadline"]),
            24,
        )
        notes.append("задача на контроль оплаты")
    notify(service, deal, "booked", "sms", "client", {"hotel": deal.get("hotel"), "city": deal.get("city")})
    return notes


def _paid_part(service, deal, day):
    rest = (deal.get("client_price") or 0) - (deal.get("paid_client") or 0)
    ensure_task(
        service,
        deal,
        "payment",
        "Забрать остаток оплаты: " + rules.rubles(max(rest, 0)),
        24,
        key="payment-rest|" + str(deal["id"]),
    )
    return ["задача на остаток оплаты"]


def _paid_full(service, deal, day):
    ensure_task(service, deal, "operator_docs", "Запросить у туроператора билеты, ваучер и страховку", 24)
    notify(service, deal, "paid_full", "in-app", "manager")
    return ["задача на документы туроператора"]


def _issued(service, deal, day):
    notify(service, deal, "documents_issued", "sms", "client", {"departure": rules.ru_date(deal.get("date_from"))})
    ensure_task(service, deal, "handover", "Подтвердить, что турист получил документы на вылет", 24)
    return ["уведомление клиенту о выдаче", "задача на подтверждение выдачи"]


def _travelling(service, deal, day):
    notify(service, deal, "trip_started", "sms", "client", {"host": deal.get("host_contacts")})
    return ["уведомление с контактами принимающей стороны"]


def _done(service, deal, day):
    notes = []
    try:
        package = docflow.build_package(service, deal["id"], "accounting")
        notes.append("пакет для бухгалтерии: " + str(package.get("status")))
    except AppError as error:
        ensure_task(service, deal, "package", "Собрать пакет для бухгалтерии: " + error.message, 24)
        notes.append("пакет не собран: " + error.message)
    ensure_task(service, deal, "closing", "Подписать акт и закрыть сделку в 1С", 72)
    return notes + ["задача на акт и закрытие"]


def _cancelled(service, deal, day):
    ensure_task(service, deal, "cancellation", "Оформить аннуляцию: фактические расходы и возврат остатка", 24)
    notify(service, deal, "cancelled", "in-app", "manager")
    return ["задача на расчёт аннуляции", "ссылки на документы отозваны"]


ENTRY_ACTIONS = {
    COLLECTING: _collecting,
    APPLICATION: _application,
    BOOKED: _booked,
    PAID_PART: _paid_part,
    PAID_FULL: _paid_full,
    ISSUED: _issued,
    TRAVELLING: _travelling,
    DONE: _done,
    CANCELLED: _cancelled,
}


# --- переход по статусам ---------------------------------------------


def transition(service, deal_id, status, on=None, force=False, reason=""):
    """Меняет статус сделки: карта переходов → валидации → запись → автодействия."""
    db = service.db
    service.require("admin", "senior", "manager")
    deal = service.get("deals", deal_id, True)
    if status not in TRANSITIONS:
        raise AppError("Неизвестный статус сделки: «" + str(status) + "»", 422)
    current = deal["status"]
    if status == current:
        return {"deal": deal.get("number"), "status": status, "changed": False, "warnings": [], "actions": []}
    allowed = TRANSITIONS[current]
    if status not in allowed:
        if not force:
            hint = ", ".join(allowed) if allowed else "переходы недоступны"
            raise AppError(
                "Из статуса «" + current + "» нельзя перейти в «" + status + "». Доступно: " + hint,
                409,
                {"from": current, "to": status, "allowed": list(allowed)},
            )
        service.require(*OVERRIDE_ROLES)
        if len(str(reason).strip()) < 10:
            raise AppError("Укажите основание нетипового перехода (не короче 10 символов)")
    issues = rules.gate(db, deal_id, status, on=on)
    patch = {"status": status, "last_contact_at": now()}
    if status == DONE:
        patch["completed_at"] = now()
    db.update("deals", deal_id, patch)
    warnings = [item for item in issues if item["level"] == rules.WARN]
    service.audit(
        "status",
        "deals",
        deal_id,
        {
            "from": current,
            "to": status,
            "forced": bool(force),
            "reason": str(reason).strip(),
            "warnings": [item["code"] for item in warnings],
        },
    )
    if status == CANCELLED:
        service.bump_deal(deal_id)  # отзывает ссылки и аннулирует черновики договоров
    fresh = db.one("deals", deal_id)
    action = ENTRY_ACTIONS.get(status)
    done = action(service, fresh, on) if action else []
    return {
        "deal": fresh.get("number"),
        "status": status,
        "changed": True,
        "warnings": warnings,
        "actions": done,
        "next": statuses_after(status),
    }


# --- планировщик 09:00 по Москве --------------------------------------

# Какие проверки имеет смысл гонять каждое утро: они зависят от даты, а не от действий менеджера.
WATCH_CODES = ("V1", "V3", "V4", "V5", "V9", "V11")
PACKAGE_STATUSES = (BOOKED, PAID_PART, PAID_FULL)
WATCH_WINDOW_DAYS = 90


def moscow_day(on=None):
    """Рабочая дата агентства — всегда по Europe/Moscow."""
    return on or datetime.now(MSK).date().isoformat()


def days_left(day, value):
    """Сколько суток осталось до value от day (отрицательное число — уже прошло)."""
    if not value:
        return None
    try:
        return (as_date(str(value)[:10]) - as_date(day)).days
    except Exception:
        return None


def departure_package(service, deal, day):
    """За config.PACKAGE_DAYS суток до вылета собирает пакет и проверяет комплектность."""
    left = days_left(day, deal.get("date_from"))
    if left != config.PACKAGE_DAYS or deal.get("status") not in PACKAGE_STATUSES:
        return []
    tag = "departure|" + str(deal["id"])
    try:
        package = docflow.build_package(service, deal["id"], "before_departure")
    except AppError as error:
        ensure_task(service, deal, "package", "Собрать пакет на вылет: " + error.message, 8, key=tag)
        return [{"deal": deal.get("number"), "package": None, "problem": error.message}]
    gaps = package.get("missing") or []
    if gaps:
        names = ", ".join(str(item.get("template")) for item in gaps)
        ensure_task(
            service,
            deal,
            "package",
            "Пакет на вылет неполный, не хватает: " + names,
            8,
            key=tag + "|missing",
        )
        return [{"deal": deal.get("number"), "package": package["id"], "status": package.get("status"), "missing": names}]
    notify(
        service,
        deal,
        "departure_package",
        "sms",
        "client",
        {"departure": rules.ru_date(deal.get("date_from")), "hotel": deal.get("hotel")},
        key=tag,
    )
    return [{"deal": deal.get("number"), "package": package["id"], "status": package.get("status")}]


def rule_tasks(service, deal, day):
    """Проверки с привязкой к дате: паспорта, тарифы, согласия, визы."""
    left = days_left(day, deal.get("date_from"))
    if left is None or left < 0 or left > WATCH_WINDOW_DAYS:
        return []
    found = []
    for item in rules.check_deal(service.db, deal["id"], on=day, codes=WATCH_CODES):
        if item["level"] != rules.BLOCK:
            continue
        ensure_task(
            service,
            deal,
            "rule:" + item["code"],
            item["code"] + ". " + item["message"],
            24,
            key="rule|" + str(deal["id"]) + "|" + item["code"],
        )
        found.append({"deal": deal.get("number"), "code": item["code"], "message": item["message"]})
    return found


def payment_reminders(service, deal, day):
    """Напоминание за сутки и эскалация руководителю по просрочке (V6)."""
    out = []
    plan = (
        ("client", "client_price", "paid_client", "client_deadline", "client"),
        ("operator", "operator_net", "paid_operator", "operator_deadline", "manager"),
    )
    for side, total_key, paid_key, deadline_key, addressee in plan:
        rest = (deal.get(total_key) or 0) - (deal.get(paid_key) or 0)
        left = days_left(day, deal.get(deadline_key))
        if rest <= 0 or left is None:
            continue
        stamp = "|".join([side, str(deal["id"]), str(deal.get(deadline_key))])
        payload = {"rest": rules.rubles(rest), "deadline": rules.ru_date(deal.get(deadline_key))}
        if left < 0:
            notify(service, deal, "payment_overdue_" + side, "in-app", "manager", payload, key="late|" + stamp)
            ensure_task(
                service,
                deal,
                "payment",
                ("Оплата клиента" if side == "client" else "Оплата туроператору")
                + " просрочена на "
                + str(-left)
                + " дн.: "
                + rules.rubles(rest),
                rules.ESCALATION_HOURS,
                key="late-task|" + stamp,
            )
            out.append({"deal": deal.get("number"), "side": side, "state": "просрочено", "days": -left})
        elif left <= 1:
            notify(service, deal, "payment_due_" + side, "sms" if addressee == "client" else "in-app", addressee, payload, key="due|" + stamp)
            out.append({"deal": deal.get("number"), "side": side, "state": "срок близок", "days": left})
    return out


def daily_run(service, on=None, name="daily"):
    """Прогон планировщика. Один раз в сутки: повторный вызов вернёт skipped."""
    db = service.db
    service.require("admin", "senior")
    day = moscow_day(on)
    previous = db.find("job_runs", "name=? AND run_date=?", (name, day))
    if previous is not None and previous.get("finished_at"):
        return {"job": name, "date": day, "skipped": True}
    run = previous or db.insert("job_runs", {"name": name, "run_date": day}, service.actor)
    report = {"job": name, "date": day, "skipped": False, "checked": 0, "packages": [], "tasks": [], "reminders": []}
    for deal in db.all("deals"):
        if deal.get("status") in (DONE, CANCELLED):
            continue
        report["checked"] += 1
        report["packages"] += departure_package(service, deal, day)
        report["tasks"] += rule_tasks(service, deal, day)
        report["reminders"] += payment_reminders(service, deal, day)
    db.update("job_runs", run["id"], {"finished_at": now()})
    service.audit(
        "job_run",
        "job_runs",
        run["id"],
        {
            "name": name,
            "date": day,
            "checked": report["checked"],
            "packages": len(report["packages"]),
            "tasks": len(report["tasks"]),
            "reminders": len(report["reminders"]),
        },
    )
    return report
