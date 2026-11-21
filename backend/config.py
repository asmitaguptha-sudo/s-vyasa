"""
Configuration settings for the Vaada Khata backend.
"""

import os
from pathlib import Path

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = Path(__file__).resolve().parent

# Application settings
APP_NAME = "Vaada Khata Backend"
APP_VERSION = "0.1.0"
APP_ENV = os.getenv("APP_ENV", "development")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

# Database path (SQLite)
DEFAULT_DB_PATH = str(BASE_DIR / "vaada_khata.db")
DATABASE_PATH = os.getenv("DATABASE_PATH", DEFAULT_DB_PATH)
