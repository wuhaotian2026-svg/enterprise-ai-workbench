from __future__ import annotations

from policy_api.auth.passwords import hash_password, verify_password


def test_hash_password_uses_argon2id_and_verifies_the_original_password() -> None:
    password = "correct horse battery staple"

    password_hash = hash_password(password)

    assert password_hash.startswith("$argon2id$")
    assert password not in password_hash
    assert verify_password(password, password_hash) is True


def test_verify_password_rejects_an_incorrect_password_without_raising() -> None:
    password_hash = hash_password("correct password")

    assert verify_password("incorrect password", password_hash) is False


def test_password_hash_repr_does_not_expose_the_plaintext_password() -> None:
    password = "repr-must-not-leak-this-password"

    password_hash = hash_password(password)

    assert password not in repr(password_hash)
