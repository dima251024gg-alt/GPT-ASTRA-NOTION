"""Configuration: no shared production secrets or permissive production defaults."""
import base64, os, secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODE = os.getenv("APP_ENV", "development")
PRODUCTION = MODE == "production"
VAR = Path(os.getenv("DATA_DIR", str(ROOT / "var")))
VAR.mkdir(parents=True, exist_ok=True, mode=0o700)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///" + str(VAR / "aquamarine.sqlite3"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")
REGION = os.getenv("DATA_REGION", "RU")

def key(name):
    value = os.getenv(name)
    if not value:
        if PRODUCTION:
            raise RuntimeError("Не задан обязательный секрет: " + name)
        path = VAR / (name.lower() + ".key")
        if not path.exists():
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f: f.write(base64.b64encode(secrets.token_bytes(32)).decode())
            except FileExistsError: pass
        value = path.read_text().strip()
    raw = base64.b64decode(value, validate=True)
    if len(raw) != 32: raise RuntimeError(name + ": требуется ключ длиной 32 байта в Base64")
    return raw

DATA_KEY = key("DATA_KEY")
TOKEN_KEY = key("TOKEN_KEY")
AUDIT_KEY = key("AUDIT_KEY")
SESSION_MINUTES = 30
MAX_UPLOAD = 20 * 1024 * 1024
RETENTION_YEARS = int(os.getenv("RETENTION_YEARS", "3"))
PACKAGE_DAYS = int(os.getenv("PACKAGE_DAYS", "3"))
PROVIDER_MODE = os.getenv("PROVIDER_MODE", "mock")
if PRODUCTION:
    if REGION != "RU" or not PUBLIC_URL.startswith("https://"):
        raise RuntimeError("Для production обязательны DATA_REGION=RU и HTTPS")
    if not DATABASE_URL.startswith("postgresql://"):
        raise RuntimeError("Production требует PostgreSQL")
    if PROVIDER_MODE == "mock": raise RuntimeError("Тестовые провайдеры запрещены в production")
    if os.getenv("LEGAL_APPROVED") != "true" or os.getenv("INFRA_RU_ATTESTED") != "true":
        raise RuntimeError("Подтвердите правовую проверку и размещение инфраструктуры в РФ")
