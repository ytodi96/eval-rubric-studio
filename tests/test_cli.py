import pytest
from typer.testing import CliRunner

from evalstudio.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def test_import_list_show(demo_dir):
    r = runner.invoke(app, ["import", str(demo_dir)])
    assert r.exit_code == 0, r.output
    assert "support-email" in runner.invoke(app, ["list"]).output
    shown = runner.invoke(app, ["show", "support-email"])
    assert shown.exit_code == 0, shown.output
    assert "30 test cases" in shown.output
    assert "prompt adapter, openai / gpt-6-luna" in shown.output


def test_approve(demo_dir):
    runner.invoke(app, ["import", str(demo_dir)])
    r = runner.invoke(app, ["approve", "support-email", "evalset"])
    assert r.exit_code == 0 and "Approved evalset v1" in r.output


def test_unknown_project_is_a_clear_error():
    r = runner.invoke(app, ["show", "nope"])
    assert r.exit_code == 1
    assert "No project called 'nope'" in r.output


def test_run_dry_run_on_a_bundle_folder_calls_nothing(demo_dir, monkeypatch):
    import evalstudio.llm as llm

    def no_calls(*a, **kw):
        raise AssertionError("a dry run must not call a model")

    monkeypatch.setattr(llm, "_openai", no_calls)
    r = runner.invoke(app, ["run", str(demo_dir), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "30 test case(s)" in r.output and "Judge" in r.output and "Total:" in r.output
    assert "first judge message" in r.output


def test_dry_runs_of_generating_commands(demo_dir):
    runner.invoke(app, ["import", str(demo_dir), "--approve"])
    r = runner.invoke(app, ["generate", "support-email", "-n", "40", "--dry-run"])
    assert r.exit_code == 0 and "generate: test cases: 1 call(s)" in r.output, r.output
    r = runner.invoke(app, ["rubrics", "support-email", "--dry-run"])
    assert r.exit_code == 0 and "Nothing to write" in r.output, r.output
    r = runner.invoke(app, ["intake", "x", "--brief", str(demo_dir / "brief.md"), "--dry-run"])
    assert r.exit_code == 0 and "intake: questions" in r.output and "intake: use-case card" in r.output, r.output


def test_doctor_finds_the_key_without_printing_it(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")
    monkeypatch.delenv("EVALSTUDIO_PROVIDER", raising=False)
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.output
    assert "OPENAI_API_KEY: found" in r.output and "openai / gpt-6.1-sol" in r.output
    assert "sk-test-secret-value" not in r.output


def test_doctor_flags_a_missing_key(monkeypatch):
    monkeypatch.setenv("EVALSTUDIO_JUDGE_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY: missing" in r.output
