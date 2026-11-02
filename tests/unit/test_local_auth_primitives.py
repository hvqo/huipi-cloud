"""Deterministic checks for password and recovery configuration boundaries."""

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.identity import local_auth, mail
from huipi_cloud.modules.identity.local_admin import AccountCreate, PasswordResetByAdmin
from huipi_cloud.modules.identity.local_http import PasswordChangeBody, ResetPasswordBody


@pytest.mark.anyio
@pytest.mark.parametrize("password", ["123456", "abcdef", "!@#$%^", "中🚀文🚀字🚀"])
async def test_six_character_password_accepts_any_character_type(password):
    local_auth.validate_password(password)
    assert AccountCreate(
        request_id="00000000-0000-0000-0000-000000000001",
        account="teacher@example.invalid",
        display_name="教师",
        password=password,
    )
    assert PasswordResetByAdmin(
        request_id="00000000-0000-0000-0000-000000000001", password=password
    )
    assert PasswordChangeBody(current_password="old", new_password=password)
    assert ResetPasswordBody(token="a" * 43, password=password)
    encoded = await local_auth.hash_password(password)
    assert await local_auth.verify_password(encoded, password)


def test_password_shorter_than_six_is_rejected():
    with pytest.raises(ValueError):
        local_auth.validate_password("12345")
    with pytest.raises(ValidationError):
        AccountCreate(
            request_id="00000000-0000-0000-0000-000000000001",
            account="teacher@example.invalid",
            display_name="教师",
            password="12345",
        )
    with pytest.raises(ValidationError):
        PasswordResetByAdmin(request_id="00000000-0000-0000-0000-000000000001", password="12345")
    with pytest.raises(ValidationError):
        PasswordChangeBody(current_password="old", new_password="12345")
    with pytest.raises(ValidationError):
        ResetPasswordBody(token="a" * 43, password="12345")


@pytest.mark.anyio
async def test_invalid_long_password_never_matches_internal_dummy_replacement():
    encoded = await local_auth.hash_password("invalid-input-too-long")
    assert await local_auth.verify_password(encoded, "invalid-input-too-long")
    assert not await local_auth.verify_password(encoded, "x" * 129)
    assert not await local_auth.verify_password(encoded, "\ud800")


def test_account_normalization_supports_email_phone_and_rejects_invalid_unicode():
    assert local_auth.normalize_login(" Teacher@Example.Invalid ") == "teacher@example.invalid"
    assert local_auth.normalize_login("+8613800138000") == "+8613800138000"
    with pytest.raises(ValueError):
        local_auth.normalize_login("broken\ud800@example.invalid")


def test_recovery_mail_requires_fixed_https_or_development_loopback(monkeypatch):
    monkeypatch.setattr(mail.settings, "auth_smtp_host", "smtp.example.invalid")
    monkeypatch.setattr(mail.settings, "auth_smtp_sender", "sender@example.invalid")
    monkeypatch.setattr(mail.settings, "auth_smtp_user", "")
    monkeypatch.setattr(mail.settings, "auth_smtp_password", None)
    monkeypatch.setattr(mail.settings, "app_env", "production")
    monkeypatch.setattr(mail.settings, "auth_public_base_url", "http://example.invalid")
    assert not mail.configured()
    monkeypatch.setattr(
        mail.settings, "auth_public_base_url", "https://example.invalid/?attacker=x"
    )
    assert not mail.configured()
    monkeypatch.setattr(mail.settings, "auth_public_base_url", "https://example.invalid")
    assert mail.configured()
