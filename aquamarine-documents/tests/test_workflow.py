"""Автотесты движка статусов и планировщика (спринт S4).

Проверяем три вещи:
- переходы идут только по карте и только при пройденных валидациях;
- автодействия (задачи, пакеты, уведомления) срабатывают ровно один раз;
- планировщик 09:00 собирает пакет на вылет и напоминает о дедлайнах оплаты.
"""

import itertools
import os
import sys
import tempfile
import types
import unittest

SANDBOX = tempfile.TemporaryDirectory()
DB_PATH = os.path.join(SANDBOX.name, "workflow.sqlite3")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("PROVIDER_MODE", "mock")
os.environ.setdefault("DATA_DIR", SANDBOX.name)
os.environ.setdefault("DATABASE_URL", "sqlite:///" + DB_PATH)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend  # noqa: E402
from backend.security import AppError  # noqa: E402

# Движок PDF/DOCX подменяем: здесь важна логика статусов, а не верстка страниц.
try:
    from backend import documents as ENGINE  # noqa: E402
except Exception:  # pragma: no cover - зависит от окружения
    ENGINE = types.ModuleType("backend.documents")
    ENGINE.build_files = lambda text, meta=None: {
        "pdf": b"%PDF-1.4\n%%EOF",
        "docx": b"PK\x03\x04",
        "sha256": "0" * 64,
        "pages": 1,
    }
    ENGINE.merge_pdfs = lambda items: b"%PDF-1.4\n%%EOF"
    ENGINE.page_count = lambda raw: 1
    ENGINE.digest = lambda raw: "0" * 64
    ENGINE.to_pdf = lambda text, meta=None: b"%PDF-1.4\n%%EOF"
    ENGINE.to_docx = lambda text, meta=None: b"PK\x03\x04"
    ENGINE.font_paths = lambda: ()
sys.modules["backend.documents"] = ENGINE
backend.documents = ENGINE

from backend import docflow, ocr, rules, workflow  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.db import now  # noqa: E402
from backend.service import Service  # noqa: E402

