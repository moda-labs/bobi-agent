from pathlib import Path

import pytest

from bobi.workflow.schema import load_workflow


def test_empty_workflow_has_actionable_error(tmp_path: Path):
    workflow = tmp_path / "empty.yaml"
    workflow.write_text("")

    with pytest.raises(ValueError, match="workflow file is empty"):
        load_workflow(workflow)


def test_non_mapping_workflow_has_actionable_error(tmp_path: Path):
    workflow = tmp_path / "list.yaml"
    workflow.write_text("- not\n- a\n- workflow\n")

    with pytest.raises(ValueError, match="must contain a YAML mapping"):
        load_workflow(workflow)
