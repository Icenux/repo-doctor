from __future__ import annotations

import json
from pathlib import Path

import pytest
from syrupy.assertion import SnapshotAssertion

from repo_doctor.masking import register_secret
from repo_doctor.reports import FORMATS, render
from repo_doctor.reports.jsonreport import SCHEMA_VERSION, schema_text
from tests.factory import fake_aws_key
from tests.sample import NOW, sample_result


@pytest.mark.parametrize("fmt", FORMATS)
def test_report_snapshot(fmt: str, snapshot: SnapshotAssertion) -> None:
    assert render(sample_result(), fmt, now=NOW) == snapshot  # type: ignore[arg-type]


def test_json_schema_stable() -> None:
    data = json.loads(render(sample_result(), "json", now=NOW))
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["summary"]["repos"] == 14
    infra = next(r for r in data["repos"] if r["name"] == "infra-notes")
    assert infra["band"] == "high"
    f = infra["findings"][0]
    assert set(f) == {
        "id",
        "fingerprint",
        "severity",
        "category",
        "title",
        "message",
        "location",
        "snippet",
        "kind",
        "fixable",
        "data",
    }


def test_schema_file_up_to_date() -> None:
    path = Path(__file__).parent.parent / "docs" / "report-schema.json"
    assert path.read_text("utf-8") == schema_text(), (
        "spusť: uv run python -m repo_doctor.reports.jsonreport > docs/report-schema.json"
    )


@pytest.mark.parametrize("fmt", FORMATS)
def test_reports_never_contain_secrets(fmt: str) -> None:
    secret = fake_aws_key(42)
    register_secret(secret)
    result = sample_result()
    # simulace chyby: tajemství proteklo do textu varování – musí být i tak zamaskované
    result.warnings.append(f"chyba při čtení {secret}")
    text = render(result, fmt, now=NOW)  # type: ignore[arg-type]
    assert secret not in text
    assert "AKIA…(20 znaků)" in text


def test_html_escapes_and_is_self_contained() -> None:
    result = sample_result()
    result.repos[0].name = "<script>alert(1)</script>"
    html = render(result, "html", now=NOW)
    assert "<script>alert(1)</script>" not in html
    assert "http://" not in html.replace("http-equiv", "")
    assert "https://" not in html.split("<main>")[0]  # žádné externí zdroje v hlavičce
    assert "prefers-color-scheme:dark" in html


def test_markdown_code_span_keeps_backticks() -> None:
    from repo_doctor.reports.markdown import _code

    assert _code("a`b") == "``a`b``"
    assert _code("`x`") == "`` `x` ``"
    assert _code("a|b") == "`a\\|b`"


def test_local_time_has_zone() -> None:
    from repo_doctor.reports import local_time

    assert local_time(NOW).endswith("UTC")
