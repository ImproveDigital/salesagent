"""Internal workflow-step statuses must map to AdCP task statuses in webhooks.

``WorkflowStep.status`` uses internal vocabulary (``requires_approval``,
``pending``, ``in_progress`` ...). Task-status webhooks must carry the AdCP
``GeneratedTaskStatus`` value the buyer understands; a buyer waiting on
``input-required`` cannot act on ``unknown``.
"""

import pytest
from adcp.webhooks import GeneratedTaskStatus

from src.core.context_manager import _coerce_task_status


@pytest.mark.parametrize(
    ("internal", "expected"),
    [
        ("requires_approval", GeneratedTaskStatus.input_required),
        ("pending_approval", GeneratedTaskStatus.input_required),
        ("pending", GeneratedTaskStatus.submitted),
        ("in_progress", GeneratedTaskStatus.working),
        ("approved", GeneratedTaskStatus.working),
    ],
)
def test_internal_statuses_map_to_adcp(internal: str, expected: GeneratedTaskStatus):
    assert _coerce_task_status(internal) is expected


@pytest.mark.parametrize("value", [s.value for s in GeneratedTaskStatus])
def test_adcp_values_pass_through(value: str):
    assert _coerce_task_status(value) is GeneratedTaskStatus(value)


@pytest.mark.parametrize("raw", ["", None, "something_internal"])
def test_unmapped_falls_back_to_unknown(raw: str | None):
    assert _coerce_task_status(raw) is GeneratedTaskStatus.unknown
