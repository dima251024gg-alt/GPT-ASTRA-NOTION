"""Автотесты спринта S3: движок шаблонов и вывод в PDF/DOCX."""

import io
import unittest
import zipfile

from backend import documents, render
from backend.security import AppError

RUBLES = ("рубль", "рубля", "рублей")


class NumbersTest(unittest.TestCase):
    def test_money_format(self):
        self.assertEqual(render.money(12345678), "123\\u00a0456,78")
        self.assertEqual(render.money(4500), "45,00")
        self.assertEqual(render.money(-100), "-1,00")
        self.assertEqual(render.money(None), "")

    def test_money_words(self):
        self.assertEqual(render.money_words(0), "Ноль рублей 00 копеек")
        self.assertEqual(render.money_words(100100), "Одна тысяча один рубль 00 копеек")
        self.assertEqual(render.money_words(200000), "Две тысячи рублей 00 копеек")
        self.assertEqual(
            render.money_words(12345678),
            "Сто двадцать три тысячи четыреста пятьдесят шесть рублей 78 копеек",
        )
        self.assertTrue(render.money_words(-500).startswith("Минус "))

    def test_plural_forms(self):
        self.assertEqual(render.plural(1, RUBLES), "рубль")
        self.assertEqual(render.plural(11, RUBLES), "рублей")
        self.assertEqual(render.plural(23, RUBLES), "рубля")
        self.assertEqual(render.plural(25, RUBLES), "рублей")

    def test_dates(self):
        self.assertEqual(render.date_ru("2026-03-07"), "07.03.2026")
        self.assertEqual(render.date_ru("2026-03-07T09:05:00+03:00"), "07.03.2026")
        self.assertEqual(render.datetime_ru("2026-03-07T09:05:00+03:00"), "07.03.2026 09:05")
        self.assertEqual(render.date_ru("07.03.2026"), "07.03.2026")
        self.assertEqual(render.date_ru(""), "")


class GenitiveTest(unittest.TestCase):
    CASES = (
        ("Семёнов Илья Петрович", "Семёнова Ильи Петровича"),
        ("Шукина-Ёлкина Юлия Сергеевна", "Шукиной-Ёлкиной Юлии Сергеевны"),
        ("Кузнецов Андрей", "Кузнецова Андрея"),
        ("Шевченко Ольга Ивановна", "Шевченко Ольги Ивановны"),
        ("Достоевский Фёдор Михайлович", "Достоевского Фёдора Михайловича"),
        ("Гуща Никита Игоревич", "Гущи Никиты Игоревича"),
        ("Царёва Мария", "Царёвой Марии"),
        ("Любимова Любовь Петровна", "Любимовой Любови Петровны"),
        ("Ким Сергей", "Кима Сергея"),
    )

    def test_genitive(self):
        for source, expected in self.CASES:
            with self.subTest(fio=source):
                self.assertEqual(render.genitive_fio(source), expected)

    def test_gender_detection(self):
        self.assertEqual(render.detect_gender("Семёнов Илья Петрович"), "male")
        self.assertEqual(render.detect_gender("Царёва Мария"), "female")
        self.assertEqual(render.detect_gender("Шевченко Ольга Ивановна"), "female")
        self.assertEqual(render.detect_gender("Гуща Никита Игоревич"), "male")
        self.assertEqual(
            render.genitive_fio("Соколова Александра", gender="female"),
            "Соколовой Александры",
        )


