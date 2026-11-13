from pathlib import Path

import pytest

from src.lead_parsing import iter_raw_rows, map_columns, validate_row
from src.schemas import Catalogue


@pytest.fixture
def motor(catalogue: Catalogue):
    return catalogue.workflows["motor_renewal"]


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "batch_001.csv"
    path.write_text(text, encoding="utf-8", newline="")
    return path


def rows(path, schema):
    return list(iter_raw_rows(path, schema))


def test_header_aliases_and_reordered_columns(tmp_path, motor):
    path = write(tmp_path, "Language,Insured Name,Policy No,Mobile No,Expiry Date,Premium,agent_notes\r\n"
                           "hi,Asha Rao,POL-1,9876543210,2026-12-01,\"₹27,554\",ignore me\r\n")
    [(number, raw)] = rows(path, motor)
    assert number == 1
    assert raw == {"preferred_language": "hi", "customer_name": "Asha Rao", "policy_number": "POL-1",
                   "phone": "9876543210", "policy_expiry": "2026-12-01", "premium_amount": "₹27,554"}
    assert "agent_notes" not in raw  # unmatched column ignored


def test_header_normalisation_applies_to_names_and_aliases(tmp_path, motor):
    cols = map_columns(["  Customer_Name ", "PHONE__NUMBER", "policy\tno"], motor)
    assert cols == {"customer_name": 0, "phone": 1, "policy_number": 2}


def test_blank_rows_skipped_and_not_counted(tmp_path, motor):
    path = write(tmp_path, "customer_name,phone,policy_number\r\n,,\r\nA,9876543210,P1\r\n\r\n   ,  ,\r\nB,9876543211,P2\r\n")
    assert [n for n, _ in rows(path, motor)] == [1, 2]


def test_values_trimmed_and_empty_values_missing(tmp_path, motor):
    path = write(tmp_path, "customer_name,phone,policy_number,premium_amount\r\n  Asha  , 9876543210 ,P1,   \r\n")
    [(_, raw)] = rows(path, motor)
    assert raw["customer_name"] == "Asha" and raw["phone"] == "9876543210" and raw["premium_amount"] is None


def test_short_rows_are_padded(tmp_path, motor):
    path = write(tmp_path, "customer_name,phone,policy_number\r\nAsha,9876543210\r\n")
    [(_, raw)] = rows(path, motor)
    assert raw["policy_number"] is None


def test_first_matching_column_wins_for_duplicate_headers(tmp_path, motor):
    cols = map_columns(["name", "customer name", "phone", "policy"], motor)
    assert cols["customer_name"] == 0


def test_bom_is_ignored(tmp_path, motor):
    path = tmp_path / "b.csv"
    path.write_bytes("\ufeffcustomer_name,phone,policy_number\r\nA,9876543210,P\r\n".encode("utf-8"))
    assert len(rows(path, motor)) == 1


def good(**overrides):
    base = {"customer_name": "Asha", "phone": "98765 43210", "policy_number": "P1", "policy_expiry": None,
            "premium_amount": None, "preferred_language": None}
    base.update(overrides)
    return base


def test_valid_row_is_normalised(motor):
    result = validate_row(motor, 1, good(policy_expiry="31-Oct-2026", premium_amount="Rs. 1,000", preferred_language="HI"))
    assert not result.rejected and result.warnings == 0
    assert result.values["phone"] == "+919876543210"
    assert result.values["policy_expiry"] == "2026-10-31"
    assert result.values["premium_amount"] == "1000.00"
    assert result.values["preferred_language"] == "hi"


@pytest.mark.parametrize("field", ["customer_name", "phone", "policy_number"])
def test_missing_required_reason(motor, field):
    assert validate_row(motor, 1, good(**{field: None})).rejection_reason == f"missing_required:{field}"


def test_invalid_phone_reason_including_na(motor):
    assert validate_row(motor, 1, good(phone="N/A")).rejection_reason == "invalid_phone"
    assert validate_row(motor, 1, good(phone="12345")).rejection_reason == "invalid_phone"


def test_first_failure_in_schema_order_wins(motor):
    # name missing AND phone invalid AND policy missing -> customer_name comes first
    assert validate_row(motor, 1, good(customer_name=None, phone="x", policy_number=None)).rejection_reason == \
        "missing_required:customer_name"
    # phone invalid AND policy missing -> phone is declared before policy_number
    assert validate_row(motor, 1, good(phone="x", policy_number=None)).rejection_reason == "invalid_phone"


def test_optional_invalid_becomes_null_with_warning(motor):
    result = validate_row(motor, 1, good(policy_expiry="31/02/2026", premium_amount="-5", preferred_language="Hindi"))
    assert not result.rejected and result.warnings == 3
    assert result.values["policy_expiry"] is None and result.values["premium_amount"] is None
    assert result.values["preferred_language"] is None


def test_missing_optional_is_not_a_warning(motor):
    assert validate_row(motor, 1, good()).warnings == 0


def test_rejected_rows_report_no_warnings(motor):
    result = validate_row(motor, 1, good(policy_expiry="bad", phone="bad"))
    assert result.rejected and result.warnings == 0 and result.values == {}
