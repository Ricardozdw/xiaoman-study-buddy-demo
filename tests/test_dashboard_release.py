from deploy.xiaoman.preflight import check
from deploy.xiaoman.study_schema import render
from deploy.xiaoman.preflight import valid_databases, valid_origins
from deploy.xiaoman import preflight
import json
from pathlib import Path
import pytest


def test_preflight_cannot_succeed_with_missing_evidence(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "x" * 64)
    monkeypatch.setenv("XIAOMAN_MVP", "1")
    monkeypatch.setenv("LLM_CONFIG_JSON", "invalid")
    report=check({"unit_tests": True})
    assert not report["ready"]
    assert not next(r["passed"] for r in report["checks"] if r["check"] == "主备模型")
    assert not next(r["passed"] for r in report["checks"] if r["check"] == "live_evaluation")


def test_schema_uses_current_models_and_scoped_policies():
    sql=render()
    assert "CREATE TABLE IF NOT EXISTS bad_cases" in sql
    assert "CREATE TABLE IF NOT EXISTS llm_calls" in sql
    assert "auth.uid()" not in sql
    assert "WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), ''))" in sql
    assert sql.count("ENABLE ROW LEVEL SECURITY") == sql.count("CREATE TABLE IF NOT EXISTS")
    assert sql.count("FORCE ROW LEVEL SECURITY") == sql.count("CREATE TABLE IF NOT EXISTS")


@pytest.mark.parametrize("value", ["https://","https://user:pass@example.test","https://example.test/path","https://example.test?token=secret","*","https://example.test:broken"])
def test_invalid_origins_do_not_pass_preflight(value):
    assert not valid_origins(value)


def test_database_roles_and_connection_types_are_checked():
    assert not valid_databases("sqlite:///db","sqlite:///db")
    assert not valid_databases("postgresql://postgres@db/study","postgresql://system@db/study")
    assert not valid_databases("postgresql://same@db/study","postgresql://same@db/study")
    assert valid_databases("postgresql+psycopg://app_login@db/study","postgresql+psycopg://system_login@db/study")


@pytest.mark.parametrize("config", ['null','[]','{"models":[],"tiers":null}','{"models":{},"tiers":{"fast":{}}}'])
def test_bad_model_configuration_is_a_failed_check_not_a_crash(monkeypatch,config):
    monkeypatch.setenv("LLM_CONFIG_JSON",config)
    assert not next(row["passed"] for row in check()["checks"] if row["check"]=="主备模型")


@pytest.fixture
def release_bundle(tmp_path, monkeypatch):
    dist = tmp_path / "web/dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<script src='/study/assets/app.js'></script><link rel='stylesheet' href='/study/assets/app.css'>")
    (assets / "app.js").write_text("console.log('public fixture')")
    (assets / "app.css").write_text("body { color: black; }")
    (dist / "xiaoman-lake-blue.png").write_bytes(b"\x89PNG\r\n\x1a\npublic fixture")
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    return dist


@pytest.fixture
def release_evidence(monkeypatch):
    # 仅用于预检测试的环境和结论，不执行模型或数据库请求。
    monkeypatch.setenv("AUTH_SECRET", "test-only-" * 8)
    monkeypatch.setenv("XIAOMAN_MVP", "1")
    monkeypatch.setenv("XIAOMAN_DEMO_CLOCK", "0")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://study.example.test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://app_login@db/study")
    monkeypatch.setenv("SYSTEM_DATABASE_URL", "postgresql+psycopg://system_login@db/study")
    monkeypatch.setenv("PREFLIGHT_TEST_MODEL_KEY", "private-fixture-value")
    monkeypatch.setenv("LLM_CONFIG_JSON", json.dumps({
        "models": {name: {"model": name, "api_key_env": "PREFLIGHT_TEST_MODEL_KEY"} for name in ("candidate-a", "candidate-b")},
        "tiers": {tier: ["candidate-a", "candidate-b"] for tier in ("fast", "strong")},
    }))
    template = Path(__file__).resolve().parents[1] / "deploy/xiaoman/release-evidence.example.json"
    example = json.loads(template.read_text())
    assert example and all(value is False for value in example.values())
    return dict.fromkeys(example, True)


def test_complete_bundle_passes_but_missing_evidence_still_blocks(release_bundle, release_evidence):
    report = check(release_evidence)
    assert report["ready"] and report["blockers"] == []
    report = check({**release_evidence, "public_pdf": False})
    assert not report["ready"] and report["blockers"] == ["public_pdf"]
    assert report["generated_at"].endswith("+00:00")


@pytest.mark.parametrize("missing", ["index.html", "assets/app.js", "assets/app.css", "xiaoman-lake-blue.png"])
def test_index_path_alone_cannot_hide_missing_bundle_files(release_bundle, release_evidence, missing):
    (release_bundle / missing).unlink()
    report = check(release_evidence)
    assert not report["ready"]
    assert report["blockers"] == ["前端产物"]


@pytest.mark.parametrize("path,body", [
    ("assets/app.js", b""), ("assets/app.css", b""),
    ("assets/app.js", b"<!doctype html><html>error</html>"),
    ("assets/app.css", b"\xef\xbb\xbf<html>error</html>"),
    ("xiaoman-lake-blue.png", b"not a PNG"),
])
def test_empty_and_wrong_bundle_content_blocks_release(release_bundle, path, body):
    (release_bundle / path).write_bytes(body)
    assert not preflight.frontend_ready(release_bundle)[0]


@pytest.mark.parametrize("script", ["/assets/app.js", "https://other.example/app.js", "/study/assets/../../../outside.js"])
def test_wrong_base_external_or_escaping_assets_do_not_pass(release_bundle, script):
    (release_bundle / "index.html").write_text(f'<script src="{script}"></script><link rel="stylesheet" href="/study/assets/app.css">')
    assert not preflight.frontend_ready(release_bundle)[0]


def test_saved_report_matches_stdout_without_secrets(release_bundle, release_evidence, tmp_path, capsys):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(release_evidence))
    output = tmp_path / "reports/release.json"
    assert preflight.main([str(evidence), "--output", str(output)]) == 0
    stdout = capsys.readouterr().out
    assert json.loads(stdout) == json.loads(output.read_text())
    assert "private-fixture-value" not in stdout
    assert "postgresql+psycopg://" not in stdout


@pytest.mark.parametrize("contents", ["{broken", "[]", "true"])
def test_bad_evidence_is_a_structured_failed_report(release_bundle, tmp_path, capsys, contents):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(contents)
    assert preflight.main([str(evidence)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["ready"] and "验收记录" in report["blockers"]


def test_missing_evidence_file_does_not_crash(release_bundle, tmp_path, capsys):
    assert preflight.main([str(tmp_path / "missing.json")]) == 1
    assert "验收记录" in json.loads(capsys.readouterr().out)["blockers"]


def test_report_cannot_overwrite_its_evidence(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text('{"unit_tests": false}')
    with pytest.raises(SystemExit) as error:
        preflight.main([str(path), "--output", str(path)])
    assert error.value.code == 2
    assert path.read_text() == '{"unit_tests": false}'


def test_unwritable_report_is_an_explicit_failure(release_bundle, release_evidence, tmp_path, capsys):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(release_evidence))
    not_directory = tmp_path / "file"
    not_directory.write_text("keep")
    assert preflight.main([str(evidence), "--output", str(not_directory / "report.json")]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["blockers"] == ["报告保存"]
    assert not_directory.read_text() == "keep"
