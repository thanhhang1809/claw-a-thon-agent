"""
paths.py — central filesystem layout + .env loader for QE Watchdog.

ROOT là thư mục gốc của app (chứa server.py). Mọi đường dẫn config / snapshot /
data đều suy ra từ đây nên không phụ thuộc module nằm ở folder con nào.
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

CONFIG_DIR    = os.path.join(ROOT, "config")
RULES_YAML    = os.path.join(CONFIG_DIR, "rules.yaml")
SNAPSHOTS_DIR = os.path.join(ROOT, "snapshots")
DATA_DIR      = os.path.join(ROOT, "data")
DB_PATH       = os.path.join(DATA_DIR, "watchdog.db")
UI_INDEX      = os.path.join(ROOT, "ui", "index.html")

# data/ chứa watchdog.db lúc runtime — đảm bảo tồn tại
os.makedirs(DATA_DIR, exist_ok=True)


def snapshot_path(name: str) -> str:
    """Đường dẫn tuyệt đối tới 1 file snapshot trong snapshots/."""
    if os.path.isabs(name):
        return name
    # chấp nhận cả 'ge_sprint_snapshot.json' lẫn 'snapshots/ge_sprint_snapshot.json'
    return os.path.join(SNAPSHOTS_DIR, os.path.basename(name))


def load_env() -> None:
    """Nạp .env cho local dev (prod: runtime tự inject env). Tìm ở ROOT rồi ROOT/.."""
    for candidate in (os.path.join(ROOT, ".env"), os.path.join(ROOT, "..", ".env")):
        if os.path.exists(candidate):
            for line in open(candidate, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break


# Auto-load .env on import
try:
    load_env()
except Exception as e:
    pass  # silently ignore load errors
