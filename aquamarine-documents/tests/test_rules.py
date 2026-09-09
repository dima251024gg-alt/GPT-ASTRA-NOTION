"""Автотесты валидаций сделки V1–V11 (спринт S4).

Модуль намеренно держит собственную базу (отдельный файл sqlite) и собственный
набор данных: проверки удобнее гонять на «испорченных» сделках, а тесты
документооборота в tests/test_docflow.py должны оставаться независимыми.

Каждый тест собирает свою сделку и своих туристов — так видно, что именно ловит проверка,
и тесты не зависят от порядка запуска.
"""

import itertools
import os
import sys
import tempfile
import types
import unittest

SANDBOX = tempfile.TemporaryDirectory()
DB_PATH = os.path.join(SANDBOX.name, "rules.sqlite3")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("PROVIDER_MODE", "mock")
os.environ.setdefault("DATA_DIR", SANDBOX.name)
os.environ.setdefault("DATABASE_URL", "sqlite:///" + DB_PATH)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend  # noqa: E402
from backend.security import AppError  # noqa: E402

# Движок PDF/DOCX нужен только для импорта docflow (его дёргает V10) — подставляем заглушку.
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

from backend import docflow, ocr, rules  # noqa: E402
from backend.db import Database, now  # noqa: E402
from backend.service import Service  # noqa: E402

DEPARTURE = "2026-07-10"
RETURN = "2026-07-17"
TODAY = "2026-06-01"
COUNTER = itertools.count(1)