class EngineTest(unittest.TestCase):
    CONTEXT = {
        "DEAL": {"NUMBER": "АКВ-2026-00123", "DATE": "2026-03-07", "NOTE": ""},
        "CLIENT": {"FIO": "Семёнов Илья Петрович"},
        "TOUR": {"PRICE": 24500000},
        "TOURISTS": {"LIST": [
            {"FIO": "Семёнов Илья Петрович", "BIRTH_DATE": "1990-02-03"},
            {"FIO": "Семёнова Анна Ильинична", "BIRTH_DATE": "2016-07-19"},
        ]},
    }

    def test_substitution_and_filters(self):
        text = render.render_text(
            "№ [[DEAL.NUMBER]] от [[DEAL.DATE|date]]; заказчик [[CLIENT.FIO|gen]]; "
            "сумма [[TOUR.PRICE|money]] ([[TOUR.PRICE|words]]); туристов [[TOURISTS.LIST|count]]",
            self.CONTEXT,
        )
        self.assertIn("№ АКВ-2026-00123 от 07.03.2026", text)
        self.assertIn("заказчик Семёнова Ильи Петровича", text)
        self.assertIn("Двести сорок пять тысяч рублей 00 копеек", text)
        self.assertTrue(text.endswith("туристов 2"))

    def test_unknown_field_is_empty(self):
        self.assertEqual(render.render_text("[[DEAL.MISSING]]|[[NOPE]]", self.CONTEXT), "|")

    def test_each_loop(self):
        text = render.render_text(
            "[[#EACH TOURISTS.LIST]][[INDEX]]. [[ITEM.FIO]] ([[ITEM.BIRTH_DATE|date]]); [[/EACH]]",
            self.CONTEXT,
        )
        self.assertEqual(
            text,
            "1. Семёнов Илья Петрович (03.02.1990); 2. Семёнова Анна Ильинична (19.07.2016); ",
        )

    def test_empty_loop(self):
        self.assertEqual(render.render_text("а[[#EACH NOPE]]x[[/EACH]]б", self.CONTEXT), "аб")

    def test_condition_with_else(self):
        body = "[[#IF DEAL.NOTE]]есть[[ELSE]]нет[[/IF]]|[[#IF DEAL.NUMBER]]есть[[ELSE]]нет[[/IF]]"
        self.assertEqual(render.render_text(body, self.CONTEXT), "нет|есть")

    def test_nested_blocks(self):
        body = "[[#EACH A.LIST]][[ITEM.NAME]]:[[#IF ITEM.OK]]да[[ELSE]]нет[[/IF]];[[/EACH]]"
        context = {"A": {"LIST": [{"NAME": "x", "OK": 1}, {"NAME": "y", "OK": ""}]}}
        self.assertEqual(render.render_text(body, context), "x:да;y:нет;")

    def test_unknown_filter(self):
        with self.assertRaises(AppError) as caught:
            render.render_text("[[DEAL.NUMBER|magic]]", self.CONTEXT)
        self.assertIn("Неизвестный фильтр", caught.exception.message)

    def test_broken_templates(self):
        for broken in ("[[#EACH A.LIST]]без закрытия", "[[/IF]]", "[[DEAL.NUMBER"):
            with self.subTest(body=broken):
                with self.assertRaises(AppError):
                    render.render_text(broken, self.CONTEXT)

    def test_required_fields_block_generation(self):
        template = {
            "name": "Счёт на оплату",
            "type": "invoice",
            "body": "[[DEAL.NUMBER]] [[PAYMENT.AMOUNT|money]]",
            "required_fields": ["DEAL.NUMBER", "PAYMENT.AMOUNT", "CLIENT.FIO"],
        }
        context = {"DEAL": {"NUMBER": "АКВ-2026-00123"}}
        self.assertEqual(render.missing_fields(template, context), ["PAYMENT.AMOUNT", "CLIENT.FIO"])
        with self.assertRaises(AppError) as caught:
            render.render_template(template, context)
        self.assertIn("не заполнены обязательные поля", caught.exception.message)
        self.assertEqual(caught.exception.details["missing"], ["PAYMENT.AMOUNT", "CLIENT.FIO"])

    def test_render_template_ok(self):
        template = {
            "name": "Счёт на оплату",
            "type": "invoice",
            "body": "[[DEAL.NUMBER]]: [[PAYMENT.AMOUNT|money]] руб.",
            "required_fields": ["DEAL.NUMBER", "PAYMENT.AMOUNT"],
        }
        context = {"DEAL": {"NUMBER": "АКВ-2026-00123"}, "PAYMENT": {"AMOUNT": 5000000}}
        self.assertEqual(
            render.render_template(template, context),
            "АКВ-2026-00123: 50\\u00a0000,00 руб.",
        )


class FilesTest(unittest.TestCase):
    META = {
        "title": "Договор АКВ-2026-00123",
        "agency": "ООО «Аквамарин»",
        "deal_number": "Сделка АКВ-2026-00123",
        "document_code": "contract_tour v1",
        "generated_at": "09.09.2026 10:35",
        "verify_url": "http://localhost:8000/p/token",
    }
    TEXT = "ДОГОВОР О РЕАЛИЗАЦИИ ТУРИСТСКОГО ПРОДУКТА\\n\\nЗаказчик: Семёнов Илья Петрович.\\n"

    def test_font_available(self):
        self.assertIsNotNone(documents.font_paths(), "в системе нет шрифта с кириллицей")

    def test_pdf_and_docx(self):
        files = documents.build_files(self.TEXT, self.META)
        self.assertTrue(files["pdf"].startswith(b"%PDF"))
        self.assertGreaterEqual(files["pages"], 1)
        self.assertEqual(len(files["sha256"]), 64)
        with zipfile.ZipFile(io.BytesIO(files["docx"])) as archive:
            names = archive.namelist()
            body = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("word/footer1.xml", names)
        self.assertIn("Семёнов Илья Петрович", body)

    def test_pagination(self):
        long_text = self.TEXT + ("Условия договора действуют до полного исполнения сторонами обязательств. " * 200)
        pdf = documents.to_pdf(long_text, self.META)
        self.assertGreater(documents.page_count(pdf), 1)

    def test_package_merge(self):
        first = documents.to_pdf(self.TEXT, self.META)
        second = documents.to_pdf("ПАМЯТКА ТУРИСТУ\\n\\nВылет в 09:40.", {"document_code": "memo v1"})
        bundle = documents.merge_pdfs([first, second])
        self.assertEqual(
            documents.page_count(bundle),
            documents.page_count(first) + documents.page_count(second),
        )

    def test_broken_package_input(self):
        with self.assertRaises(AppError):
            documents.merge_pdfs([b"not a pdf at all"])
        with self.assertRaises(AppError):
            documents.merge_pdfs([])


if __name__ == "__main__":
    unittest.main()
