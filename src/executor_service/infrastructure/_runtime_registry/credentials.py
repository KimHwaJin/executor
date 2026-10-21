"""Encryption and resolution of persisted Runtime credentials."""

from cryptography.fernet import Fernet, InvalidToken

from executor_service.domain.errors import RuntimeTargetConfigurationError


class RuntimeCredentialCipher:
    def __init__(self, encryption_key: str) -> None:
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeTargetConfigurationError(
                "RUNTIME_CREDENTIAL_KEY must be a valid Fernet key."
            ) from exc

    def encrypt(self, credential: str) -> str:
        return self._fernet.encrypt(credential.encode()).decode("ascii")

    def resolve(
        self, credential_ref: str, credential_ciphertext: str | None
    ) -> str:
        if credential_ref == "encrypted:database" and credential_ciphertext:
            try:
                return self._fernet.decrypt(
                    credential_ciphertext.encode("ascii")
                ).decode()
            except (InvalidToken, UnicodeDecodeError) as exc:
                raise RuntimeTargetConfigurationError(
                    "Stored Runtime Target credential cannot be decrypted."
                ) from exc
        raise RuntimeTargetConfigurationError(
            "Unsupported Runtime Target credential reference."
        )
