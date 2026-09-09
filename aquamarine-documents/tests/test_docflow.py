"""Автотесты сервисного слоя документооборота (спринт S3).

Проверяется логика, а не вёрстка: версии шаблонов, идемпотентность генерации,
блокировка по незаполненным полям (V10), автосборка пакетов, задачи при некомплекте,
жизненный цикл защищённой ссылки и целостность аудит-лога.

Сам движок PDF/DOCX подменён заглушкой — его проверки в tests/test_documents.py.
"""

import hashlib
import os
import sys
import tempfile
import types
import unittest

SANDBOX = tempfile.TemporaryDirectory()
os.environ["APP_ENV"] = "test"
os.environ["PROVIDER_MODE"] = "mock"
os.environ["DATA_DIR"] = SANDBOX.name
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(SANDBOX.name, "docflow.sqlite3")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend  # noqa: E402
from backend.security import AppError  # noqa: E402


def _build_files(text, meta=None):
    body = str(text).encode("utf-8")
    pdf = b"%PDF-1.4\n" + body[:400] + b"\n%%EOF"
    return {
        "pdf": pdf,
        "docx": b"PK\x03\x04" + body[:200],
        "sha256": hashlib.sha256(pdf).hexdigest(),
        "pages": 1,
    }


def _merge_pdfs(items):
    if not items:
        raise AppError("Нет файлов для сборки пакета")
    for index, item in enumerate(items, start=1):
        if not item.startswith(b"%PDF"):
            raise AppError("Файл №" + str(index) + " в пакете не похож на PDF")
    return b"%PDF-1.4\n" + b"\n".join(items) + b"\n%%EOF"


ENGINE = types.ModuleType("backend.documents")
ENGINE.build_files = _build_files
ENGINE.merge_pdfs = _merge_pdfs
ENGINE.page_count = lambda data: max(1, data.count(b"%PDF") - 1)
ENGINE.digest = lambda data: hashlib.sha256(data).hexdigest()
sys.modules["backend.documents"] = ENGINE
backend.documents = ENGINE

from backend import docflow, render  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.service import Service  # noqa: E402


