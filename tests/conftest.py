from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shipping_billing.config import load_app_config  # noqa: E402
from shipping_billing.pipeline import ShippingBillingPipeline  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
EXISTING_BILLS = FIXTURES / "existing" / "bills.json"


@pytest.fixture
def config():
    app = load_app_config(ROOT / "config" / "settings.yaml")
    app.settings.accounting_entity.name = "Origin Agent Ltd"
    app.settings.accounting_entity.display_name = "Origin Agent Ltd"
    app.settings.accounting_entity.aliases = ["Origin Agent Ltd"]
    return app


@pytest.fixture
def run_batch(config):
    created: list[ShippingBillingPipeline] = []

    def _run(paths, *, existing_bills=EXISTING_BILLS, use_llm=False, use_ocr=False):
        pipeline = ShippingBillingPipeline(
            config,
            use_llm=use_llm,
            use_ocr=use_ocr,
            existing_bills_path=existing_bills,
        )
        created.append(pipeline)
        try:
            return pipeline.run([str(path) for path in paths])
        finally:
            pipeline.close()

    yield _run
    for pipeline in created:
        pipeline.close()
