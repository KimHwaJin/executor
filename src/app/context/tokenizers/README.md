# Offline context tokenizer

The application counts shared context-budget tokens with tiktoken `cl100k_base`.
These are not the GLM/Qwen server's exact tokens or billing usage.

- Data: https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
- SHA-256: `223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`
- Encoding definition: `tiktoken_ext.openai_public.cl100k_base` from tiktoken.
- License: [MIT](LICENSE.md).

The asset is included as package data; runtime loading performs no network access.
Missing/corrupt data is an error, never a fallback to byte or character estimates.
