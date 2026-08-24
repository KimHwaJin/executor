"""Runtime credential helpers for database-backed test targets."""

from cryptography.fernet import Fernet

TEST_RUNTIME_CREDENTIAL_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def runtime_credential_fields(
    credential: str = "test-token",
) -> dict[str, str]:
    """Return the same encrypted credential columns produced by Runtime Target registration."""

    ciphertext = (
        Fernet(TEST_RUNTIME_CREDENTIAL_KEY.encode())
        .encrypt(credential.encode())
        .decode()
    )
    return {
        "credential_ref": "encrypted:database",
        "credential_ciphertext": ciphertext,
    }
