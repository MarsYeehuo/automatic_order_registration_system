from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_app_config
from .output import write_outputs
from .pipeline import ShippingBillingPipeline
from .schema import build_schema, validate_records
from .utils import read_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shipping-billing",
        description="多格式航运账单解析、匹配与入账决策 Agent",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="处理一批文件并输出 results/audit/summary")
    run.add_argument("inputs", nargs="+", help="文件或目录路径，可多个")
    run.add_argument("--config", default=None, help="settings.yaml 路径")
    run.add_argument("--existing", default=None, help="已有账单 JSON 路径")
    run.add_argument("--output-dir", default="outputs", help="输出目录（默认 outputs）")
    run.add_argument("--no-llm", action="store_true", help="禁用 LangChain/LLM 调用")
    run.add_argument("--no-ocr", action="store_true", help="禁用 OCR 调用")
    run.add_argument("--entity", default=None, help="覆盖记账主体名称")
    run.add_argument(
        "--entity-alias",
        action="append",
        default=[],
        help="为记账主体追加别名，可重复",
    )
    run.add_argument(
        "--non-bill-operation",
        choices=["CREATE", "PENDING_REVIEW"],
        default=None,
        help="非账单文档含有效费用时的操作，默认读取配置",
    )
    run.add_argument("--quiet", action="store_true", help="仅输出最终文件路径")
    run.add_argument(
        "--fail-on-pending",
        action="store_true",
        help="存在 PENDING_REVIEW 记录时以退出码 3 结束",
    )

    validate = sub.add_parser("validate", help="校验 results.json 的结构与业务不变量")
    validate.add_argument("results", help="results.json 路径")

    schema = sub.add_parser("schema", help="输出结果 JSON Schema")
    schema.add_argument("--output", default=None, help="写入文件；缺省打印到标准输出")
    return parser


def _configure(args: argparse.Namespace):
    config = load_app_config(args.config)
    settings = config.settings
    if args.entity:
        settings.accounting_entity.name = args.entity
        if args.entity not in settings.accounting_entity.aliases:
            settings.accounting_entity.aliases.append(args.entity)
    for alias in args.entity_alias:
        if alias and alias not in settings.accounting_entity.aliases:
            settings.accounting_entity.aliases.append(alias)
    if args.non_bill_operation:
        settings.non_bill_operation = args.non_bill_operation
    return config


def _run(args: argparse.Namespace) -> int:
    config = _configure(args)
    pipeline = ShippingBillingPipeline(
        config,
        use_llm=not args.no_llm,
        use_ocr=not args.no_ocr,
        existing_bills_path=args.existing,
    )
    try:
        result = pipeline.run(args.inputs)
    finally:
        pipeline.close()
    outputs = write_outputs(result.records, result.summary, args.output_dir, config)
    if args.quiet:
        print(outputs["results"])
    else:
        summary = result.summary
        print(f"run_id: {result.run_id}")
        print(f"documents: {summary['documents_total']}  records: {summary['records_total']}")
        print("operations: " + ", ".join(
            f"{key}={value}" for key, value in sorted(summary["operations"].items())
        ))
        print("postings: " + ", ".join(
            f"{key}={value}" for key, value in sorted(summary["postings"].items())
        ))
        print(f"results: {outputs['results']}")
        print(f"audit:   {outputs['audit']}")
        print(f"summary: {outputs['summary']}")
    pending = sum(
        1 for record in result.records if record.operation.value == "PENDING_REVIEW"
    )
    if args.fail_on_pending and pending:
        return 3
    return 0


def _validate(args: argparse.Namespace) -> int:
    payload = read_json(args.results)
    errors = validate_records(payload)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"FAILED: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print(f"OK: {len(payload)} record(s) valid")
    return 0


def _schema(args: argparse.Namespace) -> int:
    schema = build_schema()
    text = json.dumps(schema, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(args.output)
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "validate":
        return _validate(args)
    if args.command == "schema":
        return _schema(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
