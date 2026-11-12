import pytest
from pydantic import ValidationError

from odoo_agent_server.schemas import OdooCodeFileChange


def test_code_file_change_defaults_preserve_existing_write_behavior():
    change = OdooCodeFileChange(relative_path="x/a.py", content="x = 1\n")
    assert change.operation == "write"
    assert change.mode == "overwrite"
    assert change.force is False


def test_code_file_change_supports_explicit_delete():
    change = OdooCodeFileChange(relative_path="x/old.csv", operation="delete")
    assert change.operation == "delete"


def test_delete_rejects_mixed_write_fields():
    with pytest.raises(ValidationError):
        OdooCodeFileChange(relative_path="x/old.csv", operation="delete", content="")


def test_append_rejects_search_replace():
    with pytest.raises(ValidationError):
        OdooCodeFileChange(relative_path="x/a.txt", mode="append", search="a", replace="b")
