"""Durable strategy command processor and runtime lease owner."""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid

from app.services.strategy_command_repository import StrategyCommand, StrategyCommandRepository
from app.utils.logger import get_logger
from app.utils.strategy_runtime_logs import append_strategy_log


logger = get_logger(__name__)


def build_worker_id() -> str:
    configured = os.getenv("QD_WORKER_ID", "").strip()
    if configured:
        return configured
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class TradingWorker:
    def __init__(self, executor, repository: StrategyCommandRepository | None = None) -> None:
        self.executor = executor
        self.repository = repository or StrategyCommandRepository()
        self.worker_id = build_worker_id()
        self.command_lease_seconds = max(10, int(os.getenv("STRATEGY_COMMAND_LEASE_SEC", "30")))
        self.strategy_lease_seconds = max(10, int(os.getenv("STRATEGY_RUNTIME_LEASE_SEC", "30")))
        self.poll_seconds = max(0.1, float(os.getenv("STRATEGY_COMMAND_WORKER_POLL_SEC", "0.5")))
        self.max_attempts = max(1, int(os.getenv("STRATEGY_COMMAND_MAX_ATTEMPTS", "3")))
        self._stop = threading.Event()
        self._last_heartbeat = 0.0
        self._last_lease_renewal = 0.0
        self._global_lease_key = "trading-global-services"
        self._global_services_leader = False
        self._last_global_lease_check = 0.0

    def run_forever(self) -> None:
        logger.info("Trading worker started: %s", self.worker_id)
        self._ensure_global_services()
        self._heartbeat()
        self.restore_desired_strategies()
        try:
            while not self._stop.is_set():
                self._heartbeat()
                self._ensure_global_services()
                self._renew_runtime_leases()
                command = self.repository.claim_next(
                    owner_id=self.worker_id,
                    lease_seconds=self.command_lease_seconds,
                    max_attempts=self.max_attempts,
                )
                if command is None:
                    self._stop.wait(self.poll_seconds)
                    continue
                self._execute(command)
        finally:
            self._shutdown_local_runtimes()
            if self._global_services_leader:
                self.repository.release_process_lease(
                    lease_key=self._global_lease_key,
                    owner_id=self.worker_id,
                )
            self.repository.mark_worker_stopped(self.worker_id)
            logger.info("Trading worker stopped: %s", self.worker_id)

    def stop(self) -> None:
        self._stop.set()

    def restore_desired_strategies(self) -> None:
        from app.services.strategy import StrategyService

        rows = StrategyService().get_running_strategies_with_type()
        restored = 0
        for row in rows or []:
            strategy_id = int(row["id"])
            if not self._acquire_runtime(strategy_id):
                continue
            if self.executor.start_strategy(strategy_id):
                restored += 1
            else:
                self.repository.release_strategy_lease(
                    strategy_id=strategy_id,
                    owner_id=self.worker_id,
                )
                StrategyService().update_strategy_status(strategy_id, "stopped")
        logger.info("Trading runtime restore completed: %s/%s", restored, len(rows or []))

    def _execute(self, command: StrategyCommand) -> None:
        try:
            if command.command_type == "start":
                result = self._start(command.strategy_id)
            elif command.command_type == "stop":
                result = self._stop_strategy(
                    command.strategy_id,
                    close_positions=bool(command.payload.get("close_positions")),
                )
            elif command.command_type == "restart":
                self._stop_strategy(command.strategy_id)
                result = self._start(command.strategy_id)
            else:
                result = self._reconcile(command.strategy_id)
            self.repository.complete(command.id, result=result)
        except Exception as exc:
            append_strategy_log(command.strategy_id, "error", str(exc))
            logger.error(
                "Strategy command failed: command=%s strategy=%s type=%s",
                command.id,
                command.strategy_id,
                command.command_type,
                exc_info=True,
            )
            if command.attempts < self.max_attempts:
                delay = min(60, 2 ** max(0, command.attempts - 1))
                self.repository.fail(command.id, str(exc), retry_delay_seconds=delay)
            else:
                self.repository.fail(command.id, str(exc))
                if command.command_type in {"start", "restart"}:
                    from app.services.strategy import StrategyService

                    StrategyService().update_strategy_status(command.strategy_id, "stopped")

    def _start(self, strategy_id: int) -> dict:
        from app.services.strategy import StrategyService

        strategy = StrategyService().get_strategy(strategy_id)
        if not strategy or str(strategy.get("status") or "").lower() != "running":
            return {"strategy_id": strategy_id, "status": "skipped", "reason": "desired_state_changed"}
        if strategy_id in self._local_strategy_ids():
            return {"strategy_id": strategy_id, "runtime_owner": self.worker_id, "status": "running"}
        if not self._acquire_runtime(strategy_id):
            raise RuntimeError("Strategy runtime lease is owned by another trading worker.")
        if not self.executor.start_strategy(strategy_id):
            self.repository.release_strategy_lease(strategy_id=strategy_id, owner_id=self.worker_id)
            detail = getattr(self.executor, "_last_start_failure", "") or "Executor rejected the strategy."
            raise RuntimeError(detail)
        alive, hint = self.executor.wait_strategy_running(strategy_id, timeout=3.0)
        if not alive:
            self.executor.stop_strategy(strategy_id, persist_status=False)
            self.repository.release_strategy_lease(strategy_id=strategy_id, owner_id=self.worker_id)
            raise RuntimeError(hint or "Strategy exited during startup.")
        return {"strategy_id": strategy_id, "runtime_owner": self.worker_id, "status": "running"}

    def _stop_strategy(self, strategy_id: int, *, close_positions: bool = False) -> dict:
        if close_positions:
            result = self.executor.stop_strategy_with_policy(
                strategy_id,
                close_positions=True,
            )
            self.repository.release_strategy_lease(
                strategy_id=strategy_id,
                owner_id=self.worker_id,
            )
            return result
        if not self.executor.stop_strategy(strategy_id, persist_status=False):
            raise RuntimeError("Executor failed to stop the local strategy runtime.")
        self.repository.release_strategy_lease(strategy_id=strategy_id, owner_id=self.worker_id)
        return {"strategy_id": strategy_id, "status": "stopped"}

    def _reconcile(self, strategy_id: int) -> dict:
        running = self._local_strategy_ids()
        return {
            "strategy_id": strategy_id,
            "runtime_owner": self.worker_id if strategy_id in running else "",
            "running": strategy_id in running,
        }

    def _acquire_runtime(self, strategy_id: int) -> bool:
        token = self.repository.acquire_strategy_lease(
            strategy_id=strategy_id,
            owner_id=self.worker_id,
            lease_seconds=self.strategy_lease_seconds,
        )
        return token is not None

    def _renew_runtime_leases(self) -> None:
        now = time.monotonic()
        if now - self._last_lease_renewal < max(1.0, self.strategy_lease_seconds / 3):
            return
        self._last_lease_renewal = now
        for strategy_id in self._local_strategy_ids():
            try:
                renewed = self.repository.renew_strategy_lease(
                    strategy_id=strategy_id,
                    owner_id=self.worker_id,
                    lease_seconds=self.strategy_lease_seconds,
                )
                if not renewed:
                    logger.error("Strategy runtime lease lost: strategy=%s", strategy_id)
                    self.executor.stop_strategy(strategy_id, persist_status=False)
            except Exception:
                logger.error("Strategy runtime lease renewal failed: strategy=%s", strategy_id, exc_info=True)

    def _local_strategy_ids(self) -> list[int]:
        lock = getattr(self.executor, "lock", None)
        running = getattr(self.executor, "running_strategies", {})
        if lock is None:
            return [int(value) for value in running]
        with lock:
            return [int(value) for value in running]

    def _heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat < 10:
            return
        self._last_heartbeat = now
        try:
            self.repository.fail_exhausted_commands(self.max_attempts)
            self.repository.record_worker_heartbeat(
                worker_id=self.worker_id,
                role="trading",
                metadata={"running_strategies": len(self._local_strategy_ids())},
            )
        except Exception:
            logger.warning("Trading worker heartbeat failed", exc_info=True)

    def _shutdown_local_runtimes(self) -> None:
        for strategy_id in self._local_strategy_ids():
            try:
                self.executor.stop_strategy(strategy_id, persist_status=False)
            finally:
                self.repository.release_strategy_lease(
                    strategy_id=strategy_id,
                    owner_id=self.worker_id,
                )

    def _ensure_global_services(self) -> None:
        now = time.monotonic()
        interval = max(2.0, self.strategy_lease_seconds / 3)
        if now - self._last_global_lease_check < interval:
            return
        self._last_global_lease_check = now
        try:
            if self._global_services_leader:
                renewed = self.repository.renew_process_lease(
                    lease_key=self._global_lease_key,
                    owner_id=self.worker_id,
                    lease_seconds=self.strategy_lease_seconds,
                )
                if not renewed:
                    logger.error("Trading global service lease was lost; stopping process")
                    self._stop.set()
                return

            acquired = self.repository.acquire_process_lease(
                lease_key=self._global_lease_key,
                owner_id=self.worker_id,
                lease_seconds=self.strategy_lease_seconds,
            )
            if not acquired:
                return
            from app.startup import _start_trading_support_services

            _start_trading_support_services()
            self._global_services_leader = True
            logger.info("Trading worker owns global exchange services: %s", self.worker_id)
        except Exception:
            logger.warning("Trading global service lease check failed", exc_info=True)
