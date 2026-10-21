from __future__ import annotations

import json
from decimal import Decimal

from shipping_billing.ocr import OCRClient
from shipping_billing.utils import sha256_file


def sample_response() -> dict:
    return {
        "success": True,
        "message": "ok",
        "data": {
            "orderType": "orderChange",
            "remark": "",
            "list": [
                {
                    "fileType": "debitNote",
                    "invoiceNumber": "INV-1",
                    "mblNumber": "OOLU4055331281",
                    "shipper": "Shipper Ltd",
                    "consignee": "Origin Agent Ltd",
                    "currency": "USD",
                    "ctrList": [{"ctrNumber": "TIIU8975975"}],
                    "chargeList": [
                        {
                            "chargeItem": "Handling Fee",
                            "amount": "1O.50",
                            "currency": "USD",
                            "chargeCtr": "TIIU8975975",
                        },
                        {"chargeItem": "THC", "amount": "65.00"},
                    ],
                    "page_index": [1],
                }
            ],
        },
    }


def test_parse_response_maps_charges_and_containers(config):
    result = OCRClient._parse_response(sample_response(), duration=0.1, attempts=1)
    assert result.success
    assert result.usable
    assert result.invoice_numbers() == ["INV-1"]
    assert result.mbl_numbers() == ["OOLU4055331281"]
    assert result.all_containers() == ["TIIU8975975"]
    charges = result.all_charges()
    assert [charge.charge_item for charge in charges] == ["Handling Fee", "THC"]
    assert charges[0].amount == Decimal("10.50")
    assert charges[0].amount_raw == "1O.50"
    assert charges[0].currency == "USD"
    assert charges[0].correction_note == "1O.50 -> 10.50"
    assert charges[1].amount == Decimal("65.00")
    assert charges[1].currency == "USD"
    assert charges[1].correction_note is None
    assert result.prompt_payload()["items"][0]["currency"] == "USD"
    assert result.prompt_payload()["items"][0]["charges"][1]["currency"] == "USD"


def test_parse_response_failure():
    result = OCRClient._parse_response(
        {"success": False, "message": "bad file"}, duration=0.1, attempts=1
    )
    assert not result.success
    assert result.error == "bad file"
    assert not result.usable


def test_parse_response_remark_failure():
    data = sample_response()
    data["data"]["remark"] = "处理失败：图片不清晰"
    result = OCRClient._parse_response(data, duration=0.1, attempts=1)
    assert not result.success
    assert "处理失败" in (result.error or "")


def test_disabled_client_does_not_call_network(config, tmp_path):
    client = OCRClient(config.settings.ocr, enabled=False)
    path = tmp_path / "bill.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    result = client.recognize(path)
    assert not result.success
    assert result.error == "OCR disabled"


def test_cached_response_is_used_without_network(config, tmp_path):
    cache_dir = tmp_path / "cache"
    raw_dir = tmp_path / "raw"
    cache_dir.mkdir()
    settings = config.settings.ocr.model_copy(
        update={
            "cache_dir": str(cache_dir),
            "raw_dir": str(raw_dir),
            "url_mode": "none",
        }
    )
    path = tmp_path / "bill.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    digest = sha256_file(path)
    (cache_dir / f"{digest}_orderChange.json").write_text(
        json.dumps(sample_response()), encoding="utf-8"
    )
    client = OCRClient(settings, enabled=True)
    result = client.recognize(path, digest)
    assert result.success
    assert result.cached
    assert result.all_charges()[0].amount == Decimal("10.50")


def test_missing_host_returns_error(config, tmp_path):
    settings = config.settings.ocr.model_copy(
        update={
            "cache_dir": str(tmp_path / "cache"),
            "raw_dir": str(tmp_path / "raw"),
            "url_mode": "none",
        }
    )
    path = tmp_path / "bill.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    client = OCRClient(settings, enabled=True)
    result = client.recognize(path)
    assert not result.success
    assert "未托管文件" in (result.error or "")
