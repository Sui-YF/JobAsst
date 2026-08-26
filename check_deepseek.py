"""Minimal connectivity check. Never prints the API key."""

from deepseek_client import _json_call, is_configured


if not is_configured():
    raise SystemExit("DeepSeek API Key is not configured")

result = _json_call(
    'Return JSON only in this shape: {"status": "ok"}.',
    {"task": "connectivity_test"},
)
print("DeepSeek connectivity:", result.get("status", "unexpected response"))
