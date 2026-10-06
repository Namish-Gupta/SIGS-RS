"""
Certificate Generator for SIGS-RS Secure MQTT (TLS 1.2+ & mTLS).

Generates:
1. Root CA (ca.crt, ca.key)
2. Broker / Server Cert (server.crt, server.key) with SANs: localhost, mosquitto, 127.0.0.1
3. Client Cert (client.crt, client.key) for X.509 client certificate authentication (SRS REQ-SE-1)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def generate_certificates(output_dir: Path | str | None = None) -> None:
    if output_dir is None:
        target_dir = Path(__file__).parent
    else:
        target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate Root CA
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "SIGS-RS Root CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SIGS-RS Security Grid"),
    ])
    now = datetime.now(timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # Write CA files
    (target_dir / "ca.crt").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (target_dir / "ca.key").write_bytes(
        ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    # 2. Generate Server / Broker Certificate
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SIGS-RS Mosquitto Broker"),
    ])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.DNSName("mosquitto"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    (target_dir / "server.crt").write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    (target_dir / "server.key").write_bytes(
        server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    # 3. Generate Client Certificate (mTLS / X.509 Authentication)
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "sigs-sensor-client"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SIGS-RS Virtual Sensor Grid"),
    ])
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(client_name)
        .issuer_name(ca_name)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    (target_dir / "client.crt").write_bytes(client_cert.public_bytes(serialization.Encoding.PEM))
    (target_dir / "client.key").write_bytes(
        client_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    print(f"Successfully generated certificates in {target_dir}")


if __name__ == "__main__":
    generate_certificates()
