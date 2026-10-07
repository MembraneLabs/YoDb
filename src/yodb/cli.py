"""The ``yodb`` command: look at a catalog, validate it against its databases, explain and run queries.

    yodb catalog  CATALOG                 what can be queried (no database needed)
    yodb validate CATALOG                 inspect the real sources and check the catalog against them
    yodb explain  CATALOG QUERY           the plan: sources, pushdown, read order, strategy
    yodb query    CATALOG QUERY           run it
    yodb mcp      CATALOG                 serve the catalog to an AI agent over MCP (standard input/output)

``CATALOG`` is a directory holding ``datasets.yaml``, ``sources.yaml`` and ``relations.yaml``.
``QUERY`` is JSON text, ``@file.json``, or ``-`` for standard input.  Each catalog ``connection_ref``
is read from the environment variable ``YODB_CONN_<REF>`` (upper-cased, non-alphanumerics as ``_``),
or from ``--connections FILE`` (a YAML or JSON mapping of reference to libpq conninfo).

Exit status: 0 success, 1 a YoDb error (printed with its code), 2 bad usage, 3 an unexpected failure.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import json
import math
from pathlib import Path
import sys
from typing import Any
from uuid import UUID

import yaml

from .catalog import CatalogValidationError, load_catalog
from .client import YoDb
from .errors import YoDbError


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except YoDbError as error:
        _print_error(error)
        return 1
    except CatalogValidationError as error:
        print(f"error [catalog_load_failed]: {error}", file=sys.stderr)
        return 1
    except UsageError as error:
        print(f"yodb: {error}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    except Exception as error:   # noqa: BLE001 - the last line of defence for a command-line tool
        if args.debug:
            raise
        print(f"internal error: {type(error).__name__} (re-run with --debug for the traceback)", file=sys.stderr)
        return 3


class UsageError(Exception):
    pass


# --- commands -----------------------------------------------------------------------------


def _catalog(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    description = {
        name: {
            "description": dataset.description,
            "fields": {
                field: spec.type.value + (" (semantic)" if spec.semantic_eligible else "")
                for field, spec in dataset.fields.items()
            },
            "identity_source": catalog.resolution[name].identity_source,
            "field_sources": dict(catalog.resolution[name].field_sources),
        }
        for name, dataset in catalog.datasets.items()
    }
    if args.json:
        print(json.dumps(description, indent=2))
        return 0
    print(f"catalog {catalog.metadata.name} v{catalog.metadata.version}: {len(description)} dataset(s), {len(catalog.sources)} source(s)")
    for name, item in description.items():
        print(f"\n{name}  - {item['description']}")
        width = max(len(field) for field in item["fields"])
        for field, kind in item["fields"].items():
            print(f"  {field:<{width}}  {kind:<18} from {item['field_sources'].get(field, item['identity_source'])}")
    return 0


def _validate(args: argparse.Namespace) -> int:
    with _open(args) as db:
        status = db.status()
    print(f"catalog {status['catalog']['name']} v{status['catalog']['version']}: active")
    for source, state in status["sources"].items():
        print(f"  {source:<20} {state}")
    return 0


def _explain(args: argparse.Namespace) -> int:
    query = _read_query(args.query)
    with _open(args) as db:
        explanation = db.explain(query)
    if args.json:
        print(json.dumps(_jsonable(explanation), indent=2))
        return 0
    print(f"plan {explanation.plan_kind}   fingerprint {explanation.plan_fingerprint[:12]}")
    for node in explanation.nodes:
        parts = []
        if node.fields:
            parts.append("fields=" + ",".join(node.fields))
        if node.pushed_filter_fields:
            parts.append("pushed=" + ",".join(node.pushed_filter_fields))
        if node.residual_filter:
            parts.append("residual_filter")
        if node.ordering:
            parts.append("order=" + ",".join(node.ordering))
        if node.limit is not None:
            parts.append(f"limit={node.limit}")
        parts.extend(detail for detail in node.detail)
        print(f"  {node.kind:<22} @{node.location:<12} {' '.join(parts)}")
    if explanation.optimizer:
        print("  optimizer: " + " ".join(explanation.optimizer))
    return 0


def _query(args: argparse.Namespace) -> int:
    query = _read_query(args.query)
    with _open(args) as db:
        result = db.query(query, timeout_seconds=args.timeout)
    rows = [dict(row) for row in result.rows]
    if args.format == "json":
        print(json.dumps({"rows": _jsonable(rows), "query_fingerprint": result.query_fingerprint}, indent=2))
    elif args.format == "jsonl":
        for row in rows:
            print(json.dumps(_jsonable(row)))
    else:
        print(_table(rows))
    return 0


def _mcp(args: argparse.Namespace) -> int:
    try:
        from .mcp_server import serve
        import mcp.server.mcpserver  # noqa: F401 - fail here, before the catalog is opened
    except ImportError as error:
        raise UsageError('the MCP server needs the "mcp" package, version 2 or later: pip install "yodb[mcp]"') from error
    with _open(args) as db:
        status = db.status()["catalog"]
        print(f"yodb mcp: serving catalog {status['name']} v{status['version']} on standard input/output", file=sys.stderr)
        serve(db, timeout_seconds=args.timeout)
    return 0


# --- helpers ------------------------------------------------------------------------------


def _open(args: argparse.Namespace) -> YoDb:
    return YoDb.connect(
        args.catalog,
        _connections(args.connections),
        statistics=not args.no_statistics,
        statement_timeout_seconds=args.statement_timeout,
    )


def _connections(path: str | None) -> Mapping[str, str] | None:
    if path is None:
        return None
    try:
        loaded = yaml.safe_load(Path(path).read_text())          # JSON is valid YAML
    except OSError as error:
        raise UsageError(f"cannot read the connections file: {error.strerror}") from error
    except yaml.YAMLError as error:
        raise UsageError("the connections file is not valid YAML or JSON") from error
    if not isinstance(loaded, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in loaded.items()):
        raise UsageError("the connections file must map each connection reference to a conninfo string")
    return loaded


def _read_query(text: str) -> str:
    if text == "-":
        return sys.stdin.read()
    if text.startswith("@"):
        try:
            return Path(text[1:]).read_text()
        except OSError as error:
            raise UsageError(f"cannot read the query file: {error.strerror}") from error
    return text


def _jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, float) and not math.isfinite(value):
        return None                                               # JSON has no NaN or Infinity
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    return value


def _table(rows: list[dict[str, Any]], *, width: int = 48) -> str:
    if not rows:
        return "(0 rows)"
    columns = list(rows[0])
    cells = [[_cell(row.get(column), width) for column in columns] for row in rows]
    sizes = [max(len(column), *(len(line[i]) for line in cells)) for i, column in enumerate(columns)]
    lines = ["  ".join(column.ljust(size) for column, size in zip(columns, sizes))]
    lines.append("  ".join("-" * size for size in sizes))
    lines += ["  ".join(cell.ljust(size) for cell, size in zip(line, sizes)) for line in cells]
    lines.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines)


def _cell(value: Any, width: int) -> str:
    text = "NULL" if value is None else (value.isoformat() if isinstance(value, (datetime, date)) else str(value))
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_error(error: YoDbError) -> None:
    detail = error.detail
    where = f" at {detail.location}" if detail.location else ""
    source = f" (source {detail.source_name})" if detail.source_name else ""
    print(f"error [{detail.code.value}]{where}{source}: {detail.message}", file=sys.stderr)
    for source_name, items in (detail.details.get("sources") or {}).items():
        for item in items:
            print(f"  {source_name}: {item}", file=sys.stderr)
    if detail.retryable:
        print("  (retryable)", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yodb", description="Query a federated, read-only data catalog.")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser, *, connects: bool = True) -> None:
        command.add_argument("catalog", help="directory with datasets.yaml, sources.yaml and relations.yaml")
        command.add_argument("--debug", action="store_true", help="show tracebacks for unexpected failures")
        if connects:
            command.add_argument("--connections", metavar="FILE", help="YAML/JSON mapping of connection reference to conninfo "
                                 "(default: YODB_CONN_<REF> environment variables)")
            command.add_argument("--no-statistics", action="store_true", help="plan with the fixed rules only")
            command.add_argument("--statement-timeout", type=float, default=60.0, metavar="SECONDS",
                                 help="database statement timeout when --timeout is not given (default 60)")

    catalog = commands.add_parser("catalog", help="list the datasets and fields (no database needed)")
    common(catalog, connects=False)
    catalog.add_argument("--json", action="store_true")
    catalog.set_defaults(run=_catalog)

    validate = commands.add_parser("validate", help="check the catalog against the real databases")
    common(validate)
    validate.set_defaults(run=_validate)

    explain = commands.add_parser("explain", help="show the plan for a query without running it")
    common(explain)
    explain.add_argument("query", help="JSON text, @file.json, or - for standard input")
    explain.add_argument("--json", action="store_true")
    explain.set_defaults(run=_explain)

    query = commands.add_parser("query", help="run a query")
    common(query)
    query.add_argument("query", help="JSON text, @file.json, or - for standard input")
    query.add_argument("--format", choices=("table", "json", "jsonl"), default="table")
    query.add_argument("--timeout", type=float, metavar="SECONDS", help="time limit for the whole query")
    query.set_defaults(run=_query)

    mcp = commands.add_parser("mcp", help="serve the catalog to an AI agent over MCP (standard input/output)")
    common(mcp)
    mcp.add_argument("--timeout", type=float, default=60.0, metavar="SECONDS",
                     help="time limit for each query as a whole (default 60)")
    mcp.set_defaults(run=_mcp)
    return parser


if __name__ == "__main__":
    sys.exit(main())
