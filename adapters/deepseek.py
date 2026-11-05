"""Bounded DeepSeek HTTPS calls; credentials stay in the trusted Windows controller."""

import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ModelServiceError(ValueError):
    """Public code only, never server bodies, request headers or credential values."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelServiceError("model_redirect_refused")


def load_deepseek_key(encrypted_path: Path) -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    if os.name != "nt" or not encrypted_path.is_file():
        raise ModelServiceError("model_credential_missing")
    # DPAPI is bound to the current Windows user. The child sends only into this
    # captured pipe; its plaintext output is never forwarded to tool/UI logs.
    escaped = str(encrypted_path.resolve()).replace("'", "''")
    code = (
        "$s=Import-Clixml -LiteralPath '"
        + escaped
        + "'; $p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); try {[Console]::Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($p))} finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code], capture_output=True, timeout=10
    )
    if result.returncode or not result.stdout:
        raise ModelServiceError("model_credential_decryption_failed")
    return result.stdout.decode("utf-8").strip()


class DeepSeekClient:
    endpoint = "https://api.deepseek.com"

    def __init__(
        self,
        key: str,
        *,
        model="deepseek-flash",
        max_calls=8,
        max_tokens=2048,
        deadline_seconds=600,
        stop=None,
        base_url: str | None = None,
    ):
        if not 1 <= max_calls <= 20 or not 1 <= max_tokens <= 4096 or not 1 <= deadline_seconds <= 900:
            raise ValueError("bounded_model_limits_required")
        self._key, self.model, self.max_calls, self.max_tokens = key, model, max_calls, max_tokens
        self.endpoint = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or type(self).endpoint).rstrip("/")
        self.calls, self.total_tokens = 0, 0
        self.started = time.monotonic()
        self.deadline_seconds = deadline_seconds
        self.usage: list[dict] = []
        self.stop = stop
        self.opener = build_opener(NoRedirect())

    def request(self, route: str, payload: dict | None = None):
        if route not in {"/models", "/chat/completions"}:
            raise ModelServiceError("model_route_refused")
        remaining = self.deadline_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise ModelServiceError("model_run_limit")
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = Request(
            self.endpoint + route,
            data=data,
            headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=min(60, remaining)) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ModelServiceError("model_response_limit")
                return json.loads(raw)
        except HTTPError as exc:
            raise ModelServiceError("model_http_" + str(exc.code)) from None
        except json.JSONDecodeError:
            raise ModelServiceError("model_response_invalid") from None
        except (URLError, OSError):
            raise ModelServiceError("model_transport_failed") from None

    def available_models(self):
        data = self.request("/models")
        return [entry["id"] for entry in data.get("data", []) if isinstance(entry.get("id"), str)]

    def query(self, messages):
        if self.calls >= self.max_calls or time.monotonic() - self.started >= self.deadline_seconds:
            raise ModelServiceError("model_run_limit")
        self.calls += 1  # Unknown transport outcomes retain the reservation; no automatic retry.
        data = self.request(
            "/chat/completions",
            {
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": self.max_tokens,
                "stream": False,
                "response_format": {"type": "json_object"},
                "thinking": {"type": "disabled"},
                **({"stop": self.stop} if self.stop else {}),
            },
        )
        if not isinstance(data, dict):
            raise ModelServiceError("model_response_invalid")
        usage = data.get("usage", {})
        choices = data.get("choices")
        if (
            not isinstance(usage, dict)
            or not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], dict)
            or not isinstance(choices[0].get("message"), dict)
            or not isinstance(choices[0]["message"].get("content"), str)
            or ("total_tokens" in usage and type(usage["total_tokens"]) is not int)
        ):
            raise ModelServiceError("model_response_invalid")
        total_tokens = usage.get("total_tokens", 0)
        self.total_tokens += total_tokens
        self.usage.append(
            {
                "call_index": self.calls,
                "response_model": data.get("model"),
                "usage": usage,
                "finish_reason": choices[0].get("finish_reason"),
            }
        )
        return choices[0]["message"]["content"]
