from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ProjectSettings(BaseModel):
    name: str = "shipping-billing-agent"
    rule_version: str = "1.0.0"
    prompt_version: str = "1.0.0"


class AccountingEntitySettings(BaseModel):
    name: str | None = None
    display_name: str | None = None
    role_hint: str | None = None
    aliases: list[str] = Field(default_factory=list)

    def all_names(self) -> list[str]:
        values = [self.name, self.display_name, self.role_hint, *self.aliases]
        out: list[str] = []
        for value in values:
            if value and value.strip() and value not in out:
                out.append(value.strip())
        return out


class TunnelSettings(BaseModel):
    provider: str = "localhostrun"
    ssh_bin: str | None = None
    remote: str = "nokey@localhost.run"
    startup_timeout_seconds: int = 30
    keepalive_interval: int = 15


class OCRSettings(BaseModel):
    endpoint: str
    task: str = "orderChange"
    task_type: str = ""
    callback: str = ""
    timeout_connect: int = 10
    timeout_read: int = 300
    retries: int = 2
    retry_backoff_seconds: int = 3
    url_mode: str = "tunnel"
    public_base_url: str | None = None
    public_dir: str = "outputs/_public"
    serve_dir: str = "outputs/_tmp"
    max_file_mb: int = 60
    cache_dir: str = "outputs/ocr_cache"
    raw_dir: str = "outputs/ocr_raw"
    tunnel: TunnelSettings = Field(default_factory=TunnelSettings)


class LLMSettings(BaseModel):
    enabled: bool = True
    key_file: str | None = None
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    temperature: float = 0
    max_tokens: int = 4096
    timeout_seconds: int = 120
    retries: int = 1
    cache_dir: str = "outputs/llm_cache"
    max_input_chars: int = 14000


class ParsingSettings(BaseModel):
    max_eml_depth: int = 3
    max_attachment_mb: int = 60
    inline_images: str = "ignore"
    ocr_all_pdfs: bool = True
    min_pdf_text_chars: int = 80
    date_locale: str = "US"
    supported_extensions: list[str] = Field(default_factory=list)


class ValidationSettings(BaseModel):
    amount_tolerance: float = 0.01
    total_tolerance: float = 0.02
    unit_price_tolerance: float = 0.02
    min_charge_confidence: float = 0.45
    min_auto_operation_confidence: float = 0.70
    suspicious_character_correction: bool = True
    allow_zero_amount: bool = False
    max_future_days: int = 730


class MatchingSettings(BaseModel):
    charge_name_fuzzy_threshold: float = 0.86
    party_fuzzy_threshold: float = 0.82
    date_tolerance_days: int = 0
    amount_tolerance: float = 0.01
    rule_order: list[str] = Field(default_factory=list)


