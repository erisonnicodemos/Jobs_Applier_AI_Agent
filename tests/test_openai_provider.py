"""Direct OpenAI provider integration, with no live network calls or applications."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI, RateLimitError

from aihawk.apply import server_parameters
from aihawk.config import ConfigError, load_config
from aihawk.database import Job
from aihawk.llm import account_problem, cause, explain, make_client
from aihawk.scoring import OPENAI_MAX_POSTINGS, fits
from aihawk.session import Session


def test_openai_reads_its_own_key_and_default_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "aihawk.yaml").write_text("provider: openai\n", encoding="utf-8")
    (tmp_path / "key").write_text("sk-orca-old", encoding="utf-8")
    monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    config = load_config(tmp_path)
    assert config.is_openai
    assert config.api_key() == "sk-openai-test"
    assert config.key_env == "OPENAI_API_KEY"
    assert config.key_file == tmp_path / "openai-key"
    assert config.model == "gpt-4.1-mini"
    assert config.quick_model == "gpt-4.1-mini"
    assert config.decision_model == "gpt-4o-mini"
    client = make_client(config)
    assert str(client.base_url).rstrip("/") == "https://api.openai.com/v1"
    asyncio.run(client.close())


def test_openai_saved_key_never_reuses_orcarouter_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "aihawk.yaml").write_text("provider: openai\n", encoding="utf-8")
    (tmp_path / "key").write_text("sk-orca-old", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = load_config(tmp_path)
    assert config.api_key() == ""
    config.save_key("sk-openai-saved")
    assert config.api_key() == "sk-openai-saved"
    assert (tmp_path / "key").read_text(encoding="utf-8") == "sk-orca-old"


def test_orcarouter_still_default_and_env_can_choose_openai(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIHAWK_AI_PROVIDER", raising=False)
    monkeypatch.delenv("ORCAROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = load_config(tmp_path)
    assert config.provider == "orcarouter"
    assert config.base_url == "https://api.orcarouter.ai/v1"
    assert config.key_env == "ORCAROUTER_API_KEY"
    monkeypatch.setenv("AIHAWK_AI_PROVIDER", "openai")
    assert load_config(tmp_path).is_openai


def test_invalid_provider_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIHAWK_AI_PROVIDER", raising=False)
    (tmp_path / "aihawk.yaml").write_text("provider: invented\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="provider"):
        load_config(tmp_path)


def test_openai_has_no_orcarouter_balance_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIHAWK_AI_PROVIDER", "openai")
    session = Session(tmp_path)
    try:
        def must_not_be_called(key: str) -> Any:
            raise AssertionError("OpenAI must not call the OrcaRouter balance endpoint")

        session.key_client = must_not_be_called  # type: ignore[method-assign]
        assert asyncio.run(session.credit_left("sk-openai-test")) is None
        assert asyncio.run(session.has_no_credit("sk-openai-test")) is False
    finally:
        session.close()


def test_browser_never_receives_either_ai_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-orca-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    env = server_parameters(tmp_path, tmp_path).env
    assert env is not None
    assert "ORCAROUTER_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_openai_scoring_uses_chat_completions_not_systemone() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-openai-test"
        payload = json.loads(request.content)
        seen.append(payload)
        schema = payload["response_format"]["json_schema"]["schema"]
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert schema["additionalProperties"] is False
        values = {key: 0.8 for key in schema["required"]}
        return httpx.Response(200, json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o-mini",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(values)},
            }],
            "usage": {"prompt_tokens": 120, "completion_tokens": 35, "total_tokens": 155},
        })

    async def run() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = AsyncOpenAI(base_url="https://api.openai.com/v1", api_key="sk-openai-test", http_client=http_client)
            jobs = [
                Job(f"id-{i}", "Example", f"Senior Engineer {i}", "Remote Brazil", "url", "apply_url", "Backend development")
                for i in range(OPENAI_MAX_POSTINGS + 1)
            ]
            return await fits(client, "gpt-4o-mini", "Senior engineer CV", jobs, "Remote, eligible in Brazil", lambda reason: None, provider="openai")

    values = asyncio.run(run())
    assert values == [0.8] * (OPENAI_MAX_POSTINGS + 1)
    assert len(seen) == 2
    assert len(seen[0]["response_format"]["json_schema"]["schema"]["required"]) == OPENAI_MAX_POSTINGS
    assert len(seen[1]["response_format"]["json_schema"]["schema"]["required"]) == 1


def test_openai_insufficient_quota_is_not_treated_as_a_transient_429() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    error = RateLimitError(
        "quota exhausted",
        response=httpx.Response(429, request=request),
        body={"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}},
    )
    assert cause(error) == "no_credit"
    assert account_problem(error)
    assert "OpenAI API quota" in explain(error)