class RulesTest(unittest.TestCase):
    """Семья в Турцию и поездка в визовую страну — типовые случаи турагентства."""

    @classmethod
    def setUpClass(cls):
        db = Database("sqlite:///" + DB_PATH)
        db.migrate()
        cls.db = db
        cls.user = db.insert(
            "users",
            {
                "full_name": "Смирнова Ольга Ивановна",
                "email": "rules@aquamarine.test",
                "phone": "+7 900 000-11-22",
                "role": "admin",
                "active": 1,
                "two_fa_enabled": 0,
                "password_hash": "x",
                "failed_attempts": 0,
                "last_totp_step": 0,
            },
        )
        cls.service = Service(db, cls.user, "127.0.0.1", "tests")
        docflow.sync_templates(db, cls.user["id"])
        cls.turkey = db.insert(
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
        cls.india = db.insert(
            "countries",
            {
                "code": "IN",
                "name": "Индия",
                "passport_months": 6,
                "passport_days": 0,
                "passport_rule_basis": "Правила выдачи электронной визы Индии",
                "visa_required": 1,
                "visa_lead_days": 20,
                "memo": "Электронная виза оформляется до 20 дней.",
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

    # --- вспомогательные сборщики данных -------------------------------------

    @classmethod
    def file_row(cls, name="notarized-consent.pdf"):
        """Запись в files: проверкам важен факт приложенного файла, а не его содержимое."""
        return cls.db.insert(
            "files",
            {
                "name": name,
                "mime": "application/pdf",
                "size": 1024,
                "storage_path": "tests/" + name,
                "encrypted": 1,
                "uploaded_by": cls.user["id"],
                "entity_type": "deal_persons",
                "entity_id": "",
                "sha256": "0" * 64,
            },
        )

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
    def passport(cls, person, expiry, latin=None, mrz_names=None, with_mrz=True):
        """Загранпаспорт с настоящей MRZ TD3 (считается контрольными цифрами из ocr.build_mrz)."""
        serial = next(COUNTER)
        number = "7" + str(1000000 + serial)
        mrz_last, mrz_first = mrz_names or (person["last_name_ru"], person["first_name_ru"])
        raw = ocr.build_mrz(mrz_last, mrz_first, number, str(person["birth_date"]), expiry) if with_mrz else ""
        last_latin, first_latin = latin or (ocr.transliterate(mrz_last), ocr.transliterate(mrz_first))
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
                "last_name_latin": last_latin,
                "first_name_latin": first_latin,
                "issuing_country": "RUS",
                "mrz_raw": raw,
                "verification_status": "подтверждён",
                "is_primary": 1,
                "document_key": "foreign:75:" + number,
                "manual_mrz_review": 0,
            },
        )

    @classmethod
    def simple_document(cls, person, kind, key_prefix):
        serial = next(COUNTER)
        return cls.db.insert(
            "identity_documents",
            {
                "person_id": person["id"],
                "type": kind,
                "series": "V-МЮ",
                "number": str(100000 + serial),
                "issued_by": "Орган ЗАГС г. Москвы",
                "issue_date": "2015-06-01",
                "verification_status": "подтверждён",
                "is_primary": 0,
                "document_key": key_prefix + ":" + str(serial),
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
            "number": "АКВ-2026-9" + str(next(COUNTER)).zfill(4),
            "client_id": self.client["id"],
            "manager_id": self.user["id"],
            "status": "Забронировано (ожидает оплаты)",
            "country_id": self.turkey["id"],
            "city": "Анталия",
            "date_from": DEPARTURE,
            "date_to": RETURN,
            "nights": 7,
            "hotel": "Demo Beach Resort 5*",
            "room": "Standard Land View",
            "meal": "всё включено",
            "adults": 1,
            "children": 1,
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
            "services": "Авиаперелёт, трансфер аэропорт — отель — аэропорт, проживание, питание «всё включено», медицинская страховка",
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

    def adult_with_passport(self, **extra):
        person = self.person("Семёнов", "Илья", "Петрович", "1990-02-03", **extra)
        self.passport(person, "2031-05-10")
        return person

    # --- разбор результатов -----------------------------------------------

    def found(self, deal, codes=None, on=TODAY):
        return rules.check_deal(self.db, deal["id"], on=on, codes=codes)

    def levels(self, issues, level):
        return [item for item in issues if item["level"] == level]

    def text(self, issues):
        return " | ".join(item["message"] for item in issues)

    # --- V1: срок действия загранпаспорта ------------------------------------

    def test_v1_passport_must_cover_six_months_after_return(self):
        deal = self.deal()
        person = self.person("Семёнов", "Илья", "Петрович", "1990-02-03")
        self.passport(person, "2026-09-01")
        self.travel(deal, person)
        issues = self.found(deal, codes=("V1",))
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["level"], rules.BLOCK)
        self.assertIn("17.01.2027", issues[0]["message"])
        self.assertEqual(issues[0]["details"]["required_until"], "2027-01-17")

    def test_v1_missing_foreign_passport_blocks_trip_abroad(self):
        deal = self.deal()
        person = self.person("Щукин", "Пётр", "Ильич", "1988-03-04")
        self.simple_document(person, "паспорт РФ", "rf")
        self.travel(deal, person)
        self.assertIn("нет загранпаспорта", self.text(self.found(deal, codes=("V1",))))

    def test_v1_passes_for_valid_passport(self):
        deal = self.deal()
        self.travel(deal, self.adult_with_passport())
        self.assertEqual(self.found(deal, codes=("V1",)), [])

    # --- V2: латиница и MRZ -------------------------------------------------

    def test_v2_mrz_wins_over_manual_latin(self):
        deal = self.deal()
        person = self.person("Семёнов", "Илья", "Петрович", "1990-02-03")
        self.passport(person, "2031-05-10", latin=("SEMYONOV", "ILYA"))
        self.travel(deal, person)
        stops = self.levels(self.found(deal, codes=("V2",)), rules.BLOCK)
        self.assertEqual(len(stops), 2)
        self.assertIn("SEMENOV", self.text(stops))
        self.assertEqual(stops[0]["details"]["mrz"], "SEMENOV")

    def test_v2_transliteration_is_only_a_warning(self):
        deal = self.deal()
        person = self.person("Щукин", "Пётр", "Ильич", "1988-03-04")
        self.passport(person, "2031-05-10", mrz_names=("Шукин", "Петр"))
        self.travel(deal, person)
        issues = self.found(deal, codes=("V2",))
        self.assertEqual(self.levels(issues, rules.BLOCK), [])
        self.assertIn("889", self.text(self.levels(issues, rules.WARN)))

    def test_v2_unreadable_mrz_is_a_warning(self):
        deal = self.deal()
        person = self.person("Семёнов", "Илья", "Петрович", "1990-02-03")
        row = self.passport(person, "2031-05-10")
        self.db.update("identity_documents", row["id"], {"mrz_raw": "P<RUSSEMENOV<<ILIA"})
        self.travel(deal, person)
        issues = self.found(deal, codes=("V2",))
        self.assertEqual(self.levels(issues, rules.BLOCK), [])
        self.assertIn("сверьте латиницу", self.text(issues))

    # --- V3: тариф по возрасту ------------------------------------------

    def test_v3_tariff_uses_age_on_departure(self):
        deal = self.deal()
        self.travel(deal, self.adult_with_passport())
        child = self.person("Семёнова", "Анна", "Ильинична", "2014-07-01", gender="ж", phone="")
        self.passport(child, "2030-04-01")
        self.travel(deal, child, tariff="child")
        issues = self.found(deal, codes=("V3",))
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["details"], {"expected": "adult", "actual": "child", "age": 12})
        self.assertIn("нужен тариф «взрослый»", issues[0]["message"])

    def test_v3_child_tariff_is_correct(self):
        deal = self.deal()
        child = self.person("Семёнова", "Анна", "Ильинична", "2015-05-20", gender="ж", phone="")
        self.passport(child, "2030-04-01")
        self.travel(deal, child, tariff="child", parents_count=2)
        self.assertEqual(self.found(deal, codes=("V3",)), [])

    # --- V4: согласие на обработку ПД ----------------------------------

    def test_v4_requires_consent_and_respects_revocation(self):
        deal = self.deal()
        person = self.adult_with_passport()
        self.travel(deal, person)
        self.assertIn("152-ФЗ", self.text(self.found(deal, codes=("V4",))))
        self.consent(person, "consent_pd", deal)
        self.assertEqual(self.found(deal, codes=("V4",)), [])
        self.db.update("persons", person["id"], {"processing_blocked": 1})
        self.assertIn("отозвал согласие", self.text(self.found(deal, codes=("V4",))))

    # --- V5: несовершеннолетние -------------------------------------------

    def test_v5_minor_without_parents_needs_notarized_consent(self):
        deal = self.deal()
        child = self.person("Семёнова", "Анна", "Ильинична", "2015-05-20", gender="ж", phone="")
        self.passport(child, "2030-04-01")
        tie = self.travel(deal, child, tariff="child", parents_count=0)
        issues = self.found(deal, codes=("V5",))
        self.assertEqual(len(self.levels(issues, rules.BLOCK)), 2)
        self.assertIn("нотариальное согласие", self.text(issues))
        self.assertIn("свидетельство о рождении", self.text(issues))
        self.simple_document(child, "свидетельство о рождении", "birth")
        self.db.update(
            "deal_persons",
            tie["id"],
            {
                "notarized_consent_file_id": self.file_row()["id"],
                "legal_representative_id": self.adult_with_passport()["id"],
            },
        )
        self.assertEqual(self.found(deal, codes=("V5",)), [])

    def test_v5_single_parent_is_a_warning(self):
        deal = self.deal()
        child = self.person("Семёнова", "Анна", "Ильинична", "2015-05-20", gender="ж", phone="")
        self.passport(child, "2030-04-01")
        self.travel(deal, child, tariff="child", parents_count=1)
        issues = self.found(deal, codes=("V5",))
        self.assertEqual(self.levels(issues, rules.BLOCK), [])
        self.assertIn("согласие второго родителя", self.text(issues))

    # --- V6: дедлайны оплаты ---------------------------------------------

    def test_v6_overdue_client_payment_blocks(self):
        deal = self.deal(
            paid_client=5000000,
            paid_operator=0,
            client_deadline="2026-05-20",
            operator_deadline="2026-06-02",
        )
        self.travel(deal, self.adult_with_passport())
        issues = self.found(deal, codes=("V6",))
        stops = self.levels(issues, rules.BLOCK)
        self.assertEqual(len(stops), 1)
        self.assertIn("просрочена на 12 дн.", stops[0]["message"])
        self.assertEqual(stops[0]["details"]["rest"], 19500000)
        self.assertIn("Завтра", self.text(self.levels(issues, rules.WARN)))

    def test_v6_skips_cancelled_deal(self):
        deal = self.deal(status="Аннулировано", paid_client=0, paid_operator=0, client_deadline="2026-05-01")
        self.travel(deal, self.adult_with_passport())
        self.assertEqual(self.found(deal, codes=("V6",)), [])

    # --- V7: нетто, комиссия и цена клиента -------------------------------

    def test_v7_price_must_equal_net_plus_commission(self):
        deal = self.deal(commission=100000)
        self.travel(deal, self.adult_with_passport())
        stops = self.levels(self.found(deal, codes=("V7",)), rules.BLOCK)
        self.assertEqual(len(stops), 1)
        self.assertIn("Цифры не сходятся", stops[0]["message"])
        self.assertEqual(stops[0]["details"]["difference"], 2350000)

    def test_v7_unusual_commission_is_a_warning(self):
        deal = self.deal(operator_net=23275000, commission=1225000, paid_operator=23275000)
        self.travel(deal, self.adult_with_passport())
        issues = self.found(deal, codes=("V7",))
        self.assertEqual(self.levels(issues, rules.BLOCK), [])
        self.assertIn("5,0", self.text(issues))

    # --- V8: дубли --------------------------------------------------------

    def test_v8_duplicate_traveller_rejected_by_database(self):
        """Пара «сделка + турист» уникальна в базе, а V8 остаётся страховкой при чтении."""
        deal = self.deal()
        person = self.adult_with_passport()
        self.travel(deal, person)
        with self.assertRaises(Exception):
            self.travel(deal, person)
        self.assertEqual(self.levels(self.found(deal, codes=("V8",)), rules.BLOCK), [])

    def test_v8_namesakes_are_only_a_warning(self):
        deal = self.deal()
        self.travel(deal, self.adult_with_passport())
        self.travel(deal, self.adult_with_passport())
        issues = self.found(deal, codes=("V8",))
        self.assertEqual(self.levels(issues, rules.BLOCK), [])
        self.assertTrue(issues)

    def test_duplicate_document_key_rejected_by_storage(self):
        person = self.adult_with_passport()
        other = self.person("Иванов", "Иван", "Иванович", "1991-01-01")
        existing = self.db.all("identity_documents", "person_id=?", (person["id"],))[0]
        with self.assertRaises(Exception):
            self.db.insert(
                "identity_documents",
                {
                    "person_id": other["id"],
                    "type": "загранпаспорт РФ",
                    "document_key": existing["document_key"],
                    "is_primary": 0,
                    "manual_mrz_review": 0,
                },
            )

    # --- V9: виза ---------------------------------------------------------

    def test_v9_visa_blocks_when_departure_is_close(self):
        deal = self.deal(
            country_id=self.india["id"],
            city="Гоа",
            date_from="2026-06-10",
            date_to="2026-06-20",
            hotel="Demo Goa Resort 4*",
        )
        person = self.person("Щукин", "Пётр", "Ильич", "1988-03-04", needs_visa=1)
        self.passport(person, "2031-05-10")
        self.travel(deal, person)
        stops = self.levels(self.found(deal, codes=("V9",)), rules.BLOCK)
        self.assertTrue(stops)
        self.assertIn("виза", self.text(stops))
        self.assertEqual(stops[0]["details"]["days_left"], 9)

    def test_v9_visa_document_clears_the_check(self):
        deal = self.deal(country_id=self.india["id"], date_from="2026-12-01", date_to="2026-12-12")
        person = self.person("Щукин", "Пётр", "Ильич", "1988-03-04", needs_visa=1)
        self.passport(person, "2031-05-10")
        self.travel(deal, person)
        self.assertEqual(self.levels(self.found(deal, codes=("V9",)), rules.BLOCK), [])
        self.simple_document(person, "виза", "visa")
        self.assertEqual(self.found(deal, codes=("V9",)), [])

    # --- V10: обязательные поля шаблонов -------------------------------

    def test_v10_blocks_when_template_fields_are_empty(self):
        deal = self.deal(hotel="", room="", meal="", nights=0, host_contacts="")
        self.travel(deal, self.adult_with_passport())
        issues = self.found(deal, codes=("V10",))
        self.assertTrue(issues)
        self.assertEqual(issues[0]["level"], rules.BLOCK)
        self.assertTrue(issues[0]["details"]["missing"])
        self.assertIn(issues[0]["details"]["template"], {item["type"] for item in docflow.registry.TEMPLATES})

    def test_v10_passes_for_complete_deal(self):
        deal = self.deal()
        self.travel(deal, self.adult_with_passport())
        self.assertEqual(self.found(deal, codes=("V10",)), [])

    # --- V11: трансграничная передача -----------------------------------

    def test_v11_cross_border_consent_required(self):
        deal = self.deal()
        person = self.adult_with_passport()
        self.travel(deal, person)
        self.assertIn("01.09.2025", self.text(self.found(deal, codes=("V11",))))
        self.consent(person, "consent_cross_border", deal)
        self.assertEqual(self.found(deal, codes=("V11",)), [])

    def test_v11_requires_recipients(self):
        deal = self.deal(recipients=[])
        person = self.adult_with_passport()
        self.consent(person, "consent_cross_border", deal)
        self.travel(deal, person)
        self.assertIn("получатели", self.text(self.found(deal, codes=("V11",))))

    # --- блокировка переходов -------------------------------------------

    def test_gate_blocks_transition_and_reports_issues(self):
        deal = self.deal(commission=100000)
        self.travel(deal, self.adult_with_passport())
        with self.assertRaises(AppError) as caught:
            rules.gate(self.db, deal["id"], "Оплачено полностью", on=TODAY)
        error = caught.exception
        self.assertEqual(error.status, 422)
        self.assertEqual(error.details["status"], "Оплачено полностью")
        self.assertEqual(error.details["issues"][0]["code"], "V7")

    def test_gate_allows_clean_deal(self):
        deal = self.deal()
        person = self.adult_with_passport()
        self.travel(deal, person)
        self.consent(person, "consent_pd", deal)
        self.consent(person, "consent_cross_border", deal)
        issues = rules.gate(self.db, deal["id"], "Забронировано (ожидает оплаты)", on=TODAY)
        self.assertEqual(self.levels(issues, rules.BLOCK), [])

    def test_gate_rejects_unknown_status(self):
        deal = self.deal()
        self.travel(deal, self.adult_with_passport())
        with self.assertRaises(AppError):
            rules.gate(self.db, deal["id"], "Отправлено в космос", on=TODAY)

    def test_registry_is_consistent(self):
        from backend.service import STATUSES

        for status in rules.STATUS_GATES:
            self.assertIn(status, STATUSES)
        for code in rules.ALL_CODES:
            self.assertIn(code, rules.TITLES)
        self.assertEqual(len(rules.CHECKS), 11)

    def test_summary_counts_blockers_and_warnings(self):
        deal = self.deal(commission=100000)
        child = self.person("Семёнова", "Анна", "Ильинична", "2015-05-20", gender="ж", phone="")
        self.passport(child, "2030-04-01")
        self.travel(deal, child, tariff="child", parents_count=1)
        report = rules.summary(self.found(deal, codes=("V5", "V7")))
        self.assertEqual(report["blocking"], 1)
        # одно предупреждение от V5 (едет с одним родителем) и одно от V7 (комиссия вне 8–12 %)
        self.assertEqual(report["warnings"], 2)
        self.assertFalse(report["ready"])
        self.assertEqual(report["codes"], ["V5", "V7"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
