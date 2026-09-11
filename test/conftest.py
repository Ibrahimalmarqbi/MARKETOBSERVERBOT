import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Defaults must live in conftest so every test module can import `app` on its own.
os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///test-smoke.db")
