"""
Утилиты для TLS.

1. generate_self_signed_cert() — для разработки/тестов
2. make_server_ssl_context() — SSL контекст сервера
3. make_client_ssl_context() — SSL контекст клиента
"""

import os
import ssl
import datetime
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_self_signed_cert(
    cert_path: str,
    key_path:  str,
    common_name: str = "localhost",
    days_valid:  int = 365,
    san_hosts:   list = None,
) -> None:
    """
    Генерируем RSA-2048 ключ + самоподписанный сертификат.
    Для разработки. В продакшне — используй Let's Encrypt.
    """
    san_hosts = san_hosts or [common_name, "127.0.0.1", "localhost"]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Olegshifter Dev"),
    ])

    san_list = []
    for host in san_hosts:
        try:
            import ipaddress
            ip = ipaddress.ip_address(host)
            san_list.append(x509.IPAddress(ip))
        except ValueError:
            san_list.append(x509.DNSName(host))

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    Path(cert_path).parent.mkdir(parents=True, exist_ok=True)

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))


def make_server_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """SSL контекст для сервера — только TLS 1.2+ с современными шифрами."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # Только AEAD cipher suites
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
    return ctx


def make_client_ssl_context(
    verify_ca:       Optional[str] = None,
    insecure:        bool = False,
    server_hostname: Optional[str] = None,
) -> ssl.SSLContext:
    """
    SSL контекст для клиента.
    - verify_ca=None + insecure=False → системные CA (для Let's Encrypt и пр.)
    - verify_ca="path.pem" → доверяем конкретному CA
    - insecure=True → не проверяем вообще (только для dev!)
    """
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif verify_ca:
        ctx.load_verify_locations(verify_ca)

    return ctx