class DocflowTest(unittest.TestCase):
    """Одна сделка с взрослым и ребёнком в Турцию — типовой случай турагентства."""

    @classmethod
    def setUpClass(cls):
        db = Database()
        db.migrate()
        cls.db = db
        cls.user = db.insert(
            "users",
            {
                "full_name": "Смирнова Ольга Ивановна",
                "email": "olga@aquamarine.test",
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
        cls.synced = docflow.sync_templates(db, cls.user["id"])
        country = db.insert(
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
        operator = db.insert(
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
                "contacts": {"emergency": "+7 800 100-20-30", "phone": "+7 495 100-20-30", "email": "help@to.test"},
                "payment_hours": 24,
                "default_commission_bps": 1000,
            },
        )
        client = db.insert(
            "clients",
            {
                "type": "individual",
                "full_name": "Семёнов Илья Петрович",
                "phone": "+7 916 555-33-11",
                "email": "semenov@mail.test",
                "registration_address": "119021, г. Москва, ул. Жилая, д. 4, кв. 7",
                "birth_date": "1990-02-03",
                "manager_id": cls.user["id"],
                "status": "лид",
                "contact_key": "client-1",
                "tags": [],
            },
        )
        adult = db.insert(
            "persons",
            {
                "client_id": client["id"],
                "last_name_ru": "Семёнов",
                "first_name_ru": "Илья",
                "patronymic_ru": "Петрович",
                "gender": "male",
                "birth_date": "1990-02-03",
                "citizenship": "RUS",
                "birth_place": "г. Москва",
                "phone": "+7 916 555-33-11",
                "email": "semenov@mail.test",
                "relationship": "заказчик",
                "identity_key": "person-adult",
                "processing_blocked": 0,
                "legal_hold": 0,
                "needs_visa": 0,
            },
        )
        child = db.insert(
            "persons",
            {
                "client_id": client["id"],
                "last_name_ru": "Семёнова",
                "first_name_ru": "Анна",
                "patronymic_ru": "Ильинична",
                "gender": "female",
                "birth_date": "2015-05-20",
                "citizenship": "RUS",
                "birth_place": "г. Москва",
                "relationship": "дочь",
                "identity_key": "person-child",
                "processing_blocked": 0,
                "legal_hold": 0,
                "needs_visa": 0,
            },
        )
        db.insert(
            "identity_documents",
            {
                "person_id": adult["id"],
                "type": "паспорт РФ",
                "series": "45 12",
                "number": "123456",
                "issued_by": "ОВД района Хамовники г. Москвы",
                "division_code": "770-001",
                "issue_date": "2010-03-15",
                "issuing_country": "RUS",
                "verification_status": "подтверждён",
                "confidence": {"number": 0.98},
                "is_primary": 1,
                "document_key": "doc-adult-rf",
                "manual_mrz_review": 0,
            },
        )
        db.insert(
            "identity_documents",
            {
                "person_id": adult["id"],
                "type": "загранпаспорт РФ",
                "series": "75",
                "number": "1234567",
                "issued_by": "ФМС 77001",
                "issue_date": "2021-05-10",
                "expiry_date": "2031-05-10",
                "last_name_latin": "SEMENOV",
                "first_name_latin": "ILIA",
                "issuing_country": "RUS",
                "mrz_raw": "P<RUSSEMENOV<<ILIA<<<<<<<<<<<<<<<<<<<<<<<<<<",
                "verification_status": "подтверждён",
                "confidence": {"mrz": 0.99},
                "is_primary": 0,
                "document_key": "doc-adult-foreign",
                "manual_mrz_review": 0,
            },
        )
        db.insert(
            "identity_documents",
            {
                "person_id": child["id"],
                "type": "свидетельство о рождении",
                "series": "V-МЮ",
                "number": "123456",
                "issued_by": "Отдел ЗАГС района Хамовники г. Москвы",
                "issue_date": "2015-06-01",
                "issuing_country": "RUS",
                "verification_status": "подтверждён",
                "confidence": {"number": 0.95},
                "is_primary": 1,
                "document_key": "doc-child-birth",
                "manual_mrz_review": 0,
            },
        )
        db.insert(
            "identity_documents",
            {
                "person_id": child["id"],
                "type": "загранпаспорт РФ",
                "series": "75",
                "number": "7654321",
                "issued_by": "ФМС 77001",
                "issue_date": "2022-04-01",
                "expiry_date": "2027-04-01",
                "last_name_latin": "SEMENOVA",
                "first_name_latin": "ANNA",
                "issuing_country": "RUS",
                "mrz_raw": "P<RUSSEMENOVA<<ANNA<<<<<<<<<<<<<<<<<<<<<<<<<",
                "verification_status": "подтверждён",
                "confidence": {"mrz": 0.97},
                "is_primary": 0,
                "document_key": "doc-child-foreign",
                "manual_mrz_review": 0,
            },
        )
        db.insert(
            "addresses",
            {
                "person_id": adult["id"],
                "type": "регистрация",
                "postal_code": "119021",
                "region": "г. Москва",
                "city": "Москва",
                "street": "ул. Жилая",
                "house": "4",
                "apartment": "7",
                "full_address": "119021, г. Москва, ул. Жилая, д. 4, кв. 7",
            },
        )
        deal = db.insert(
            "deals",
            {
                "number": "АКВ-2026-00123",
                "client_id": client["id"],
                "manager_id": cls.user["id"],
                "status": "Забронировано",
                "country_id": country["id"],
                "city": "Анталия",
                "date_from": "2026-07-10",
                "date_to": "2026-07-17",
                "nights": 7,
                "hotel": "Demo Beach Resort 5*",
                "room": "Family Room, вид на море",
                "meal": "всё включено (AI)",
                "adults": 1,
                "children": 1,
                "infants": 0,
                "flight_there": {"number": "TK-2026", "summary": "10.07.2026 SVO 08:40 — AYT 12:55, TK-2026"},
                "flight_back": {"number": "TK-2027", "summary": "17.07.2026 AYT 13:45 — SVO 18:10, TK-2027"},
                "tour_type": "пакетный тур",
                "booking_number": "TO-778899",
                "operator_id": operator["id"],
                "currency": "RUB",
                "exchange_rate": "1",
                "client_price": 24500000,
                "operator_net": 22050000,
                "commission": 2450000,
                "paid_client": 0,
                "paid_operator": 0,
                "client_deadline": "2026-06-20",
                "operator_deadline": "2026-06-25",
                "cross_border": 1,
                "comments": "Просьба о детской кроватке.",
                "program": "Пляжный отдых без групповой программы",
                "route": "Москва — Анталия — Москва",
                "guide": "гид не требуется",
                "services": "авиаперелёт, трансфер, размещение 7 ночей, питание AI, медицинская страховка",
                "host_contacts": "Demo DMC, +90 555 000-00-00",
                "recipients": [{"name": "AnadoluJet", "country": "TR", "purpose": "оформление перевозки"}],
                "revision": 1,
            },
        )
        db.insert(
            "deal_persons",
            {
                "deal_id": deal["id"],
                "person_id": adult["id"],
                "role": "заказчик",
                "placement": "Family Room",
                "tariff": "adult",
                "parents_count": 0,
            },
        )
        db.insert(
            "deal_persons",
            {
                "deal_id": deal["id"],
                "person_id": child["id"],
                "role": "турист",
                "placement": "Family Room, доп. место",
                "tariff": "child",
                "parents_count": 1,
                "legal_representative_id": adult["id"],
            },
        )
        cls.client_row = client
        cls.adult = adult
        cls.child = child
        cls.deal_id = deal["id"]

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        SANDBOX.cleanup()

    def test_audit_chain_intact(self):
        """Каждая генерация попадает в неизменяемый журнал, цепочка хешей не рвётся."""
        document = docflow.generate(self.service, self.deal_id, "application")
        self.assertEqual(document["source"], "generated")
        self.assertTrue(self.db.verify_audit())
        actions = [row.get("action") for row in self.db.all("audit_log")]
        self.assertIn("generate", actions)

    def test_contract_bound_to_pdf_hash(self):
        """Договор создаётся вместе с PDF и запоминает его хеш — основа для ПЭП."""
        document = docflow.generate(self.service, self.deal_id, "contract_tour")
        contract = self.db.find("contracts", "deal_id=? AND status=?", (self.deal_id, "черновик"))
        self.assertIsNotNone(contract)
        self.assertEqual(contract["number"], "АКВ-2026-00123")
        self.assertEqual(contract["document_id"], document["id"])
        self.assertEqual(contract["pdf_file_id"], document["file_id"])
        blob, meta = docflow.document_file(self.service, document["id"])
        self.assertEqual(contract["file_hash"], hashlib.sha256(blob).hexdigest())
        self.assertEqual(meta["mime"], "application/pdf")
        docx, _docx_meta = docflow.document_file(self.service, document["id"], "docx")
        self.assertTrue(docx.startswith(b"PK"))

    def test_generate_is_idempotent(self):
        """Повторный запрос не плодит дубли; принудительный — даёт новую версию."""
        first = docflow.generate(self.service, self.deal_id, "memo")
        again = docflow.generate(self.service, self.deal_id, "memo")
        self.assertEqual(first["id"], again["id"])
        forced = docflow.generate(self.service, self.deal_id, "memo", force=True)
        self.assertNotEqual(forced["id"], first["id"])
        self.assertEqual(int(forced["version"]), int(first["version"]) + 1)
        self.assertTrue(self.db.one("documents", first["id"])["superseded"])
        self.assertFalse(self.db.one("documents", forced["id"])["superseded"])

    def test_link_lifecycle(self):
        """Ссылка на пакет: токен в базе не хранится, прежняя при перевыдаче отзывается."""
        package = docflow.build_package(self.service, self.deal_id, "on_contract")
        link, url = docflow.issue_link(self.service, package["id"])
        raw = url.rsplit("/", 1)[-1]
        stored = self.db.one("portal_links", link["id"])
        self.assertNotIn(raw, str(stored.get("token_hash") or ""))
        self.assertEqual(docflow.link_by_token(self.db, raw)["id"], link["id"])
        with self.assertRaises(AppError):
            docflow.link_by_token(self.db, "чужой-токен")
        docflow.mark_opened(self.db, stored, self.user["id"])
        self.assertTrue(self.db.one("packages", package["id"])["opened_at"])
        second, second_url = docflow.issue_link(self.service, package["id"])
        self.assertNotEqual(second["id"], link["id"])
        with self.assertRaises(AppError) as ctx:
            docflow.link_by_token(self.db, raw)
        self.assertIn("отозвана", ctx.exception.message)
        fresh = docflow.link_by_token(self.db, second_url.rsplit("/", 1)[-1])
        self.assertEqual(fresh["id"], second["id"])

    def test_missing_fields_blocked(self):
        """V10: без обязательных полей документ не формируется, а сообщает, чего не хватает."""
        with self.assertRaises(AppError) as ctx:
            docflow.generate(self.service, self.deal_id, "invoice")
        self.assertEqual(ctx.exception.status, 422)
        missing = list(ctx.exception.details["missing"])
        self.assertTrue(missing)
        self.assertTrue(any(field.startswith("PAYMENT.") for field in missing))
        self.assertIn("обязательные поля", ctx.exception.message)
        defaults = {
            "NUMBER": "17",
            "RECEIPT_NUMBER": "17",
            "AMOUNT": 24500000,
            "DUE_DATE": "2026-06-20",
            "PAID_AT": "2026-06-19",
            "METHOD": "безналичный расчёт",
            "PURPOSE": "Оплата туристского продукта по договору",
        }
        extra = {}
        for field in missing:
            block, _, name = field.partition(".")
            extra.setdefault(block, {})[name] = defaults.get(name, "—")
        document = docflow.generate(self.service, self.deal_id, "invoice", extra)
        self.assertEqual(document["type"], "invoice")
        text = render.render_template(
            docflow.template_row(self.db, "invoice"), docflow.build_context(self.db, self.deal_id, extra)
        )
        self.assertNotIn("[[", text)
        self.assertIn("АКВ-2026-00123", text)

    def test_new_version_after_deal_change(self):
        """Правка сделки обесценивает прежние документы и рождает новую версию."""
        before = docflow.generate(self.service, self.deal_id, "memo")
        self.service.bump_deal(self.deal_id)
        after = docflow.generate(self.service, self.deal_id, "memo")
        self.assertNotEqual(after["id"], before["id"])
        self.assertEqual(int(after["version"]), int(before["version"]) + 1)
        self.assertEqual(int(after["data_revision"]), int(before["data_revision"]) + 1)
        self.assertTrue(self.db.one("documents", before["id"])["superseded"])
        self.assertFalse(self.db.one("documents", after["id"])["superseded"])

    def test_package_before_departure_incomplete(self):
        """Пакет на вылет без файлов туроператора помечается неполным и ставит задачу."""
        package = docflow.build_package(self.service, self.deal_id, "before_departure")
        self.assertEqual(package["status"], "неполный")
        codes = sorted({item["template"] for item in package["missing"]})
        self.assertEqual(codes, ["insurance", "ticket", "voucher"])
        for item in package["missing"]:
            self.assertIn("туроператора", item["message"])
        tasks = [row for row in self.db.all("tasks", "deal_id=?", (self.deal_id,)) if row.get("type") == "package_incomplete"]
        self.assertTrue(tasks)
        self.assertIn("неполный", tasks[0]["title"])
        self.assertEqual(tasks[0]["assignee_id"], self.user["id"])
        repeat = docflow.build_package(self.service, self.deal_id, "before_departure")
        self.assertEqual(repeat["id"], package["id"])
        again = [row for row in self.db.all("tasks", "deal_id=?", (self.deal_id,)) if row.get("type") == "package_incomplete"]
        self.assertEqual(len(again), len(tasks))
        with self.assertRaises(AppError):
            docflow.build_package(self.service, self.deal_id, "unknown_package")

    def test_package_on_contract_complete(self):
        """Пакет при заключении собирается целиком: согласия на каждого и согласие за ребёнка."""
        package = docflow.build_package(self.service, self.deal_id, "on_contract")
        self.assertEqual(list(package["missing"] or []), [])
        self.assertEqual(package["status"], "готов")
        kinds = [self.db.one("documents", doc_id)["type"] for doc_id in package["document_ids"]]
        self.assertIn("contract_tour", kinds)
        self.assertIn("pep_agreement", kinds)
        self.assertIn("consent_minor", kinds)
        self.assertIn("consent_cross_border", kinds)
        self.assertEqual(kinds.count("consent_pd"), 2)
        self.assertEqual(kinds.count("consent_transfer"), 2)
        self.assertEqual(kinds.count("consent_minor"), 1)
        self.assertEqual(len(package["document_ids"]), 11)
        blob, meta = docflow.package_file(self.service, package["id"])
        self.assertTrue(blob.startswith(b"%PDF"))
        self.assertEqual(meta["mime"], "application/pdf")
        listing = docflow.deal_documents(self.service, self.deal_id)
        self.assertTrue(listing)
        self.assertTrue(all(item["sha256"] for item in listing))
        self.assertTrue(all(item["has_docx"] for item in listing))
        self.assertTrue(any(item["type"] == "consent_minor" and item["person"] for item in listing))

    def test_sync_templates_versioning(self):
        """Реестр шаблонов заливается один раз, правка тела — новая версия в истории."""
        self.assertGreaterEqual(len(self.synced["created"]), 14)
        self.assertEqual(len(self.synced["created"]), len(self.db.all("templates")))
        self.assertEqual(self.synced["updated"], [])
        row = docflow.template_row(self.db, "memo")
        self.db.update("templates", row["id"], {"body": str(row["body"]) + "\n\nРучная правка."})
        result = docflow.sync_templates(self.db, self.user["id"])
        self.assertEqual(result["updated"], ["memo"])
        fresh = docflow.template_row(self.db, "memo")
        self.assertEqual(int(fresh["version"]), int(row["version"]) + 1)
        self.assertEqual(str(fresh["body"]), str(row["body"]))
        versions = self.db.all("template_versions", "template_id=?", (row["id"],))
        self.assertGreaterEqual(len(versions), 2)
        self.assertEqual(docflow.sync_templates(self.db, self.user["id"])["updated"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
