"""The rules file: refused when wrong, and traceable in every status it produces."""

import pytest
from pydantic import ValidationError

from recon.config import build_config


def test_misspelt_or_invalid_config_is_refused():
    with pytest.raises(ValidationError):
        build_config({"rules": {"tolerances": {"fees": 0.01}}})
    with pytest.raises(ValidationError):
        build_config({"rules": {"status_deadline_minutes": 0}})


def test_rule_version_changes_with_configuration():
    assert build_config().rule_version != build_config({"rules": {"tolerances": {"fee": 0.01}}}).rule_version
