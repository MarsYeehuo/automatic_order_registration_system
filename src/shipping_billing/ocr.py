from __future__ import annotations

import json
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import requests
from pydantic import BaseModel, ConfigDict, Field

from .config import OCRSettings
from .utils import parse_decimal, sha256_file


class OCRError(RuntimeError):
    pass


class OCRChargeItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    charge_item: str | None = None
    amount: Decimal | None = None
    amount_raw: str | None = None
    currency: str | None = None
    charge_ctr: str | None = None
    correction_note: str | None = None


class OCRContainerItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    ctr_number: str | None = None
    eta: str | None = None
    last_free_date: str | None = None


class OCRItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    file_type: str | None = None
    invoice_number: str | None = None
    mbl_number: str | None = None
    shipper: str | None = None
    consignee: str | None = None
    currency: str | None = None
    containers: list[str] = Field(default_factory=list)
    charges: list[OCRChargeItem] = Field(default_factory=list)
    suspicious: str | None = None
    requires_review: str | None = None
    page_index: list[int] = Field(default_factory=list)
    remark: str | None = None


class OCRResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool
    message: str | None = None
    order_type: str | None = None
    items: list[OCRItem] = Field(default_factory=list)
    error: str | None = None
    duration_seconds: float | None = None
    attempts: int = 1
    cached: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def usable(self) -> bool:
        if not self.success:
            return False
        return any(item.charges or item.invoice_number or item.mbl_number for item in self.items)

    def all_charges(self) -> list[OCRChargeItem]:
        return [charge for item in self.items for charge in item.charges]

    def all_containers(self) -> list[str]:
        seen: list[str] = []
        for item in self.items:
            for number in item.containers:
                if number and number not in seen:
                    seen.append(number)
        return seen

    def invoice_numbers(self) -> list[str]:
        return [i.invoice_number for i in self.items if i.invoice_number]

    def mbl_numbers(self) -> list[str]:
        return [i.mbl_number for i in self.items if i.mbl_number]

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "order_type": self.order_type,
            "items": [
                {
                    "file_type": item.file_type,
                    "invoice_number": item.invoice_number,
                    "mbl_number": item.mbl_number,
                    "shipper": item.shipper,
                    "consignee": item.consignee,
                    "currency": item.currency,
                    "containers": item.containers,
                    "charges": [
                        {
                            "charge_item": charge.charge_item,
                            "amount": str(charge.amount) if charge.amount is not None else None,
                            "currency": charge.currency,
                        }
                        for charge in item.charges
                    ],
                }
                for item in self.items
            ],
        }


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or "file"


class _QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, directory: str = ".", **kwargs: Any) -> None:
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


