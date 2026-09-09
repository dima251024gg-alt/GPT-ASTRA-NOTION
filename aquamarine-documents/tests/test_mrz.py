"""Тесты MRZ TD3 и транслитерации по Приказу МВД России № 889."""
import unittest
from datetime import date
from backend.ocr import build_mrz, check_digit, mrz_date, parse_mrz, transliterate
from backend.security import AppError

class CheckDigitTest(unittest.TestCase):
    def test_icao_examples(self):
        self.assertEqual(check_digit("L898902C3"), "6")
        self.assertEqual(check_digit("740812"), "2")
        self.assertEqual(check_digit("120415"), "9")
        self.assertEqual(check_digit("ZE184226B<<<<<"), "1")
    def test_fillers_and_bad_character(self):
        self.assertEqual(check_digit("<<<<<<<<<<<<<<"), "0")
        with self.assertRaisesRegex(AppError, "Недопустимый символ"): check_digit("AB-12")

class TransliterationTest(unittest.TestCase):
    def test_order_889_special_letters(self):
        self.assertEqual(transliterate("ЁЖИЙХЦЧШЩЪЫЬЭЮЯ"), "EZHIIKHTSCHSHSHCHIEYEIUIA")
    def test_soft_sign_and_case(self):
        self.assertEqual(transliterate("Илья"), "ILIA")
        self.assertEqual(transliterate("Любовь"), "LIUBOV")
    def test_hyphenated_surname_is_mrz_safe(self):
        raw=build_mrz("Соловьёв-Петров","Анна Мария","751234567","1990-02-28","2030-02-28","F")
        first,_=raw.splitlines(); self.assertRegex(first,r"^[A-Z0-9<]{44}$")
        parsed=parse_mrz(raw,today=date(2026,9,10))
        self.assertEqual(parsed["fields"]["last_name_latin"],"SOLOVEV PETROV")
        self.assertEqual(parsed["fields"]["first_name_latin"],"ANNA MARIIA")

class Td3Test(unittest.TestCase):
    ICAO=("P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\n"
          "L898902C36UTO7408122F1204159ZE184226B<<<<<10")
    def test_known_icao_td3_sample(self):
        result=parse_mrz(self.ICAO,today=date(2010,1,1))
        self.assertTrue(result["mrz_valid"]); self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["fields"]["number"],"L898902C3")
        self.assertEqual(result["fields"]["last_name_latin"],"ERIKSSON")
        self.assertEqual(result["fields"]["first_name_latin"],"ANNA MARIA")
        self.assertEqual(result["fields"]["birth_date"],"1974-08-12")
        self.assertEqual(result["fields"]["expiry_date"],"2012-04-15")
    def test_build_parse_round_trip_and_mrz_latin(self):
        raw=build_mrz("Иванов","Пётр Сергеевич","721234567","1985-06-17","2031-07-22","M")
        self.assertEqual([44,44],list(map(len,raw.splitlines())))
        result=parse_mrz(raw,today=date(2026,9,10)); self.assertTrue(result["mrz_valid"])
        self.assertEqual(result["fields"]["last_name_latin"],"IVANOV")
        self.assertEqual(result["fields"]["first_name_latin"],"PETR SERGEEVICH")
        self.assertEqual(result["fields"]["number"],"721234567")
    def test_compact_88_character_input(self):
        self.assertTrue(parse_mrz(self.ICAO.replace("\n",""),today=date(2010,1,1))["mrz_valid"])
    def test_bad_check_digit_requires_manual_review(self):
        first,second=self.ICAO.splitlines(); damaged=second[:9]+("7" if second[9]!="7" else "8")+second[10:]
        result=parse_mrz(first+"\n"+damaged,today=date(2010,1,1))
        self.assertFalse(result["mrz_valid"]); self.assertFalse(result["checks"]["number"])
        self.assertTrue(result["warnings"]); self.assertLess(result["confidence"]["number"],.75)
    def test_impossible_date_requires_manual_review(self):
        first,second=self.ICAO.splitlines(); invalid="741332"
        changed=second[:13]+invalid+check_digit(invalid)+second[20:]
        changed=changed[:43]+check_digit(changed[:10]+changed[13:20]+changed[21:43])
        result=parse_mrz(first+"\n"+changed,today=date(2010,1,1))
        self.assertFalse(result["mrz_valid"]); self.assertEqual(result["fields"]["birth_date"],"")
        self.assertFalse(result["checks"]["birth_date"])
    def test_invalid_shape_and_date_helpers(self):
        with self.assertRaisesRegex(AppError,"две строки по 44"): parse_mrz("P<RUSSHORT")
        with self.assertRaisesRegex(AppError,"Недопустимая дата"): mrz_date("991332",birth=True,today=date(2026,9,10))
        with self.assertRaisesRegex(AppError,"ручной проверки"): mrz_date("ABCDEF",birth=True,today=date(2026,9,10))
    def test_builder_rejects_invalid_inputs(self):
        bad=[dict(number="12-34"),dict(birth="1999-02-30"),dict(expiry="2030-13-01"),dict(gender="Ж"),dict(country="RU")]
        defaults=dict(last="Иванов",first="Иван",number="721234567",birth="1999-01-01",expiry="2030-01-01",gender="M",country="RUS")
        for patch in bad:
            with self.subTest(patch=patch),self.assertRaises(AppError): build_mrz(**(defaults|patch))

if __name__=="__main__": unittest.main()