DEPARTURE = "2026-07-10"
RETURN = "2026-07-17"
COUNTER = itertools.count(1)


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db = Database("sqlite:///" + DB_PATH)
        db.migrate()
        cls.db = db
        cls.user = db.insert(
            "users",
            {
                "full_name": "Ковалёва Ирина Сергеевна",
                "email": "workflow@aquamarine.test",
                "phone": "+7 900 000-77-88",
                "role": "admin",
                "active": 1,
                "two_fa_enabled": 0,
                "password_hash": "x",
                "failed_attempts": 0,
                "last_totp_step": 0,
            },
        )
        cls.manager = db.insert(
            "users",
            {
                "full_name": "Сергеев Павел Андреевич",
                "email": "manager@aquamarine.test",
                "phone": "+7 900 000-55-44",
                "role": "manager",
                "active": 1,
                "two_fa_enabled": 0,
                "password_hash": "x",
                "failed_attempts": 0,
                "last_totp_step": 0,
            },
        )
        cls.service = Service(db, cls.user, "127.0.0.1", "tests")
        cls.junior = Service(db, cls.manager, "127.0.0.1", "tests")
        docflow.sync_templates(db, cls.user["id"])
        cls.country = db.insert(
            "countries",
            {
                "code": "TR",
                "name": "Турция",
                "passport_months": 6,
                "passport_days": 0,
                "passport_rule_basis": "Правила въезда Республики Турция",
                "visa_required": 0,
                "visa_lead_days": 0,
                "memo": "Загранпаспорт должен быть действителен 6 месяцев после возвращения.",
            },
        )
        cls.operator = db.insert(
            "operators",
            {
                "full_name": "ООО «ТурОператор Демо»",
                "short_name": "ТО Демо",
                "inn": "7712345678",
                "ogrn": "1027700000000",
                "address": "115035, г. Москва, ул. Примерная, д. 1",
                "registry_number": "РТО 012345",
                "guarantee_type": "фонд персональной ответственности",
                "guarantee_amount": 5000000000,
                "guarantor": "Ассоциация «Турпомощь»",
                "guarantee_number": "ФО-2026-777",
                "guarantee_from": "2026-01-01",
                "guarantee_to": "2026-12-31",
                "guarantee_address": "101000, г. Москва, ул. Гарантийная, д. 5",
                "contacts": {"phone": "+7 495 000-00-00", "emergency": "+7 800 100-20-30", "email": "agents@to-demo.test"},
                "portal_url": "https://agents.to-demo.test",
                "default_commission_bps": 1000,
                "payment_hours": 24,
                "agent_agreement": "АГ-2026-15 от 12.01.2026",
            },
        )
        cls.client = db.insert(
            "clients",
            {
                "type": "физлицо",
                "full_name": "Семёнов Илья Петрович",
                "phone": "+7 916 555-33-11",
                "email": "semenov@example.test",
                "messenger": "telegram",
                "source": "рекомендация",
                "registration_address": "119021, г. Москва, ул. Жилая, д. 4, кв. 7",
                "manager_id": cls.user["id"],
                "birth_date": "1990-02-03",
                "status": "активный",
            },
        )

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        SANDBOX.cleanup()

    # --- фикстуры сделки ------------------------------------------------

    @classmethod
    def person(cls, last, first, patronymic, birth, **extra):
        data = {
            "client_id": cls.client["id"],
            "last_name_ru": last,
            "first_name_ru": first,
            "patronymic_ru": patronymic,
            "gender": "м",
            "birth_date": birth,
            "citizenship": "РФ",
            "birth_place": "г. Москва",
            "phone": "+7 916 555-33-11",
            "email": "semenov@example.test",
            "relationship": "основной турист",
            "needs_visa": 0,
            "processing_blocked": 0,
            "legal_hold": 0,
        }
        data.update(extra)
        return cls.db.insert("persons", data)

    @classmethod
    def passport(cls, person, expiry="2031-05-10"):
        serial = next(COUNTER)
        number = "7" + str(1000000 + serial)
        raw = ocr.build_mrz(person["last_name_ru"], person["first_name_ru"], number, str(person["birth_date"]), expiry)
        return cls.db.insert(
            "identity_documents",
            {
                "person_id": person["id"],
                "type": "загранпаспорт РФ",
                "series": "75",
                "number": number,
                "issued_by": "УМВД России",
                "issue_date": "2021-05-11",
                "expiry_date": expiry,
                "last_name_latin": ocr.transliterate(person["last_name_ru"]),
                "first_name_latin": ocr.transliterate(person["first_name_ru"]),
                "issuing_country": "RUS",
                "mrz_raw": raw,
                "verification_status": "подтверждён",
                "is_primary": 1,
                "document_key": "foreign:75:" + number,
                "manual_mrz_review": 0,
            },
        )

    @classmethod
    def consent(cls, person, code, deal=None):
        return cls.db.insert(
            "consents",
            {
                "person_id": person["id"],
                "type": code,
                "received_at": now(),
                "method": "ПЭП: код из СМС",
                "text_version": "1",
                "deal_id": deal["id"] if deal else None,
                "purposes": ["бронирование тура"],
                "recipients": ["ООО «ТурОператор Демо»"],
                "ip": "127.0.0.1",
                "user_agent": "tests",
            },
        )

    def deal(self, **overrides):
        data = {
            "number": "АКВ-2026-8" + str(next(COUNTER)).zfill(4),
            "client_id": self.client["id"],
            "manager_id": self.user["id"],
            "status": "Забронировано (ожидает оплаты)",
            "country_id": self.country["id"],
            "city": "Анталия",
            "date_from": DEPARTURE,
            "date_to": RETURN,
            "nights": 7,
            "hotel": "Demo Beach Resort 5*",
            "room": "Standard Land View",
            "meal": "всё включено",
            "adults": 1,
            "children": 0,
            "infants": 0,
            "tour_type": "пакетный тур",
            "booking_number": "TO-778899",
            "operator_id": self.operator["id"],
            "currency": "RUB",
            "client_price": 24500000,
            "operator_net": 22050000,
            "commission": 2450000,
            "paid_client": 24500000,
            "paid_operator": 22050000,
            "client_deadline": "2026-06-20",
            "operator_deadline": "2026-06-25",
            "cross_border": 1,
            "recipients": ["Demo Beach Resort 5*, Турция", "ООО «ТурОператор Демо»"],
            "flight_there": {"number": "TK-2026", "departure": "2026-07-10T08:40", "arrival": "2026-07-10T12:55"},
            "flight_back": {"number": "TK-2027", "departure": "2026-07-17T13:40", "arrival": "2026-07-17T18:05"},
            "host_contacts": "Принимающая сторона: Demo DMC, +90 242 000-00-00",
            "services": "Авиаперелёт, трансфер, проживание, питание «всё включено», медицинская страховка",
            "program": "Пляжный отдых",
            "route": "Москва — Анталия — Москва",
            "guide": "не требуется",
            "revision": 1,
        }
        data.update(overrides)
        return self.db.insert("deals", data)

    def travel(self, deal, person, tariff="adult", **extra):
        data = {
            "deal_id": deal["id"],
            "person_id": person["id"],
            "role": "турист",
            "placement": "основное место",
            "tariff": tariff,
            "parents_count": 2,
        }
        data.update(extra)
        return self.db.insert("deal_persons", data)

    def ready_deal(self, status, **overrides):
        """Сделка, которая проходит все блокирующие проверки V1–V11."""
        deal = self.deal(status=status, **overrides)
        person = self.person("Семёнов", "Илья", "Петрович", "1990-02-03")
        self.passport(person)
        self.travel(deal, person)
        for code in ("consent_pd", "consent_transfer", "consent_cross_border"):
            self.consent(person, code, deal)
        return deal, person

    # --- чтение побочных эффектов --------------------------------------

    def tasks_of(self, deal, kind=None):
        rows = self.db.all("tasks", "deal_id=?", (deal["id"],))
        return [row for row in rows if kind is None or row.get("type") == kind]

    def notes_of(self, deal, template=None):
        rows = self.db.all("notifications", "deal_id=?", (deal["id"],))
        return [row for row in rows if template is None or row.get("template") == template]

    def packages_of(self, deal, kind=None):
        rows = self.db.all("packages", "deal_id=?", (deal["id"],))
        return [row for row in rows if kind is None or row.get("type") == kind]

    def status_of(self, deal):
        return self.db.one("deals", deal["id"])["status"]

    # --- карта переходов -------------------------------------------------

    def test_transition_creates_task_once(self):
        deal, _ = self.ready_deal("Согласован вариант")
        result = workflow.transition(self.service, deal["id"], "Собираем документы", on="2026-06-15")
        self.assertTrue(result["changed"])
        self.assertEqual(self.status_of(deal), "Собираем документы")
        self.assertEqual(len(self.tasks_of(deal, "documents")), 1)
        again = workflow.transition(self.service, deal["id"], "Собираем документы", on="2026-06-15")
        self.assertFalse(again["changed"])
        self.assertEqual(len(self.tasks_of(deal, "documents")), 1)

    def test_transition_rejects_jump_over_statuses(self):
        deal = self.deal(status="Новый лид")
        with self.assertRaises(AppError) as ctx:
            workflow.transition(self.service, deal["id"], "Оплачено полностью", on="2026-06-15")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("Подбор тура", ctx.exception.details["allowed"])
        self.assertEqual(self.status_of(deal), "Новый лид")

    def test_unknown_status_is_rejected(self):
        deal = self.deal(status="Новый лид")
        with self.assertRaises(AppError) as ctx:
            workflow.transition(self.service, deal["id"], "Оплачено частично (наличные)", on="2026-06-15")
        self.assertEqual(ctx.exception.status, 422)

    def test_force_needs_senior_role_and_reason(self):
        deal, person = self.ready_deal("Новый лид", manager_id=self.manager["id"])
        with self.assertRaises(AppError):
            workflow.transition(
                self.junior, deal["id"], "Собираем документы",
                on="2026-06-15", force=True, reason="клиент подтвердил по телефону",
            )
        with self.assertRaises(AppError):
            workflow.transition(
                self.service, deal["id"], "Собираем документы",
                on="2026-06-15", force=True, reason="надо",
            )
        self.assertEqual(self.status_of(deal), "Новый лид")
        result = workflow.transition(
            self.service, deal["id"], "Собираем документы",
            on="2026-06-15", force=True, reason="Клиент уже согласовал вариант по телефону",
        )
        self.assertTrue(result["changed"])
        self.assertEqual(self.status_of(deal), "Собираем документы")
        trail = [row for row in self.db.all("audit_log", "entity_id=?", (deal["id"],)) if row["action"] == "status"]
        self.assertTrue(any((row.get("details") or {}).get("forced") for row in trail))

    def test_validations_block_booking(self):
        deal, _ = self.ready_deal("Заявка отправлена оператору", client_price=20000000)
        with self.assertRaises(AppError) as ctx:
            workflow.transition(self.service, deal["id"], "Забронировано (ожидает оплаты)", on="2026-06-15")
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("Забронировано", ctx.exception.message)
        self.assertEqual(self.status_of(deal), "Заявка отправлена оператору")

    # --- автодействия при входе в статус -----------------------------

    def test_booking_builds_package_and_notifies_client(self):
        deal, _ = self.ready_deal("Заявка отправлена оператору", paid_client=0, paid_operator=0)
        result = workflow.transition(self.service, deal["id"], "Забронировано (ожидает оплаты)", on="2026-06-15")
        self.assertTrue(result["changed"])
        self.assertIn("Оплачено частично", result["next"])
        self.assertEqual(len(self.packages_of(deal, "on_contract")), 1)
        self.assertEqual(len(self.tasks_of(deal, "payment")), 1)
        sms = self.notes_of(deal, "booked")
        self.assertEqual(len(sms), 1)
        self.assertEqual(sms[0]["channel"], "sms")
        docs = self.db.all("documents", "deal_id=?", (deal["id"],))
        self.assertTrue(any(row.get("type") == "contract_tour" for row in docs))

    def test_cancellation_revokes_portal_links(self):
        deal, _ = self.ready_deal("Забронировано (ожидает оплаты)", paid_client=0, paid_operator=0)
        docflow.build_package(self.service, deal["id"], "on_contract")
        package = self.packages_of(deal, "on_contract")[0]
        link, address = docflow.issue_link(self.service, package["id"])
        self.assertIn("/portal/", address)
        result = workflow.transition(self.service, deal["id"], "Аннулировано", on="2026-06-15", reason="Клиент отказался от тура")
        self.assertTrue(result["changed"])
        self.assertTrue(self.db.one("portal_links", link["id"])["revoked_at"])
        self.assertEqual(len(self.tasks_of(deal, "cancellation")), 1)
        self.assertEqual(len(self.notes_of(deal, "cancelled")), 1)

    def test_helpers_are_idempotent(self):
        deal, _ = self.ready_deal("Собираем документы")
        for _ in range(2):
            workflow.ensure_task(self.service, deal, "documents", "Проверить паспорта", key="manual|" + deal["id"])
            workflow.notify(self.service, deal, "booked", channel="sms", to="client", key="manual|" + deal["id"])
        self.assertEqual(len(self.tasks_of(deal, "documents")), 1)
        self.assertEqual(len(self.notes_of(deal, "booked")), 1)

    # --- планировщик 09:00 -----------------------------------------------

    def test_daily_run_builds_departure_package(self):
        deal, _ = self.ready_deal("Оплачено полностью")
        report = workflow.daily_run(self.service, on="2026-07-07", name="test-departure")
        self.assertFalse(report["skipped"])
        self.assertGreaterEqual(report["checked"], 1)
        packages = self.packages_of(deal, "before_departure")
        self.assertEqual(len(packages), 1)
        # Билетов и ваучера от туроператора нет, значит менеджер должен получить задачу.
        self.assertTrue(self.tasks_of(deal, "package") or self.notes_of(deal, "departure_package"))
        repeat = workflow.daily_run(self.service, on="2026-07-07", name="test-departure")
        self.assertTrue(repeat["skipped"])
        self.assertEqual(len(self.packages_of(deal, "before_departure")), 1)

    def test_daily_run_is_idempotent(self):
        deal, _ = self.ready_deal("Собираем документы")
        first = workflow.daily_run(self.service, on="2026-06-01", name="test-idle")
        tasks = len(self.db.all("tasks"))
        notes = len(self.db.all("notifications"))
        second = workflow.daily_run(self.service, on="2026-06-01", name="test-idle")
        self.assertFalse(first["skipped"])
        self.assertTrue(second["skipped"])
        self.assertEqual(len(self.db.all("tasks")), tasks)
        self.assertEqual(len(self.db.all("notifications")), notes)
        self.assertEqual(self.status_of(deal), "Собираем документы")

    def test_daily_run_reminds_about_payments(self):
        soon, _ = self.ready_deal(
            "Забронировано (ожидает оплаты)",
            paid_client=0, paid_operator=0,
            client_deadline="2026-06-20", operator_deadline="2026-06-25",
            date_from="2026-09-10", date_to="2026-09-17",
        )
        late, _ = self.ready_deal(
            "Забронировано (ожидает оплаты)",
            paid_client=0, paid_operator=0,
            client_deadline="2026-06-10", operator_deadline="2026-06-12",
            date_from="2026-09-12", date_to="2026-09-19",
        )
        report = workflow.daily_run(self.service, on="2026-06-19", name="test-money")
        self.assertFalse(report["skipped"])
        self.assertEqual(len(self.notes_of(soon, "payment_due_client")), 1)
        self.assertEqual(len(self.notes_of(late, "payment_overdue_client")), 1)
        self.assertEqual(len(self.notes_of(late, "payment_overdue_operator")), 1)
        self.assertTrue(self.tasks_of(late, "payment"))

    def test_rule_tasks_for_expiring_passport(self):
        deal = self.deal(status="Собираем документы", date_from="2026-08-20", date_to="2026-08-27")
        person = self.person("Коротков", "Максим", "Игоревич", "1988-04-12")
        self.passport(person, "2026-09-01")
        self.travel(deal, person)
        self.consent(person, "consent_pd", deal)
        workflow.daily_run(self.service, on="2026-06-20", name="test-rules")
        tasks = self.tasks_of(deal, "rule:V1")
        self.assertEqual(len(tasks), 1)
        self.assertIn("паспорт", tasks[0]["title"].lower())
        workflow.daily_run(self.service, on="2026-06-21", name="test-rules")
        self.assertEqual(len(self.tasks_of(deal, "rule:V1")), 1)


if __name__ == "__main__":
    unittest.main()