class OutputSettings(BaseModel):
    extended: bool = False
    results_file: str = "results.json"
    audit_file: str = "audit.jsonl"
    summary_file: str = "summary.json"
    include_raw_text_in_audit: bool = True
    redact_sensitive: bool = True
    ensure_ascii: bool = False


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")

    project: ProjectSettings = Field(default_factory=ProjectSettings)
    accounting_entity: AccountingEntitySettings = Field(default_factory=AccountingEntitySettings)
    entity_overrides: dict[str, Any] = Field(default_factory=dict)
    non_bill_operation: str = "CREATE"
    ocr: OCRSettings
    llm: LLMSettings = Field(default_factory=LLMSettings)
    parsing: ParsingSettings = Field(default_factory=ParsingSettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    matching: MatchingSettings = Field(default_factory=MatchingSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)

    root: Path = Field(default=Path("."), exclude=True)

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (self.root / path)

    def entity_for(self, source_file: str) -> AccountingEntitySettings:
        override = self.entity_overrides.get(source_file)
        if not override:
            return self.accounting_entity
        merged = self.accounting_entity.model_dump()
        if isinstance(override, str):
            merged["name"] = override
            merged["aliases"] = list(merged.get("aliases") or []) + [override]
        elif isinstance(override, dict):
            merged.update(override)
        return AccountingEntitySettings(**merged)


@dataclass
class ChargeAlias:
    canonical: str
    aliases: list[str]


@dataclass
class ChargeDictionary:
    version: str
    entries: list[ChargeAlias]

    def canonical_names(self) -> list[str]:
        return [entry.canonical for entry in self.entries]

    def alias_pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for entry in self.entries:
            for alias in entry.aliases:
                pairs.append((alias, entry.canonical))
        return pairs


@dataclass
class PaymentTermDictionary:
    version: str
    terms: dict[str, list[str]]
    display: dict[str, str] = field(default_factory=dict)
    column_tokens: dict[str, str] = field(default_factory=dict)


@dataclass
class MatchingRules:
    version: str
    rules: list[dict[str, Any]]
    charge_row_disambiguation: list[str]
    update_whitelist: list[str]
    immutable_fields: list[str]
    no_change_amount_tolerance: float


@dataclass
class ArApRules:
    version: str
    resolution_order: list[dict[str, Any]]
    direction_hints: dict[str, list[str]]


@dataclass
class LLMCredentials:
    provider: str
    base_url: str | None
    model: str | None
    api_key: str | None


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuration file must contain a mapping: {path}")
    return data


def load_settings(config_path: str | Path | None = None) -> Settings:
    if config_path is None:
        env_path = os.environ.get("SHIPPING_BILLING_CONFIG")
        config_path = env_path or Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    path = Path(config_path).resolve()
    data = _read_yaml(path)
    return Settings(**data, root=path.parent.parent)


def load_charge_dictionary(path: str | Path) -> ChargeDictionary:
    data = _read_yaml(Path(path))
    entries = [
        ChargeAlias(canonical=item["canonical"], aliases=[str(a) for a in item.get("aliases", [])])
        for item in data.get("charges", [])
    ]
    return ChargeDictionary(version=str(data.get("version", "1.0.0")), entries=entries)


def load_payment_terms(path: str | Path) -> PaymentTermDictionary:
    data = _read_yaml(Path(path))
    return PaymentTermDictionary(
        version=str(data.get("version", "1.0.0")),
        terms={str(k): [str(v) for v in values] for k, values in (data.get("terms") or {}).items()},
        display={str(k): str(v) for k, v in (data.get("display") or {}).items()},
        column_tokens={str(k): str(v) for k, v in (data.get("column_tokens") or {}).items()},
    )


def load_matching_rules(path: str | Path) -> MatchingRules:
    data = _read_yaml(Path(path))
    return MatchingRules(
        version=str(data.get("version", "1.0.0")),
        rules=list(data.get("rules") or []),
        charge_row_disambiguation=list(data.get("charge_row_disambiguation") or []),
        update_whitelist=list(data.get("update_whitelist") or []),
        immutable_fields=list(data.get("immutable_fields") or []),
        no_change_amount_tolerance=float(data.get("no_change_amount_tolerance", 0.01)),
    )


def load_ar_ap_rules(path: str | Path) -> ArApRules:
    data = _read_yaml(Path(path))
    self_hints = data.get("direction_hints") or {}
    return ArApRules(
        version=str(data.get("version", "1.0.0")),
        resolution_order=list(data.get("resolution_order") or []),
        direction_hints={
            "creditor": [str(x) for x in self_hints.get("creditor", [])],
            "debtor": [str(x) for x in self_hints.get("debtor", [])],
        },
    )


_KEY_FIELD_PATTERN = re.compile(
    r"^(?:api[_\s-]*key|key|密钥)\s*[:：=]\s*(.+)$",
    re.IGNORECASE,
)
_CONFIG_FIELD_PATTERN = re.compile(
    r"^(base\s*url|base_url|anthropic[_ ]base[_ ]url|api\s*base|供应商|provider|模型名字|模型|model)\s*[:：=]\s*(.+)$",
    re.IGNORECASE,
)


def parse_key_file(path: str | Path) -> LLMCredentials:
    """Parse either a JSON credential file or the loosely formatted template_key.txt."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            provider = str(data.get("provider") or data.get("供应商") or "")
            return LLMCredentials(
                provider=provider.lower(),
                base_url=data.get("base_url") or data.get("base url") or data.get("api_base"),
                model=data.get("model") or data.get("模型名字"),
                api_key=data.get("api_key") or data.get("key"),
            )
        except json.JSONDecodeError:
            pass

    values: dict[str, str] = {}
    api_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key_match = _KEY_FIELD_PATTERN.match(line)
        if key_match:
            api_key = key_match.group(1).strip()
            continue
        config_match = _CONFIG_FIELD_PATTERN.match(line)
        if config_match:
            name = config_match.group(1).lower().replace(" ", "")
            values[name] = config_match.group(2).strip()
            continue
        if line.lower().startswith(("sk-", "sk_")):
            api_key = line
    provider = (values.get("供应商") or values.get("provider") or "").lower()
    base_url = (
        values.get("baseurl")
        or values.get("base_url")
        or values.get("anthropic_base_url")
        or values.get("apibase")
    )
    model = values.get("模型名字") or values.get("模型") or values.get("model")
    if not provider:
        provider = "anthropic" if base_url and "anthropic" in base_url.lower() else "openai"
    return LLMCredentials(provider=provider, base_url=base_url, model=model, api_key=api_key)


def find_key_file(settings: Settings) -> Path | None:
    candidates: list[Path] = []
    env_path = os.environ.get("SHIPPING_BILLING_KEY_FILE")
    if env_path:
        candidates.append(Path(env_path))
    if settings.llm.key_file:
        candidates.append(settings.resolve_path(settings.llm.key_file))
    candidates.extend(
        [
            settings.root / "template_key.txt",
            settings.root.parent / "template_key.txt",
            Path.cwd() / "template_key.txt",
            Path.cwd().parent / "template_key.txt",
        ]
    )
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    return None


def load_llm_credentials(settings: Settings) -> LLMCredentials:
    key_file = find_key_file(settings)
    parsed = parse_key_file(key_file) if key_file else LLMCredentials("", None, None, None)
    provider = settings.llm.provider or parsed.provider or "anthropic"
    base_url = settings.llm.base_url or parsed.base_url
    model = settings.llm.model or parsed.model
    return LLMCredentials(
        provider=provider.lower(),
        base_url=base_url,
        model=model,
        api_key=parsed.api_key,
    )


@dataclass
class AppConfig:
    settings: Settings
    charge_dictionary: ChargeDictionary
    payment_terms: PaymentTermDictionary
    matching_rules: MatchingRules
    ar_ap_rules: ArApRules
    llm_credentials: LLMCredentials

    @property
    def root(self) -> Path:
        return self.settings.root


def load_app_config(config_path: str | Path | None = None) -> AppConfig:
    settings = load_settings(config_path)
    root = settings.root
    return AppConfig(
        settings=settings,
        charge_dictionary=load_charge_dictionary(root / "config" / "charge_aliases.yaml"),
        payment_terms=load_payment_terms(root / "config" / "payment_terms.yaml"),
        matching_rules=load_matching_rules(root / "config" / "matching_rules.yaml"),
        ar_ap_rules=load_ar_ap_rules(root / "config" / "ar_ap_rules.yaml"),
        llm_credentials=load_llm_credentials(settings),
    )
