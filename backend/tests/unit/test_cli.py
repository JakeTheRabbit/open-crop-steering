"""Smoke tests for :mod:`app.cli` — the s6-service entrypoint module.

These tests confirm the parser shape and the worker-factory wiring
*without running any loop*: building a worker exercises the real
dependency wiring (clients, session factory) but stops short of
``run_forever``. No Postgres / Docker required — clients are constructed
lazily, so :class:`HAClient` / :class:`InfluxClient` / :class:`LLMClient`
do not open connections at construction time.
"""

from __future__ import annotations

import pytest
from app import cli
from app.config import get_settings
from app.workers.alerts import AlertsWorker, NullNotifier
from app.workers.executor import Executor
from app.workers.seal import SealWorker
from app.workers.supervisor import Supervisor

pytestmark = pytest.mark.unit


class TestParser:
    """The argparse parser accepts every documented subcommand."""

    def test_serve_subcommand_parses(self) -> None:
        args = cli.build_parser().parse_args(["serve"])
        assert args.command == "serve"
        assert args.func is cli._cmd_serve

    def test_migrate_subcommand_parses(self) -> None:
        args = cli.build_parser().parse_args(["migrate"])
        assert args.command == "migrate"
        assert args.func is cli._cmd_migrate

    def test_cold_backup_subcommand_parses(self) -> None:
        args = cli.build_parser().parse_args(["cold-backup"])
        assert args.command == "cold-backup"
        assert args.func is cli._cmd_cold_backup

    @pytest.mark.parametrize("name", cli.WORKER_NAMES)
    def test_worker_subcommand_parses_each_worker(self, name: str) -> None:
        args = cli.build_parser().parse_args(["worker", name])
        assert args.command == "worker"
        assert args.name == name
        assert args.func is cli._cmd_worker

    def test_worker_rejects_unknown_name(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["worker", "nonsense"])

    def test_no_subcommand_is_an_error(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([])


class TestWorkerFactories:
    """The factory map resolves each worker to its class — no loops run."""

    def test_factory_map_covers_every_worker_name(self) -> None:
        assert set(cli.WORKER_FACTORIES) == set(cli.WORKER_NAMES)

    def test_build_executor_returns_executor(self) -> None:
        worker, run = cli.build_worker("executor")
        assert isinstance(worker, Executor)
        assert callable(run)

    def test_build_supervisor_returns_supervisor(self) -> None:
        worker, run = cli.build_worker("supervisor")
        assert isinstance(worker, Supervisor)
        assert callable(run)

    def test_build_supervisor_wires_no_touch_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``settings.no_touch_windows`` flows into the built Supervisor."""
        monkeypatch.setenv(
            "NO_TOUCH_WINDOWS",
            '[{"start": "22:00", "end": "06:00", "label": "night"}]',
        )
        get_settings.cache_clear()
        try:
            worker, _run = cli.build_worker("supervisor")
            assert isinstance(worker, Supervisor)
            windows = worker._no_touch_windows
            assert len(windows) == 1
            assert windows[0].start == "22:00"
            assert windows[0].end == "06:00"
            assert windows[0].label == "night"
        finally:
            get_settings.cache_clear()

    def test_build_alerts_returns_alerts_worker(self) -> None:
        worker, run = cli.build_worker("alerts")
        assert isinstance(worker, AlertsWorker)
        assert callable(run)

    def test_build_seal_returns_seal_worker(self) -> None:
        worker, run = cli.build_worker("seal")
        assert isinstance(worker, SealWorker)
        assert callable(run)

    def test_build_worker_rejects_unknown_name(self) -> None:
        with pytest.raises(KeyError):
            cli.build_worker("nonsense")

    def test_alerts_uses_null_notifier_without_telegram(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No Telegram config -> NullNotifier, not a crash."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        get_settings.cache_clear()
        worker, _run = cli.build_worker("alerts")
        assert isinstance(worker, AlertsWorker)
        # The selected notifier should be the null one.
        assert isinstance(worker._notifier, NullNotifier)
        get_settings.cache_clear()
