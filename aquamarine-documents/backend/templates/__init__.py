"""Реестр шаблонов документов и состав пакетов автовыдачи.

Всего 15 шаблонов: требуемые ТЗ 14 плюс отдельный ПКО — счёт на оплату
и приходный кассовый ордер выдаются в разных ситуациях. Каждый шаблон — словарь с ключами:
  name            — название для интерфейса
  type            — код типа (уникален, используется в API и пакетах)
  format          — форматы генерации, всегда "pdf+docx"
  body            — текст с плейсхолдерами [[...]]
  required_fields — поля, без которых генерация запрещена (валидация V10)
"""

from . import changes, closing, consents_basic, consents_special, contract, finance

MODULES = (contract, consents_basic, consents_special, finance, changes, closing)

TEMPLATES = [template for module in MODULES for template in module.TEMPLATES]

BY_TYPE = {template["type"]: template for template in TEMPLATES}

# Пакеты автовыдачи: шаблоны генерируются системой, files — сканы и файлы
# от туроператора, которые должны быть загружены в сделку.
PACKAGES = {
    "on_contract": {
        "title": "Пакет при заключении",
        "templates": ["contract_tour", "application", "memo", "consent_pd", "consent_transfer", "pep_agreement"],
        "files": [],
    },
    "before_departure": {
        "title": "Пакет на вылет",
        "templates": ["memo", "handover"],
        "files": ["ticket", "voucher", "insurance"],
    },
    "accounting": {
        "title": "Пакет для бухгалтерии",
        "templates": ["invoice", "cash_receipt", "act"],
        "files": ["operator_invoice"],
    },
}

# Условные документы: тип шаблона -> признак сделки, при котором он обязателен.
CONDITIONAL = {
    "consent_cross_border": "tour_abroad",
    "consent_minor": "has_minor",
    "power_of_attorney": "has_representative",
}

# Документы, оформляемые по событию, а не в составе пакета.
EVENT_DRIVEN = ("amendment", "cancellation")

CONTROL_TOKENS = ("/EACH", "/IF", "ELSE", "INDEX")


def placeholders(body):
    """Возвращает список токенов [[...]] в порядке появления."""
    found = []
    rest = body
    while "[[" in rest:
        _, rest = rest.split("[[", 1)
        if "]]" not in rest:
            found.append("")
            break
        token, rest = rest.split("]]", 1)
        found.append(token)
    return found


def field_names(body):
    """Имена полей без фильтров и без управляющих конструкций."""
    names = set()
    for token in placeholders(body):
        name = token.split("|")[0].strip()
        for prefix in ("#EACH ", "#IF "):
            if name.startswith(prefix):
                name = name[len(prefix):].strip()
        if not name or name in CONTROL_TOKENS or name.startswith("ITEM."):
            continue
        names.add(name)
    return names


def validate_registry():
    """Проверяет реестр: пустые плейсхолдеры, дубли типов, неиспользуемые поля."""
    problems = []
    seen = set()
    for template in TEMPLATES:
        code = template.get("type", "?")
        if not template.get("body", "").strip():
            problems.append(f"{code}: пустое тело шаблона")
        if code in seen:
            problems.append(f"{code}: дубль типа шаблона")
        seen.add(code)
        if not template.get("name"):
            problems.append(f"{code}: нет названия")
        if template.get("format") != "pdf+docx":
            problems.append(f"{code}: ожидается формат pdf+docx")
        required = template.get("required_fields") or []
        if not required:
            problems.append(f"{code}: не указаны обязательные поля")
        tokens = placeholders(template.get("body", ""))
        if any(not token.strip() for token in tokens):
            problems.append(f"{code}: пустой плейсхолдер [[]]")
        names = field_names(template.get("body", ""))
        for field in required:
            if field not in names and not any(name.startswith(field + ".") for name in names):
                problems.append(f"{code}: обязательное поле {field} не используется в тексте")
    for package, spec in PACKAGES.items():
        for code in spec["templates"]:
            if code not in BY_TYPE:
                problems.append(f"пакет {package}: неизвестный шаблон {code}")
    for code in list(CONDITIONAL) + list(EVENT_DRIVEN):
        if code not in BY_TYPE:
            problems.append(f"условный документ {code}: шаблон не найден")
    return problems


def package_templates(name):
    """Шаблоны пакета в порядке сборки единого PDF."""
    return [BY_TYPE[code] for code in PACKAGES[name]["templates"]]
