"""Entry point for the Autonomous AI Task Worker CLI and Benchmark Demo."""
from __future__ import annotations

import argparse
import json
import sys
from fastapi.testclient import TestClient

from agent.finance.finance_client import FinanceClient
from agent.orchestrator import AgentOrchestrator
from company_env.main import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous AI Task Worker")
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="Task instruction to execute (omit to run full benchmark suite).",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Run the full 11-scenario evaluation benchmark across Categories A-E.",
    )
    args = parser.parse_args()

    if args.benchmark or not args.prompt:
        from eval.run_benchmark import run_benchmark
        summary = run_benchmark()
        sys.exit(0 if summary["passed"] == summary["total"] else 1)

    app = create_app()
    with TestClient(app) as client:
        fc = FinanceClient(client=client)
        orchestrator = AgentOrchestrator(finance_client=fc)
        resp = orchestrator.run(args.prompt)
        print(
            json.dumps(
                {
                    "task_id": resp.task_id,
                    "status": str(resp.status.value if hasattr(resp.status, "value") else resp.status),
                    "success": resp.success,
                    "summary": resp.summary,
                    "answer": resp.answer,
                    "evidence": resp.evidence,
                    "clarification_options": resp.clarification_options,
                    "approval_required": resp.approval_required,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
