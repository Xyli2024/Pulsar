"""Tests for pulsar CLI — uses Click's CliRunner (no real terminal needed)."""

import json
import pytest
from click.testing import CliRunner

from pulsar.cli import main


@pytest.fixture
def runner():
    return CliRunner()


class TestOnceMode:
    def test_once_table_exits_zero(self, runner):
        result = runner.invoke(main, ["--once"])
        assert result.exit_code == 0

    def test_once_json_exits_zero(self, runner):
        result = runner.invoke(main, ["--once", "--format", "json"])
        assert result.exit_code == 0

    def test_once_json_is_valid_json(self, runner):
        result = runner.invoke(main, ["--once", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, dict)

    def test_once_json_contains_expected_keys(self, runner):
        result = runner.invoke(main, ["--once", "--format", "json"])
        data = json.loads(result.output)
        assert "cpu" in data
        assert "memory" in data
        assert "top_processes" in data

    def test_once_top_flag(self, runner):
        result = runner.invoke(main, ["--once", "--format", "json", "--top", "3"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data.get("top_processes", [])) <= 3


class TestValidation:
    def test_invalid_interval_zero(self, runner):
        result = runner.invoke(main, ["--interval", "0", "--once"])
        assert result.exit_code != 0

    def test_invalid_interval_negative(self, runner):
        result = runner.invoke(main, ["--interval", "-1", "--once"])
        assert result.exit_code != 0

    def test_invalid_top_zero(self, runner):
        result = runner.invoke(main, ["--top", "0", "--once"])
        assert result.exit_code != 0
