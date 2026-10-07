from __future__ import annotations

import sys

from pydantic import ValidationError

from policy_api.tools.planner_client import PlannerError, ToolPlanningClient
from policy_api.tools.probe import ProtocolProbeError, run_probe
from policy_api.tools.probe_config import ProbeSettings


def main() -> int:
    try:
        settings = ProbeSettings()  # type: ignore[call-arg]
    except ValidationError:
        print("probe_error=probe_configuration_invalid", file=sys.stderr)
        return 1

    try:
        with ToolPlanningClient(
            base_url=str(settings.model_base_url),
            api_key=settings.model_api_key.get_secret_value(),
            model=settings.chat_model,
            timeout=settings.model_timeout_seconds,
        ) as planner:
            run_probe(planner)
    except (PlannerError, ProtocolProbeError) as exc:
        print(f"probe_error={exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
