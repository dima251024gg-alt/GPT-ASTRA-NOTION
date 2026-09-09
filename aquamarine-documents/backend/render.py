"""Движок шаблонов: подстановка, циклы, условия и фильтры.

Синтаксис:
  [[FIELD]]                          — значение по пути через точку
  [[FIELD|date]]                     — значение с фильтром (можно несколько)
  [[#EACH LIST]]...[[/EACH]]         — цикл; внутри доступны [[INDEX]] и [[ITEM.FIELD]]
  [[#IF FIELD]]...[[ELSE]]...[[/IF]] — условие по непустому значению

Фильтры: date, datetime, money, words, gen, upper, lower, count.
Суммы всегда приходят в копейках.
Склонение ФИО эвристическое: результат всегда можно поправить вручную в карточке сделки.
"""

from .security import AppError

MISSING = object()

ONES = ("", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять",
        "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
        "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать")
TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят",
        "семьдесят", "восемьдесят", "девяносто")
HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот",
            "семьсот", "восемьсот", "девятьсот")
# (единственное, два-четыре, множественное, мужской род)
SCALES = (
    ("", "", "", True),
    ("тысяча", "тысячи", "тысяч", False),
    ("миллион", "миллиона", "миллионов", True),
    ("миллиард", "миллиарда", "миллиардов", True),
)


def plural(number, forms):
    """Выбор формы слова: (рубль, рубля, рублей)."""
    number = abs(int(number)) % 100
    tail = number % 10
    if 10 < number < 20:
        return forms[2]
    if tail == 1:
        return forms[0]
    if 2 <= tail <= 4:
        return forms[1]
    return forms[2]