@dataclass
class TunnelFileHost:
    settings: OCRSettings
    serve_dir: Path
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    _process: subprocess.Popen[str] | None = None
    _public_url: str | None = None
    _error: str | None = None
    _published: dict[str, str] = field(default_factory=dict)

    def start(self) -> None:
        if self._public_url:
            return
        if self._error:
            raise OCRError(self._error)
        self._validate_ssh()
        self.serve_dir.mkdir(parents=True, exist_ok=True)
        port = self._free_port()
        handler = lambda *args, **kwargs: _QuietHandler(  # noqa: E731
            *args, directory=str(self.serve_dir), **kwargs
        )
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        tunnel = self.settings.tunnel
        ssh_bin = tunnel.ssh_bin or shutil.which("ssh") or "ssh"
        command = [
            ssh_bin,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ServerAliveInterval=%d" % max(5, tunnel.keepalive_interval),
            "-o",
            "ExitOnForwardFailure=yes",
            "-R",
            f"80:127.0.0.1:{port}",
            tunnel.remote,
        ]
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        lines: queue.Queue[str] = queue.Queue()

        def pump() -> None:
            assert self._process is not None and self._process.stdout is not None
            for line in self._process.stdout:
                lines.put(line)

        threading.Thread(target=pump, daemon=True).start()
        deadline = time.time() + tunnel.startup_timeout_seconds
        pattern = re.compile(
            r"https://(?!admin\.)[A-Za-z0-9._-]+\.(?:lhr\.life|localhost\.run)"
        )
        while time.time() < deadline:
            timeout = max(0.5, deadline - time.time())
            try:
                line = lines.get(timeout=timeout)
            except queue.Empty:
                break
            match = pattern.search(line)
            if match:
                self._public_url = match.group(0)
                break
            if self._process.poll() is not None:
                break
        if not self._public_url:
            self._error = "无法建立 OCR 文件隧道（localhost.run）"
            self.stop()
            raise OCRError(self._error)

    def _validate_ssh(self) -> None:
        ssh_bin = self.settings.tunnel.ssh_bin or shutil.which("ssh")
        if not ssh_bin:
            self._error = "未找到 ssh 命令，无法建立 OCR 文件隧道；可改用 url_mode=static 或 none"
            raise OCRError(self._error)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def publish(self, path: Path, sha256: str) -> str:
        self.start()
        assert self._public_url is not None
        key = str(path.resolve())
        if key in self._published:
            return self._published[key]
        suffix = path.suffix.lower() or ".bin"
        target_name = f"{sha256[:12]}_{_safe_name(path.name)}"
        target = self.serve_dir / target_name
        if not target.exists() or target.stat().st_size != path.stat().st_size:
            shutil.copyfile(path, target)
        url = self._public_url.rstrip("/") + "/" + urllib.parse.quote(target_name)
        self._published[key] = url
        return url

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        self._public_url = None


