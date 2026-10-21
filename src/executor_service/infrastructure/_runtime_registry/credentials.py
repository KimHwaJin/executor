"""Encryption and resolution of persisted Runtime credentials."""

from cryptography.fernet import Fernet, InvalidToken

from executor_service.domain.errors import RuntimeTargetConfigurationError


class RuntimeCredentialCipher:
    """Encrypts Runtime Target credentials and Execution kernel secrets."""

    def __init__(self, encryption_key: str) -> None:
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeTargetConfigurationError(
                "RUNTIME_CREDENTIAL_KEY must be a valid Fernet key."
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise RuntimeTargetConfigurationError(
                "Stored Runtime credential cannot be decrypted."
            ) from exc

    def resolve(
        self, credential_ref: str, credential_ciphertext: str | None
    ) -> str:
        if credential_ref == "encrypted:database" and credential_ciphertext:
            return self.decrypt(credential_ciphertext)
        raise RuntimeTargetConfigurationError(
            "Unsupported Runtime Target credential reference."
        )
