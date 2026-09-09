"""Изменение и прекращение договора: допсоглашение и аннуляция.

Тексты — редактируемые проекты, не юридическое заключение.
"""

AMENDMENT = """ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ № [[AMENDMENT.NUMBER]]
к договору № [[DEAL.NUMBER]] от [[DEAL.DATE|date]]

г. [[AGENCY.CITY]]
[[TODAY|date]]

[[AGENCY.NAME]], далее — «Агент», и [[CLIENT.FIO]], далее — «Заказчик», заключили настоящее дополнительное соглашение.

1. Причина изменения: [[AMENDMENT.REASON]].
2. Стороны согласовали следующие изменения условий договора:
[[#EACH AMENDMENT.CHANGES]][[INDEX]]. [[ITEM.FIELD]]: было — [[ITEM.OLD]]; стало — [[ITEM.NEW]]
[[/EACH]]3. Стоимость: было [[AMENDMENT.PRICE_OLD|money]] руб., стало [[AMENDMENT.PRICE_NEW|money]] руб. ([[AMENDMENT.PRICE_NEW|words]]).
[[#IF AMENDMENT.EXTRA_PAYMENT]]4. Доплата [[AMENDMENT.EXTRA_PAYMENT|money]] руб. вносится Заказчиком до [[AMENDMENT.EXTRA_DEADLINE|date]].
[[ELSE]]4. Возврат [[AMENDMENT.REFUND|money]] руб. производится в течение 10 рабочих дней на реквизиты, указанные Заказчиком.
[[/IF]]5. Фактически понесённые расходы, связанные с изменением, составляют [[AMENDMENT.ACTUAL_COSTS|money]] руб. и подтверждены документами туроператора.
6. При замене туриста Заказчик подтверждает, что новый турист предоставил согласие на обработку персональных данных отдельным документом.
7. Остальные условия договора остаются без изменений. Соглашение является неотъемлемой частью договора и вступает в силу с момента подписания.

Агент: [[AGENCY.SIGNER_ROLE]] ____________________ / [[AGENCY.SIGNER_FIO]] /
Заказчик: ____________________ / [[CLIENT.FIO]] /
"""

CANCELLATION = """СОГЛАШЕНИЕ О РАСТОРЖЕНИИ ДОГОВОРА (АННУЛЯЦИЯ) № [[CANCELLATION.NUMBER]]
к договору № [[DEAL.NUMBER]] от [[DEAL.DATE|date]]

г. [[AGENCY.CITY]]
[[TODAY|date]]

[[AGENCY.NAME]], далее — «Агент», и [[CLIENT.FIO]], далее — «Заказчик», расторгают договор на следующих условиях.

1. Основание: [[CANCELLATION.REASON]]. Дата заявления Заказчика: [[CANCELLATION.REQUEST_DATE|date]].
2. Бронирование № [[DEAL.BOOKING_NUMBER]] у туроператора [[OPERATOR.SHORT_NAME]] аннулировано [[CANCELLATION.CANCELLED_AT|date]].
3. Расчёт по договору:
   Оплачено Заказчиком: [[DEAL.PAID_CLIENT|money]] руб.
   Фактически понесённые расходы, подтверждённые документами туроператора и Агента:
[[#EACH CANCELLATION.COSTS]][[INDEX]]. [[ITEM.NAME]] — [[ITEM.AMOUNT|money]] руб. (основание: [[ITEM.EVIDENCE]])
[[/EACH]]   Итого удержано: [[CANCELLATION.WITHHELD|money]] руб.
   К возврату Заказчику: [[CANCELLATION.REFUND|money]] руб. ([[CANCELLATION.REFUND|words]])
4. Возврат производится в течение 10 рабочих дней с даты подписания по реквизитам: [[CANCELLATION.REFUND_DETAILS]].
[[#IF CANCELLATION.INSURANCE_NOTE]]5. Страхование от невыезда: [[CANCELLATION.INSURANCE_NOTE]]
[[/IF]]6. Обязательства сторон прекращаются с даты подписания соглашения, за исключением расчётов по пункту 3.
7. Персональные данные хранятся до истечения установленных законом сроков; отзыв согласия оформляется отдельным заявлением через Кабинет туриста или в офисе.

Агент: ____________________ / [[AGENCY.SIGNER_FIO]] /
Заказчик: ____________________ / [[CLIENT.FIO]] /
"""

TEMPLATES = [
    {"name": "Дополнительное соглашение к договору", "type": "amendment", "format": "pdf+docx", "body": AMENDMENT,
     "required_fields": ["AMENDMENT.NUMBER", "AMENDMENT.REASON", "AMENDMENT.CHANGES", "AMENDMENT.PRICE_OLD",
                         "AMENDMENT.PRICE_NEW", "DEAL.NUMBER", "DEAL.DATE", "CLIENT.FIO", "AGENCY.NAME",
                         "AGENCY.CITY", "AGENCY.SIGNER_FIO"]},
    {"name": "Соглашение о расторжении (аннуляция)", "type": "cancellation", "format": "pdf+docx", "body": CANCELLATION,
     "required_fields": ["CANCELLATION.NUMBER", "CANCELLATION.REASON", "CANCELLATION.REQUEST_DATE",
                         "CANCELLATION.CANCELLED_AT", "CANCELLATION.COSTS", "CANCELLATION.WITHHELD",
                         "CANCELLATION.REFUND", "CANCELLATION.REFUND_DETAILS", "DEAL.NUMBER", "DEAL.DATE",
                         "DEAL.PAID_CLIENT", "DEAL.BOOKING_NUMBER", "CLIENT.FIO", "OPERATOR.SHORT_NAME",
                         "AGENCY.NAME", "AGENCY.CITY", "AGENCY.SIGNER_FIO"]},
]
