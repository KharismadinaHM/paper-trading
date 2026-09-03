"""
Logging setup menggunakan modul logging standar Python.
Mengirim output ke Console (StreamHandler) dan File (FileHandler / RotatingFileHandler)
dengan format: timestamp + level + module name + message.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Format standar: timestamp + level + module name + message
DEFAULT_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
    log_format: str = DEFAULT_LOG_FORMAT,
    date_format: str = DEFAULT_DATE_FORMAT,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB per log file
    backup_count: int = 5,
) -> logging.Logger:
    """
    Inisialisasi root logger aplikasi.
    - Console output (stdout)
    - File output dengan rotasi log otomatis
    """
    # 1. Tentukan log level
    level_str = (log_level or os.getenv("LOG_LEVEL", "INFO")).upper()
    numeric_level = getattr(logging, level_str, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Hindari duplikasi handler jika setup_logging dipanggil lebih dari sekali
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    formatter = logging.Formatter(fmt=log_format, datefmt=date_format)

    # 2. Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 3. File Handler (opsional jika path file ditentukan)
    file_path_str = log_file or os.getenv("LOG_FILE", "logs/app.log")
    if file_path_str:
        log_path = Path(file_path_str).resolve()
        # Otomatis buat parent folder jika belum ada
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    Helper untuk mengambil named logger di setiap modul/file aplikasi.
    Contoh: logger = get_logger(__name__)
    """
    return logging.getLogger(name)
