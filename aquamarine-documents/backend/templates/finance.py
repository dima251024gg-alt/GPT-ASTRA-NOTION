"""Финансовые документы: счёт на оплату, приходный кассовый ордер и акт.

Суммы хранятся в копейках; фильтр |money печатает рубли с копейками,
|words — сумму прописью, |date — ДД.ММ.ГГГГ, |gen — родительный падеж ФИО.
Тексты — редактируемые проекты, не юридическое заключение.
"""

INVOICE = """СЧЁТ НА ОПЛАТУ № [[PAYMENT.NUMBER]] от [[TODAY|date]]
к договору № [[DEAL.NUMBER]] от [[DEAL.DATE|date]]

Исполнитель: [[AGENCY.NAME]], ИНН [[AGENCY.INN]], адрес: [[AGENCY.ADDRESS]]
Реквизиты для оплаты:
[[AGENCY.REQUISITES]]

Плательщик: [[CLIENT.FIO]], телефон [[CLIENT.PHONE]]
Назначение платежа: оплата туристского продукта по договору № [[DEAL.NUMBER]], без НДС (УСН)

Предмет счёта
1. Туристский продукт: [[TOUR.COUNTRY]], [[TOUR.HOTEL]], сроки [[TOUR.DATE_FROM|date]] — [[TOUR.DATE_TO|date]], туристов: [[TOURISTS.COUNT]].
   Туроператор, сформировавший продукт: [[OPERATOR.SHORT_NAME]].
   Сумма: [[PAYMENT.AMOUNT|money]] руб.

Итого к оплате: [[PAYMENT.AMOUNT|money]] руб. ([[PAYMENT.AMOUNT|words]])
Оплатить до: [[PAYMENT.DUE_DATE|date]]
[[#IF PAYMENT.NOTE]]Примечание: [[PAYMENT.NOTE]]
[[/IF]]Счёт действителен до указанной даты. При оплате позже стоимость подтверждается у туроператора и может измениться; изменение оформляется дополнительным соглашением.

[[AGENCY.SIGNER_ROLE]] ____________________ / [[AGENCY.SIGNER_FIO]] /
"""

CASH_RECEIPT = """ПРИХОДНЫЙ КАССОВЫЙ ОРДЕР № [[PAYMENT.RECEIPT_NUMBER]] от [[PAYMENT.PAID_AT|date]]

Организация: [[AGENCY.NAME]], ИНН [[AGENCY.INN]]
Принято от: [[CLIENT.FIO|gen]]
Основание: оплата по договору № [[DEAL.NUMBER]] от [[DEAL.DATE|date]]
Сумма: [[PAYMENT.AMOUNT|money]] руб. ([[PAYMENT.AMOUNT|words]])
Способ оплаты: [[PAYMENT.METHOD]]
НДС: без НДС (УСН)
[[#IF PAYMENT.FISCAL_RECEIPT]]Фискальный документ (54-ФЗ): [[PAYMENT.FISCAL_RECEIPT]]
[[/IF]]
Кассир ____________________ / [[MANAGER.FIO]] /

- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
КВИТАНЦИЯ к приходному кассовому ордеру № [[PAYMENT.RECEIPT_NUMBER]] от [[PAYMENT.PAID_AT|date]]
Принято от [[CLIENT.FIO|gen]]: [[PAYMENT.AMOUNT|money]] руб. по договору № [[DEAL.NUMBER]].
Кассир ____________________
"""

ACT = """АКТ ОБ ОКАЗАННЫХ УСЛУГАХ
к договору № [[DEAL.NUMBER]] от [[DEAL.DATE|date]]

г. [[AGENCY.CITY]]
[[TODAY|date]]

[[AGENCY.NAME]], далее — «Агент», и [[CLIENT.FIO]], далее — «Заказчик», составили настоящий акт.

1. Туристский продукт: [[TOUR.COUNTRY]], [[TOUR.CITY]], отель [[TOUR.HOTEL]], сроки [[TOUR.DATE_FROM|date]] — [[TOUR.DATE_TO|date]], туристов: [[TOURISTS.COUNT]].
2. Туроператор, сформировавший продукт: [[OPERATOR.FULL_NAME]].
3. Стоимость по договору: [[TOUR.PRICE|money]] руб. ([[TOUR.PRICE|words]]). Оплачено Заказчиком: [[DEAL.PAID_CLIENT|money]] руб.
4. Документы, удостоверяющие право на услуги, переданы Заказчику [[DEAL.HANDOVER_DATE|date]].
5. Услуги по договору оказаны, поездка состоялась в согласованные сроки.
[[#IF DEAL.ACT_NOTES]]6. Замечания Заказчика: [[DEAL.ACT_NOTES]]
[[ELSE]]6. Претензий по объёму, качеству и срокам оказания услуг Заказчик не имеет.
[[/IF]]
Акт составлен в двух экземплярах, по одному для каждой стороны.

Агент: [[AGENCY.SIGNER_ROLE]] ____________________ / [[AGENCY.SIGNER_FIO]] /
Заказчик: ____________________ / [[CLIENT.FIO]] /
"""

TEMPLATES = [
    {"name": "Счёт на оплату", "type": "invoice", "format": "pdf+docx", "body": INVOICE,
     "required_fields": ["PAYMENT.NUMBER", "PAYMENT.AMOUNT", "PAYMENT.DUE_DATE", "DEAL.NUMBER", "DEAL.DATE",
                         "CLIENT.FIO", "CLIENT.PHONE", "AGENCY.NAME", "AGENCY.INN", "AGENCY.ADDRESS",
                         "AGENCY.REQUISITES", "OPERATOR.SHORT_NAME", "TOUR.COUNTRY", "TOUR.HOTEL", "TOURISTS"]},
    {"name": "Приходный кассовый ордер с квитанцией", "type": "cash_receipt", "format": "pdf+docx", "body": CASH_RECEIPT,
     "required_fields": ["PAYMENT.RECEIPT_NUMBER", "PAYMENT.PAID_AT", "PAYMENT.AMOUNT", "PAYMENT.METHOD",
                         "DEAL.NUMBER", "DEAL.DATE", "CLIENT.FIO", "AGENCY.NAME", "AGENCY.INN", "MANAGER.FIO"]},
    {"name": "Акт об оказанных услугах", "type": "act", "format": "pdf+docx", "body": ACT,
     "required_fields": ["DEAL.NUMBER", "DEAL.DATE", "CLIENT.FIO", "TOUR.COUNTRY", "TOUR.HOTEL", "TOUR.DATE_FROM",
                         "TOUR.DATE_TO", "TOUR.PRICE", "DEAL.PAID_CLIENT", "DEAL.HANDOVER_DATE",
                         "OPERATOR.FULL_NAME", "AGENCY.NAME", "AGENCY.SIGNER_FIO", "TOURISTS"]},
]
