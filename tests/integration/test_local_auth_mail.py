"""SMTP transport test with a local STARTTLS server and synthetic message only."""

import asyncio
import ipaddress
import ssl
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from huipi_cloud.modules.identity import mail


@pytest.mark.anyio
async def test_recovery_email_uses_starttls_and_fragment_link(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(certfile, keyfile)
    client_tls = ssl.create_default_context(cafile=str(certfile))
    received = []

    async def handle(reader, writer):
        writer.write(b"220 synthetic.local ESMTP\r\n")
        await writer.drain()
        while line := await reader.readline():
            command = line.upper()
            if command.startswith(b"EHLO"):
                writer.write(b"250-synthetic.local\r\n250-STARTTLS\r\n250 SIZE 40960\r\n")
            elif command.startswith(b"STARTTLS"):
                writer.write(b"220 Ready for TLS\r\n")
                await writer.drain()
                await writer.start_tls(server_tls)
                continue
            elif command.startswith((b"MAIL", b"RCPT")):
                writer.write(b"250 OK\r\n")
            elif command.startswith(b"DATA"):
                writer.write(b"354 End data with <CRLF>.<CRLF>\r\n")
                await writer.drain()
                data = bytearray()
                while (part := await reader.readline()) != b".\r\n":
                    data.extend(part)
                received.append(bytes(data))
                writer.write(b"250 queued\r\n")
            elif command.startswith(b"QUIT"):
                writer.write(b"221 bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"250 OK\r\n")
            await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(mail.settings, "auth_public_base_url", "https://huipi.example.invalid")
    monkeypatch.setattr(mail.settings, "auth_smtp_host", "127.0.0.1")
    monkeypatch.setattr(mail.settings, "auth_smtp_port", port)
    monkeypatch.setattr(mail.settings, "auth_smtp_sender", "noreply@example.invalid")
    monkeypatch.setattr(mail.settings, "auth_smtp_user", "")
    monkeypatch.setattr(mail.settings, "auth_smtp_password", None)
    monkeypatch.setattr(mail.ssl, "create_default_context", lambda: client_tls)
    try:
        assert mail.configured()
        await mail.send("teacher@example.invalid", "synthetic-token-only", "reset_password")
    finally:
        server.close()
        await server.wait_closed()
    message = BytesParser(policy=policy.default).parsebytes(received[0])
    assert message["To"] == "teacher@example.invalid"
    assert (
        "https://huipi.example.invalid/reset-password#token=synthetic-token-only"
        in message.get_content()
    )
