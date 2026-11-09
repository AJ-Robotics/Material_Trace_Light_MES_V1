"""
Configuration
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

DATABASE_PATH = DATA_DIR / "mts.db"

PASSWORD_MIN_LENGTH = 4
DEFAULT_BARCODE_SEPARATOR = "|"
LOG_FILE = LOG_DIR / "app.log"