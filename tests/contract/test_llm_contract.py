from __future__ import annotations

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from shipping_billing.config import LLMCredentials, parse_key_file
from shipping_billing.extraction.llm_extract import merge_llm_payload
from shipping_billing.llm import LLMClient, parse_json_payload
from shipping_billing.models import (
    BillCandidate,
    ContentBlock,
    ExtractionMethod,
    FieldEvidence,
    NormalizedDocument,
    SourceRef,
    SourceType,
)
from shipping_billing.normalization import ChargeNormalizer, PaymentTermNormalizer


def test_parse_json_payload_plain_object():
    assert parse_json_payload('{"charges": []}') == {"charges": []}


def test_parse_json_payload_markdown_fence():
    text = '```json\n{"amount": "10.00"}\n```'
    assert parse_json_payload(text) == {"amount": "10.00"}


def test_parse_json_payload_with_surrounding_prose():
    text = 'Here is the result:\n{"ok": true}\nHope this helps.'
    assert parse_json_payload(text) == {"ok": True}


def test_parse_json_payload_array():
    assert parse_json_payload("[1, 2]") == [1, 2]


def test_parse_json_payload_repairs_raw_newlines_and_trailing_comma():
    text = '{"note": "line one\nline two", "charges": [],}'
    assert parse_json_payload(text) == {"note": "line one line two", "charges": []}


def test_parse_json_payload_invalid_returns_none():
    assert parse_json_payload("not json at all") is None
    assert parse_json_payload("") is None
    assert parse_json_payload(None) is None


def test_client_unavailable_without_key(config):
    settings = config.settings.llm.model_copy(update={"enabled": True})
    client = LLMClient(settings, LLMCredentials("openai", None, "test-model", None))
    assert not client.available
    response = client.complete_json("system", "user")
    assert response.data is None
    assert "密钥" in (response.error or "")


def test_client_unavailable_when_disabled(config):
    settings = config.settings.llm.model_copy(update={"enabled": False})
    client = LLMClient(
        settings, LLMCredentials("openai", None, "test-model", "sk-test")
    )
    assert not client.available
    assert "disabled" in (client.disabled_reason or "")


def test_parse_key_file_json(tmp_path):
    path = tmp_path / "key.json"
    path.write_text(
        '{"provider": "openai", "base_url": "https://example.test/v1", '
        '"model": "test-model", "api_key": "sk-test"}',
        encoding="utf-8",
    )
    credentials = parse_key_file(path)
    assert credentials.provider == "openai"
    assert credentials.base_url == "https://example.test/v1"
    assert credentials.model == "test-model"
    assert credentials.api_key == "sk-test"


def test_parse_key_file_chinese_template(tmp_path):
    path = tmp_path / "template_key.txt"
    path.write_text(
        "\n".join(
            [
                "供应商：openai",
                "模型名字：test-model",
                "base_url: https://example.test/v1",
                "api_key: sk-test-123",
            ]
        ),
        encoding="utf-8",
    )
    credentials = parse_key_file(path)
    assert credentials.provider == "openai"
    assert credentials.model == "test-model"
    assert credentials.base_url == "https://example.test/v1"
    assert credentials.api_key == "sk-test-123"


def test_system_prompt_json_braces_are_literal(config, tmp_path):
    settings = config.settings.llm.model_copy(
        update={"enabled": True, "cache_dir": str(tmp_path / "cache")}
    )
    client = LLMClient(
        settings, LLMCredentials("anthropic", None, "test-model", "sk-test")
    )
    assert client.available
    client._model = RunnableLambda(
        lambda _: AIMessage(content='{"document_kind": "BILL"}')
    )
    response = client.complete_json(
        'Return JSON like {"document_kind": "BILL"}',
        "document text",
        cache_tag="brace-test",
    )
    assert response.error is None
    assert response.data == {"document_kind": "BILL"}


def test_parse_key_file_bare_key_line(tmp_path):
    path = tmp_path / "template_key.txt"
    path.write_text("sk-bare-key\n", encoding="utf-8")
    credentials = parse_key_file(path)
    assert credentials.api_key == "sk-bare-key"
    assert credentials.provider == "openai"


def test_llm_duplicate_recipient_is_ignored(config):
    text = "NEPTUNE SHIPPING LIMITED\nGENERAL NOTICE\n"
    doc = NormalizedDocument(
        document_id="doc_llm_party",
        source=SourceRef(
            source_file="notice.txt",
            source_type=SourceType.TXT,
            sha256="a" * 64,
            location="file",
        ),
        blocks=[ContentBlock(kind="text", location="file", text=text)],
    )
    candidate = BillCandidate(
        issuer="NEPTUNE SHIPPING LIMITED",
        evidence=[
            FieldEvidence(
                field="issuer",
                source_file="notice.txt",
                location="file",
                raw_text="NEPTUNE SHIPPING LIMITED",
                method=ExtractionMethod.RULE,
            )
        ],
    )
    payload = {
        "issuer": "NEPTUNE SHIPPING LIMITED",
        "recipient": "NEPTUNE SHIPPING LIMITED",
        "field_evidence": {
            "issuer": "NEPTUNE SHIPPING LIMITED",
            "recipient": "NEPTUNE SHIPPING LIMITED",
        },
    }
    merged = merge_llm_payload(
        candidate,
        doc,
        payload,
        ChargeNormalizer(config.charge_dictionary),
        PaymentTermNormalizer(config.payment_terms),
    )
    assert merged.issuer == "NEPTUNE SHIPPING LIMITED"
    assert merged.recipient is None
    assert not any(
        item.field == "recipient" for item in merged.evidence
    )
    assert any("接收方" in note for note in merged.notes)