@dataclass
class StaticFileHost:
    settings: OCRSettings

    def publish(self, path: Path, sha256: str) -> str:
        if not self.settings.public_base_url:
            raise OCRError("url_mode=static 时必须配置 ocr.public_base_url")
        target_dir = Path(self.settings.public_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = f"{sha256[:12]}_{_safe_name(path.name)}"
        target = target_dir / target_name
        if not target.exists():
            shutil.copyfile(path, target)
        return self.settings.public_base_url.rstrip("/") + "/" + urllib.parse.quote(target_name)


class NoFileHost:
    def publish(self, path: Path, sha256: str) -> str:
        raise OCRError("OCR url_mode=none，未托管文件")

    def stop(self) -> None:
        return

    def start(self) -> None:
        return


def _raw_currency(payload: dict[str, Any]) -> str | None:
    for key in ("currency", "currencyCode", "ccy", "cur"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


class OCRClient:
    def __init__(self, settings: OCRSettings, enabled: bool = True) -> None:
        self.settings = settings
        self.enabled = enabled
        self._host: TunnelFileHost | StaticFileHost | NoFileHost | None = None

    @property
    def host(self) -> TunnelFileHost | StaticFileHost | NoFileHost:
        if self._host is None:
            mode = self.settings.url_mode.lower()
            if mode == "tunnel":
                self._host = TunnelFileHost(self.settings, Path(self.settings.serve_dir))
            elif mode == "static":
                self._host = StaticFileHost(self.settings)
            else:
                self._host = NoFileHost()
        return self._host

    def start(self) -> None:
        if self.enabled and isinstance(self.host, TunnelFileHost):
            self.host.start()

    def close(self) -> None:
        if self._host is not None:
            self._host.stop()
            self._host = None

    def recognize(self, path: str | Path, sha256: str | None = None) -> OCRResult:
        source = Path(path)
        if not self.enabled:
            return OCRResult(success=False, error="OCR disabled")
        size_mb = source.stat().st_size / (1024 * 1024)
        if size_mb > self.settings.max_file_mb:
            return OCRResult(success=False, error=f"文件超过 OCR 上限 {self.settings.max_file_mb}MB")
        digest = sha256 or sha256_file(source)
        cache_dir = Path(self.settings.cache_dir)
        raw_dir = Path(self.settings.raw_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{digest}_{_safe_name(self.settings.task)}.json"
        raw_path = raw_dir / f"{digest}.json"
        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                result = self._parse_response(data, duration=0.0, attempts=0)
                result.cached = True
                return result
            except (json.JSONDecodeError, OSError, ValueError):
                pass

        try:
            file_url = self.host.publish(source, digest)
        except OCRError as exc:
            return OCRResult(success=False, error=str(exc))

        payload = {
            "file": file_url,
            "task": self.settings.task,
            "callBack": self.settings.callback,
            "taskType": self.settings.task_type,
        }
        last_error: str | None = None
        started = time.time()
        attempts = 0
        for attempt in range(self.settings.retries + 1):
            attempts = attempt + 1
            try:
                response = requests.post(
                    self.settings.endpoint,
                    json=payload,
                    timeout=(self.settings.timeout_connect, self.settings.timeout_read),
                )
                if response.status_code >= 500:
                    last_error = f"OCR HTTP {response.status_code}"
                    raise requests.RequestException(last_error)
                response.raise_for_status()
                data = response.json()
                raw_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                cache_path.write_text(
                    json.dumps(data, ensure_ascii=False), encoding="utf-8"
                )
                result = self._parse_response(
                    data, duration=time.time() - started, attempts=attempts
                )
                if result.success:
                    return result
                last_error = result.error or result.message or "OCR 返回失败"
            except (requests.RequestException, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.settings.retries:
                    time.sleep(self.settings.retry_backoff_seconds * (2**attempt))
        return OCRResult(
            success=False,
            error=last_error,
            duration_seconds=time.time() - started,
            attempts=attempts,
        )

    @staticmethod
    def _parse_response(data: dict[str, Any], duration: float, attempts: int) -> OCRResult:
        success = bool(data.get("success"))
        payload = data.get("data") or {}
        remark = str(payload.get("remark") or "")
        message = data.get("message")
        items: list[OCRItem] = []
        for raw_item in payload.get("list") or []:
            if not isinstance(raw_item, dict):
                continue
            item_currency = _raw_currency(raw_item)
            charges: list[OCRChargeItem] = []
            for raw_charge in raw_item.get("chargeList") or []:
                if not isinstance(raw_charge, dict):
                    continue
                raw_amount = str(raw_charge.get("amount") or "").strip()
                correction = None
                corrected = raw_amount
                if re.search(r"[A-Za-z]", raw_amount):
                    corrected = re.sub(r"[Oo]", "0", corrected)
                    corrected = re.sub(r"[Il|]", "1", corrected)
                    if corrected != raw_amount:
                        correction = f"{raw_amount} -> {corrected}"
                amount = parse_decimal(corrected)
                charges.append(
                    OCRChargeItem(
                        charge_item=(raw_charge.get("chargeItem") or None),
                        amount=amount,
                        amount_raw=raw_amount or None,
                        currency=_raw_currency(raw_charge) or item_currency,
                        charge_ctr=(raw_charge.get("chargeCtr") or None),
                        correction_note=correction,
                    )
                )
            containers = [
                str(ctr.get("ctrNumber"))
                for ctr in raw_item.get("ctrList") or []
                if isinstance(ctr, dict) and ctr.get("ctrNumber")
            ]
            items.append(
                OCRItem(
                    file_type=(raw_item.get("fileType") or None),
                    invoice_number=(raw_item.get("invoiceNumber") or None),
                    mbl_number=(raw_item.get("mblNumber") or None),
                    shipper=(raw_item.get("shipper") or None),
                    consignee=(raw_item.get("consignee") or None),
                    currency=item_currency,
                    containers=containers,
                    charges=charges,
                    suspicious=(raw_item.get("suspicious") or None),
                    requires_review=(raw_item.get("requiresReview") or None),
                    page_index=[int(x) for x in raw_item.get("page_index") or [] if isinstance(x, int)],
                    remark=(raw_item.get("remark") or None),
                )
            )
        error = None
        if not success:
            error = message or "OCR 返回 success=false"
        elif "处理失败" in remark:
            success = False
            error = remark
        return OCRResult(
            success=success,
            message=message,
            order_type=payload.get("orderType"),
            items=items,
            error=error,
            duration_seconds=duration,
            attempts=attempts,
            raw=data,
        )
