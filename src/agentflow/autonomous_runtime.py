"""End-to-end autonomous runtime integrating WorkerPool, FallbackExecutor, SubagentDispatcher, LiveEventBus, CommunicationOptimizer, and SharedContextRegistry."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .communication_optimizer import CommunicationOptimizer
from .fallback_executor import execute_with_fallback
from .global_config import GlobalConfig, RoleModelConfig, load_global_config
from .observability import LiveEventBus
from .shared_context import SharedContextRegistry
from .subagent_dispatcher import SubagentDispatcher
from .worker_pool import WorkerPool


class AutonomousRuntime:
    """Coordinates parallel task execution, specialized subagents, fallback, observability, and shared context."""

    def __init__(
        self,
        root: Path,
        run_id: str,
        *,
        project_id: str | None = None,
        config: GlobalConfig | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.db_path = self.root / ".agentflow" / "state.sqlite3"
        self.run_id = run_id
        self.project_id = project_id or str(self.root)
        self.config = config or load_global_config()

        self.pool = WorkerPool(
            self.db_path,
            max_concurrent_workers=(self.config.max_concurrent_workers if self.config else None),
            distribution_policy=(self.config.worker_distribution_policy if self.config else None),
        )
        self.dispatcher = SubagentDispatcher(self.config)
        self.events = LiveEventBus(self.db_path)
        self.optimizer = CommunicationOptimizer()
        self.context = SharedContextRegistry(self.db_path, self.project_id, self.run_id)

    def execute_tasks(
        self,
        tasks: list[dict[str, Any]],
        runner: Callable[[RoleModelConfig, dict[str, Any], str], Any],
        *,
        availability_check: Callable[[RoleModelConfig], bool] | None = None,
    ) -> dict[str, str]:
        """Enqueue tasks with dependencies and execute them in parallel with subagents, fallback, and observability."""
        self.pool.enqueue_tasks(self.run_id, tasks)
        self.events.emit(self.run_id, "run_started", payload={"task_count": len(tasks)})

        def _worker_exec(task: dict[str, Any], worker_id: str) -> Any:
            task_id = task["task_id"]
            payload = task.get("payload", {})
            role = str(payload.get("role", "development"))

            self.events.emit(self.run_id, "task_started", worker_id=worker_id, task_id=task_id, payload={"role": role})

            def _dispatch_exec(selected: RoleModelConfig, resolved_role: str, tid: str) -> Any:
                self.events.emit(
                    self.run_id,
                    "subagent_dispatched",
                    worker_id=worker_id,
                    task_id=tid,
                    payload={"role": resolved_role, "agent": selected.agent, "model": selected.model},
                )
                fb_res = execute_with_fallback(
                    selected,
                    lambda model_cfg, idem_key: runner(model_cfg, task, idem_key),
                    task_id=tid,
                    idempotency_key=f"{self.run_id}:{tid}",
                    availability_check=availability_check,
                    config=self.config,
                )
                for attempt in fb_res.attempts:
                    if attempt.reason == "fallback_applied":
                        self.events.emit(
                            self.run_id,
                            "fallback_applied",
                            worker_id=worker_id,
                            task_id=tid,
                            payload={"agent": attempt.agent, "model": attempt.model},
                        )
                if not fb_res.succeeded:
                    self.events.emit(
                        self.run_id,
                        "task_failed",
                        worker_id=worker_id,
                        task_id=tid,
                        payload={"error": fb_res.error, "essential": True},
                    )
                    raise RuntimeError(fb_res.error)
                return fb_res.result

            record = self.dispatcher.dispatch(task_id, role, _dispatch_exec)
            self.context.put_delta(f"task:{task_id}", {"status": "completed", "agent": record.agent, "result": record.result})
            self.events.emit(
                self.run_id,
                "task_completed",
                worker_id=worker_id,
                task_id=task_id,
                payload={"agent": record.agent, "essential": True},
            )
            return record.result

        results = self.pool.run_parallel(self.run_id, _worker_exec)
        self.events.emit(self.run_id, "run_completed", payload={"results": results, "essential": True})
        return results

    def poll_events(self, *, since_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        raw = self.events.fetch_events(self.run_id, since_seq=since_seq, limit=limit)
        return self.optimizer.optimize_messages(raw)

    def poll_context_deltas(self, *, since_version: int = 0) -> list[dict[str, Any]]:
        return self.context.get_deltas(since_version=since_version)
