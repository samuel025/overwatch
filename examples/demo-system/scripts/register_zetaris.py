"""Register the demo's two Postgres databases in Zetaris (safe to repeat). Needs the ZETARIS_* variables in .env."""
import sys
from pathlib import Path

from overwatch.config import load_dotenv
from overwatch.datalayer import register

load_dotenv(Path(__file__).resolve().parents[3] / ".env")
URL = "postgresql+psycopg://overwatch_ro:overwatch_ro@localhost:15432/"
for line in register({"opsdb": URL + "opsdb", "shopdb": URL + "shopdb"}):
    print(line)
