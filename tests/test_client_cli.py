"""The front door (``yodb.connect``) and the ``yodb`` command, against fake sources."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from yodb import cli
from yodb.client import YoDb, _activation_failure, _as_query, _resolver
from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.connections import EnvPostgresConnectionResolver, MappingPostgresConnectionResolver, PostgresConnectionSettings
from yodb.errors import ErrorCode, QueryError, SourceConnectionError
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine

from support.catalogs import SourceRowsExecutor, StaticRuntime
from support.tickets import ACTIVE, DATASETS, HELPDESK, RELATIONS, SOURCES, prio

QUERY = {"from": {"dataset": "ticket"}, "select": ["subject", "priority"], "where": prio(3), "page": {"first": 3}}


def open_db() -> YoDb:
    runtime = StaticRuntime(ACTIVE)
    engine = QueryExecutionEngine(
        runtime,
        QueryCompilerRegistry([PostgresQueryCompiler()]),
        QueryExecutionAdapterRegistry([SourceRowsExecutor({"helpdesk": HELPDESK[:3], "directory": ()})]),
    )
    return YoDb(runtime, engine)


def run(argv, *, db=None, stdin=""):
    """Run the command with ``YoDb.connect`` replaced by ``db`` (or a failing one); return (status, stdout, stderr)."""

    out, err = io.StringIO(), io.StringIO()
    patches = [mock.patch("sys.stdin", io.StringIO(stdin))]
    if db is not None:
        patches.append(mock.patch.object(YoDb, "connect", classmethod(lambda cls, *a, **k: db)))
    with redirect_stdout(out), redirect_stderr(err):
        for patch in patches:
            patch.start()
        try:
            status = cli.main(argv)
        finally:
            for patch in reversed(patches):
                patch.stop()
    return status, out.getvalue(), err.getvalue()


class ClientTests(unittest.TestCase):
    def test_a_query_runs_from_a_dictionary_or_from_json_text(self) -> None:
        db = open_db()
        by_dict = db.query(QUERY)
        by_text = db.query(json.dumps(QUERY))
        self.assertEqual([dict(r) for r in by_dict.rows], [dict(r) for r in by_text.rows])
        self.assertEqual(len(by_dict.rows), 3)

    def test_text_that_is_not_json_is_a_query_error_with_the_position(self) -> None:
        with self.assertRaises(QueryError) as caught:
            _as_query('{"from": ')
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_SHAPE_INVALID)
        self.assertIn("not valid JSON", caught.exception.detail.message)

    def test_json_text_that_cannot_be_read_safely_is_a_query_error_not_a_crash(self) -> None:
        for text in ('{"x": ' + "9" * 100_000 + "}", "[" * 100_000, "\x00\x01"):
            with self.assertRaises(QueryError) as caught:
                _as_query(text)
            self.assertEqual(caught.exception.code, ErrorCode.QUERY_SHAPE_INVALID)

    def test_explain_returns_the_plan_without_reading_anything(self) -> None:
        db = open_db()
        explanation = db.explain(QUERY)
        self.assertIn("remote_scan", [node.kind for node in explanation.nodes])

    def test_describe_lists_datasets_and_fields_with_types_and_marks_semantic_fields(self) -> None:
        described = open_db().describe()
        self.assertEqual(described["ticket"]["fields"]["priority"], {"type": "int"})
        self.assertEqual(described["ticket"]["fields"]["body"], {"type": "text", "semantic": True})

    def test_status_names_the_catalog_and_every_source(self) -> None:
        status = open_db().status()
        self.assertEqual(status["catalog"]["name"], "tickets")
        self.assertEqual(status["sources"], {"helpdesk": "valid", "directory": "valid"})

    def test_closing_releases_the_pool_once_and_a_with_block_closes(self) -> None:
        closed = []
        db = YoDb(StaticRuntime(ACTIVE), open_db()._engine, close=lambda: closed.append(1))
        with db:
            pass
        db.close()
        self.assertEqual(closed, [1])


class ActivationFailureTests(unittest.TestCase):
    @staticmethod
    def result(**sources):
        from types import SimpleNamespace as N

        return N(candidate=N(sources=sources))

    def test_a_catalog_that_does_not_match_and_a_source_that_cannot_be_reached_are_told_apart(self) -> None:
        from types import SimpleNamespace as N

        mismatch = N(error=None, validation=N(is_valid=False, findings=[N(severity=N(value="error"), message="column 'x' is missing")]))
        down = N(error=N(code=ErrorCode.SOURCE_UNAVAILABLE, message="could not be reached"), validation=None)
        detail = _activation_failure(self.result(crm=mismatch, billing=down, support=N(error=None, validation=N(is_valid=True, findings=[]))))
        self.assertEqual(detail.code, ErrorCode.SOURCE_VALIDATION_FAILED)
        self.assertEqual(detail.message, "Cannot open the catalog: the catalog does not match crm; could not inspect billing.")
        self.assertEqual(detail.details["sources"]["crm"], ["error: column 'x' is missing"])
        self.assertEqual(detail.details["sources"]["billing"], ["source_unavailable: could not be reached"])
        self.assertNotIn("support", detail.details["sources"])

    def test_a_catalog_that_never_loaded_points_at_the_command_that_says_why(self) -> None:
        from types import SimpleNamespace as N

        detail = _activation_failure(N(candidate=None))
        self.assertEqual(detail.code, ErrorCode.CATALOG_LOAD_FAILED)
        self.assertIn("yodb catalog", detail.message)


class ConnectionResolverTests(unittest.TestCase):
    def test_a_reference_is_read_from_an_upper_cased_environment_variable(self) -> None:
        resolver = EnvPostgresConnectionResolver(environ={"YODB_CONN_E2E_RECORDS": "host=db dbname=x"})
        self.assertEqual(resolver.variable_for("e2e-records"), "YODB_CONN_E2E_RECORDS")
        self.assertEqual(resolver.resolve("e2e-records"), PostgresConnectionSettings(conninfo="host=db dbname=x"))

    def test_a_missing_or_empty_variable_names_what_to_set_and_never_echoes_a_value(self) -> None:
        for environ in ({}, {"YODB_CONN_CRM": ""}):
            with self.assertRaises(SourceConnectionError) as caught:
                EnvPostgresConnectionResolver(environ=environ).resolve("crm")
            self.assertEqual(caught.exception.code, ErrorCode.CONNECTION_REFERENCE_NOT_FOUND)
            self.assertIn("YODB_CONN_CRM", caught.exception.detail.message)

    def test_connections_may_be_none_a_mapping_of_strings_or_a_resolver(self) -> None:
        self.assertIsInstance(_resolver(None), EnvPostgresConnectionResolver)
        mapped = _resolver({"crm": "host=a"})
        self.assertEqual(mapped.resolve("crm").conninfo, "host=a")
        own = MappingPostgresConnectionResolver({"crm": PostgresConnectionSettings(conninfo="host=b")})
        self.assertIs(_resolver(own), own)


class CommandTests(unittest.TestCase):
    def test_catalog_lists_datasets_fields_types_and_where_each_comes_from_without_a_database(self) -> None:
        with TemporaryDirectory() as directory:
            for name, text in (("datasets", DATASETS), ("sources", SOURCES), ("relations", RELATIONS)):
                (Path(directory) / f"{name}.yaml").write_text(text)
            status, out, _ = run(["catalog", directory])
            self.assertEqual(status, 0)
            self.assertIn("ticket", out)
            self.assertIn("text (semantic)", out)
            self.assertIn("from directory", out)               # owner lives in the second source
            status, out, _ = run(["catalog", directory, "--json"])
            self.assertEqual(json.loads(out)["ticket"]["fields"]["priority"], "int")

    def test_a_broken_catalog_is_reported_with_its_code_and_exit_status_1(self) -> None:
        with TemporaryDirectory() as directory:
            status, _, err = run(["catalog", directory])
            self.assertEqual(status, 1)
            self.assertIn("catalog_load_failed", err)

    def test_query_prints_a_table_by_default(self) -> None:
        status, out, _ = run(["query", "cat", json.dumps(QUERY)], db=open_db())
        self.assertEqual(status, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["id", "subject", "priority"])
        self.assertTrue(set(lines[1]) <= {"-", " "})
        self.assertEqual(lines[-1], "(3 rows)")

    def test_query_can_print_json_and_json_lines(self) -> None:
        _, out, _ = run(["query", "cat", json.dumps(QUERY), "--format", "json"], db=open_db())
        payload = json.loads(out)
        self.assertEqual(len(payload["rows"]), 3)
        self.assertEqual(len(payload["query_fingerprint"]), 64)
        _, out, _ = run(["query", "cat", json.dumps(QUERY), "--format", "jsonl"], db=open_db())
        self.assertEqual([json.loads(line)["subject"] for line in out.splitlines()], ["S1", "S2", "S3"])

    def test_the_query_may_come_from_a_file_or_standard_input(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "q.json"
            path.write_text(json.dumps(QUERY))
            self.assertEqual(run(["query", "cat", f"@{path}"], db=open_db())[0], 0)
        status, out, _ = run(["query", "cat", "-"], db=open_db(), stdin=json.dumps(QUERY))
        self.assertEqual(status, 0)
        self.assertIn("(3 rows)", out)

    def test_an_empty_result_says_so(self) -> None:
        empty = {**QUERY, "where": {"field": "priority", "op": "gt", "value": 99}}
        runtime = StaticRuntime(ACTIVE)
        db = YoDb(runtime, QueryExecutionEngine(
            runtime, QueryCompilerRegistry([PostgresQueryCompiler()]),
            QueryExecutionAdapterRegistry([SourceRowsExecutor({"helpdesk": (), "directory": ()})])))
        self.assertEqual(run(["query", "cat", json.dumps(empty)], db=db)[1].strip(), "(0 rows)")

    def test_a_yodb_error_prints_its_code_and_location_and_exits_1(self) -> None:
        bad = {**QUERY, "select": ["nope"]}
        status, out, err = run(["query", "cat", json.dumps(bad)], db=open_db())
        self.assertEqual((status, out), (1, ""))
        self.assertIn("error [field_not_found]", err)

    def test_invalid_json_is_an_error_not_a_crash(self) -> None:
        status, _, err = run(["query", "cat", "{not json"], db=open_db())
        self.assertEqual(status, 1)
        self.assertIn("query_shape_invalid", err)

    def test_explain_prints_the_plan_nodes(self) -> None:
        status, out, _ = run(["explain", "cat", json.dumps(QUERY)], db=open_db())
        self.assertEqual(status, 0)
        self.assertIn("remote_scan", out)
        _, out, _ = run(["explain", "cat", json.dumps(QUERY), "--json"], db=open_db())
        self.assertIn("nodes", json.loads(out))

    def test_validate_lists_every_source(self) -> None:
        status, out, _ = run(["validate", "cat"], db=open_db())
        self.assertEqual(status, 0)
        self.assertIn("helpdesk", out)
        self.assertIn("valid", out)

    def test_bad_usage_exits_2_and_unexpected_failures_exit_3_without_a_traceback(self) -> None:
        status, _, err = run(["query", "cat", "@/definitely/not/here.json"], db=open_db())
        self.assertEqual((status, "cannot read the query file" in err), (2, True))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "conn.yaml"
            path.write_text("- not\n- a mapping\n")
            status, _, err = run(["validate", "cat", "--connections", str(path)])
            self.assertEqual(status, 2)
        broken = open_db()
        with mock.patch.object(broken, "query", side_effect=ZeroDivisionError("boom")):
            status, _, err = run(["query", "cat", json.dumps(QUERY)], db=broken)
        self.assertEqual(status, 3)
        self.assertIn("ZeroDivisionError", err)
        self.assertNotIn("boom", err)                       # never echo an unexpected exception's message

    def test_a_catalog_that_does_not_match_its_sources_lists_the_problems(self) -> None:
        from yodb.errors import CatalogRuntimeError, ErrorDetail

        failure = CatalogRuntimeError(ErrorDetail(
            code=ErrorCode.SOURCE_VALIDATION_FAILED, message="Cannot open the catalog: the catalog does not match crm.",
            retryable=False, details={"sources": {"crm": ["error: column 'x' was not found"]}}))
        with mock.patch.object(YoDb, "connect", side_effect=failure):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = cli.main(["validate", "cat"])
        self.assertEqual(status, 1)
        self.assertIn("source_validation_failed", err.getvalue())
        self.assertIn("crm: error: column 'x' was not found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
