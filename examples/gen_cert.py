"""Разовый скрипт — сгенерировать самоподписанный сертификат для dev."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from tls_utils import generate_self_signed_cert


CERT_DIR  = os.path.dirname(os.path.abspath(__file__))
CERT_PATH = os.path.join(CERT_DIR, "server.crt")
KEY_PATH  = os.path.join(CERT_DIR, "server.key")


if __name__ == "__main__":
    generate_self_signed_cert(
        cert_path  = CERT_PATH,
        key_path   = KEY_PATH,
        common_name= "localhost",
        san_hosts  = ["localhost", "127.0.0.1"],
        days_valid = 365,
    )
    print(f"Создан сертификат: {CERT_PATH}")
    print(f"Приватный ключ:    {KEY_PATH}")
