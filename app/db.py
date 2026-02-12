from pathlib import Path

from sqlmodel import create_engine

DB_PATH = Path(__file__).resolve().parent / "incidents.db"
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