def _group_words(number, male):
    parts = []
    if number >= 100:
        parts.append(HUNDREDS[number // 100])
        number %= 100
    if number >= 20:
        parts.append(TENS[number // 10])
        number %= 10
    if number:
        word = ONES[number]
        if not male and number == 1:
            word = "одна"
        if not male and number == 2:
            word = "две"
        parts.append(word)
    return parts


def number_to_words(value):
    """Целое число прописью (до миллиардов)."""
    value = int(value)
    if value == 0:
        return "ноль"
    groups = []
    while value > 0:
        groups.append(value % 1000)
        value //= 1000
    if len(groups) > len(SCALES):
        raise AppError("Сумма слишком велика для записи прописью")
    words_out = []
    for index in range(len(groups) - 1, -1, -1):
        chunk = groups[index]
        if not chunk:
            continue
        single, double, many, male = SCALES[index]
        words_out.extend(_group_words(chunk, male))
        if single:
            words_out.append(plural(chunk, (single, double, many)))
    return " ".join(words_out)


def money(kopecks):
    """Копейки -> «1 234,56»."""
    if kopecks in (None, ""):
        return ""
    kopecks = int(kopecks)
    sign = "-" if kopecks < 0 else ""
    rubles, rest = divmod(abs(kopecks), 100)
    return sign + f"{rubles:,}".replace(",", "\u00a0") + f",{rest:02d}"


def money_words(kopecks):
    """Копейки -> «Сто двадцать три рубля 45 копеек»."""
    if kopecks in (None, ""):
        return ""
    kopecks = int(kopecks)
    rubles, rest = divmod(abs(kopecks), 100)
    text = number_to_words(rubles) + " " + plural(rubles, ("рубль", "рубля", "рублей"))
    text += f" {rest:02d} " + plural(rest, ("копейка", "копейки", "копеек"))
    if kopecks < 0:
        text = "минус " + text
    return text[:1].upper() + text[1:]


def date_ru(value):
    """ISO-дата -> ДД.ММ.ГГГГ. Нераспознанное значение возвращается как есть."""
    text = str(value or "").strip()
    if not text:
        return ""
    head = text.replace("/", "-").replace("T", " ").split(" ")[0]
    parts = head.split("-")
    if len(parts) == 3 and len(parts[0]) == 4:
        return f"{parts[2][:2]}.{parts[1][:2]}.{parts[0]}"
    return text


def datetime_ru(value):
    """ISO-дата со временем -> ДД.ММ.ГГГГ ЧЧ:ММ."""
    text = str(value or "").strip()
    if not text:
        return ""
    stamp = text.replace("T", " ")
    date_part = date_ru(stamp)
    time_part = ""
    if " " in stamp:
        time_part = stamp.split(" ", 1)[1][:5]
    return (date_part + " " + time_part).strip()


# --- Склонение ФИО в родительный падеж (эвристика) ---

HUSH = ("к", "г", "х", "ж", "ч", "ш", "щ")
STATIC_TAILS = ("ко", "ых", "их", "ово", "енко", "о", "е", "э", "у", "ю", "ы", "и")
MALE_NAMES_ON_A = ("никита", "илья", "данила", "кузьма", "фома", "лука", "савва",
                    "гаврила", "ерема", "даниила", "антипа")


def _tail(word, add, drop=0):
    stem = word[: len(word) - drop] if drop else word
    if word.isupper():
        add = add.upper()
    return stem + add


def _soft(word):
    """После шипящих и к/г/х пишется «и», иначе «ы»."""
    stem = word[:-1].lower()
    return "и" if stem and stem[-1] in HUSH else "ы"


def detect_gender(fio):
    """Определяет род по отчеству, затем по фамилии и имени."""
    parts = [part for part in str(fio or "").split() if part]
    if not parts:
        return "male"
    if len(parts) >= 3:
        patronymic = parts[2].lower()
        if patronymic.endswith(("овна", "евна", "ична", "инична")):
            return "female"
        if patronymic.endswith(("ович", "евич", "ич")):
            return "male"
    surname = parts[0].lower()
    if surname.endswith(("ова", "ева", "ёва", "ина", "ына", "ская", "цкая", "ая")):
        return "female"
    if surname.endswith(("ов", "ев", "ёв", "ин", "ын", "ский", "цкий")):
        return "male"
    if len(parts) >= 2:
        name = parts[1].lower()
        if name in MALE_NAMES_ON_A:
            return "male"
        if name.endswith(("а", "я")):
            return "female"
    return "male"


def _genitive_surname(word, male):
    if "-" in word:
        return "-".join(_genitive_surname(part, male) for part in word.split("-"))
    low = word.lower()
    if low.endswith(STATIC_TAILS):
        return word
    if male:
        if low.endswith(("ский", "цкий", "ской", "цкой", "ий", "ый", "ой")):
            return _tail(word, "ого", drop=2)
        if low.endswith(("ов", "ев", "ёв", "ин", "ын")):
            return _tail(word, "а")
        if low.endswith("я"):
            return _tail(word, "и", drop=1)
        if low.endswith("а"):
            return _tail(word, _soft(word), drop=1)
        if low.endswith("ь"):
            return _tail(word, "я", drop=1)
        return _tail(word, "а")
    if low.endswith(("ова", "ева", "ёва", "ина", "ына")):
        return _tail(word, "ой", drop=1)
    if low.endswith(("ская", "цкая", "ая")):
        return _tail(word, "ой", drop=2)
    if low.endswith("я"):
        return _tail(word, "и", drop=1)
    if low.endswith("а"):
        return _tail(word, _soft(word), drop=1)
    return word


def _genitive_name(word, male):
    low = word.lower()
    if low.endswith(STATIC_TAILS):
        return word
    if male:
        if low.endswith(("й", "ь", "я")):
            return _tail(word, "я" if low.endswith(("й", "ь")) else "и", drop=1)
        if low.endswith("а"):
            return _tail(word, _soft(word), drop=1)
        return _tail(word, "а")
    if low.endswith("я"):
        return _tail(word, "и", drop=1)
    if low.endswith("а"):
        return _tail(word, _soft(word), drop=1)
    if low.endswith("ь"):
        return _tail(word, "и", drop=1)
    return word


def _genitive_patronymic(word, male):
    low = word.lower()
    if male and low.endswith("ич"):
        return _tail(word, "а")
    if not male and low.endswith("на"):
        return _tail(word, "ы", drop=1)
    return word


def genitive_fio(value, gender=None):
    """«Семёнов Илья Петрович» -> «Семёнова Ильи Петровича»."""
    parts = [part for part in str(value or "").split() if part]
    if not parts:
        return ""
    male = (gender or detect_gender(value)) == "male"
    result = [_genitive_surname(parts[0], male)]
    if len(parts) > 1:
        result.append(_genitive_name(parts[1], male))
    if len(parts) > 2:
        result.append(_genitive_patronymic(parts[2], male))
    result.extend(parts[3:])
    return " ".join(result)


# --- Движок подстановки ---

FILTERS = {
    "date": date_ru,
    "datetime": datetime_ru,
    "money": money,
    "words": money_words,
    "gen": genitive_fio,
    "upper": lambda value: str(value).upper(),
    "lower": lambda value: str(value).lower(),
    "count": lambda value: len(value) if hasattr(value, "__len__") else value,
}

EMPTY = (None, "", [], {}, False)


def resolve(context, path):
    """Значение по пути «DEAL.NUMBER». Отсутствие ключа — MISSING."""
    value = context
    for key in str(path).split("."):
        if not isinstance(value, dict) or key not in value:
            return MISSING
        value = value[key]
    return value


def _next_tag(text, start):
    open_at = text.find("[[", start)
    if open_at < 0:
        return None
    close_at = text.find("]]", open_at)
    if close_at < 0:
        raise AppError("Шаблон повреждён: не закрыт плейсхолдер [[")
    return open_at, close_at + 2, text[open_at + 2 : close_at].strip()


def _match_block(text, start, open_head, close_head, else_head=None):
    """Тело блока с учётом вложенности: (тело, ветка ELSE, позиция после закрытия)."""
    depth = 1
    main = []
    alternate = None
    current = main
    cursor = start
    while True:
        tag = _next_tag(text, cursor)
        if tag is None:
            raise AppError(f"Шаблон повреждён: нет закрывающего тега [[{close_head}]]")
        tag_start, tag_end, token = tag
        head = token.split(" ", 1)[0]
        current.append(text[cursor:tag_start])
        if head == open_head:
            depth += 1
            current.append(text[tag_start:tag_end])
        elif head == close_head:
            depth -= 1
            if depth == 0:
                return "".join(main), ("".join(alternate) if alternate is not None else None), tag_end
            current.append(text[tag_start:tag_end])
        elif else_head and head == else_head and depth == 1:
            alternate = []
            current = alternate
        else:
            current.append(text[tag_start:tag_end])
        cursor = tag_end


def _value(token, context):
    parts = [part.strip() for part in token.split("|")]
    name, filters = parts[0], parts[1:]
    value = context.get("INDEX", "") if name == "INDEX" else resolve(context, name)
    if value is MISSING or value is None:
        value = ""
    for filter_name in filters:
        function = FILTERS.get(filter_name)
        if function is None:
            raise AppError(f"Неизвестный фильтр шаблона: {filter_name}")
        value = function(value)
    return str(value)


def render_text(body, context):
    """Подставляет значения в тело шаблона."""
    out = []
    cursor = 0
    while True:
        tag = _next_tag(body, cursor)
        if tag is None:
            out.append(body[cursor:])
            return "".join(out)
        start, end, token = tag
        out.append(body[cursor:start])
        head = token.split(" ", 1)[0]
        if head == "#EACH":
            name = token[len("#EACH") :].strip()
            inner, _, cursor = _match_block(body, end, "#EACH", "/EACH")
            items = resolve(context, name)
            if items is MISSING or not items:
                items = []
            for index, item in enumerate(items, start=1):
                scope = dict(context)
                scope["INDEX"] = index
                scope["ITEM"] = item if isinstance(item, dict) else {"VALUE": item}
                out.append(render_text(inner, scope))
            continue
        if head == "#IF":
            name = token[len("#IF") :].strip()
            inner, alternate, cursor = _match_block(body, end, "#IF", "/IF", "ELSE")
            value = resolve(context, name)
            filled = value is not MISSING and value not in EMPTY and value != 0
            out.append(render_text(inner if filled else (alternate or ""), context))
            continue
        if head in ("/EACH", "/IF", "ELSE"):
            raise AppError(f"Шаблон повреждён: тег [[{token}]] без открывающего блока")
        out.append(_value(token, context))
        cursor = end


def missing_fields(template, context):
    """Валидация V10: обязательные поля шаблона, которые не заполнены."""
    absent = []
    for field in template.get("required_fields") or []:
        value = resolve(context, field)
        if value is MISSING or value in (None, "", [], {}):
            absent.append(field)
    return absent


def render_template(template, context):
    """Рендерит шаблон и блокирует генерацию при незаполненных полях."""
    absent = missing_fields(template, context)
    if absent:
        raise AppError(
            "Документ «" + str(template.get("name") or template.get("type") or "") + "» нельзя сформировать: не заполнены обязательные поля",
            details={"missing": absent},
        )
    return render_text(template["body"], context)
