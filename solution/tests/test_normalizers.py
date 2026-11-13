from datetime import date
from decimal import Decimal

import pytest

from src.normalizers import (
    InvalidValue, mask_phone, normalize_header, normalize_phone, parse_date, parse_decimal, parse_enum,
)


@pytest.mark.parametrize("raw", [
    "9876543210", "+919876543210", "+91 98765 43210", "91-98765-43210", "(+91) 9876543210",
    "09876543210", "98765-43210", "  919876543210  ", "+91-9876-543-210",
])
def test_phone_formats_normalise_to_canonical(raw):
    assert normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw,expected", [("6000000000", "+916000000000"), ("7123456789", "+917123456789"),
                                          ("8123456789", "+918123456789"), ("9123456789", "+919123456789")])
def test_phone_first_digit_6_to_9_accepted(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "N/A", "abc", "5876543210", "1234567890", "0123456789",  # first digit not 6-9
    "98765432", "987654321012345", "+1 415 555 0134",  # wrong length / foreign
    "022-2345-6789",  # landline: 11 digits, strip 0 -> starts with 2
    "919876543210 12", "+92 9876543210"  # 13 digits / not +91
])
def test_phone_invalid(raw):
    with pytest.raises(InvalidValue):
        normalize_phone(raw)


def test_ten_digit_number_starting_with_91_is_not_stripped():
    assert normalize_phone("9123456789") == "+919123456789"


def test_mask_phone_keeps_only_last_four():
    assert mask_phone("+919876543210") == "+91******3210"


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-31", date(2026, 10, 31)), ("31/10/2026", date(2026, 10, 31)), ("31-10-2026", date(2026, 10, 31)),
    ("31-Oct-2026", date(2026, 10, 31)), ("31-oct-2026", date(2026, 10, 31)), ("31-OCT-2026", date(2026, 10, 31)),
    ("01/02/2026", date(2026, 2, 1)),  # day-first: 1 February, not January 2
    ("29/02/2028", date(2028, 2, 29)),
])
def test_date_formats(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["31/02/2026", "29/02/2027", "2026-13-01", "2026-02-30", "00/01/2026",
                                 "31-Foo-2026", "soon", "TBD", "", "10/31/2026", "2026/10/31", "1/2/2026"])
def test_date_invalid(raw):
    with pytest.raises(InvalidValue):
        parse_date(raw)


@pytest.mark.parametrize("raw,expected", [
    ("27554", "27554.00"), ("₹27,554", "27554.00"), ("Rs 27,554", "27554.00"), ("Rs. 24787", "24787.00"),
    ("INR 18678.3", "18678.30"), ("inr18678", "18678.00"), ("rs.1,234.567", "1234.57"),
    ("1,00,000", "100000.00"), ("0", "0.00"), ("₹ 5.005", "5.01"),  # half-up rounding to 2 places
])
def test_decimal_formats(raw, expected):
    assert parse_decimal(raw) == Decimal(expected)
    assert str(parse_decimal(raw)) == expected


@pytest.mark.parametrize("raw", ["-5", "₹-5", "N/A", "-", "twelve thousand", "NaN", "Infinity", "1e5", "",
                                 "₹", "1,,2", "12.", ".5", "$100"])
def test_decimal_invalid_including_negatives(raw):
    with pytest.raises(InvalidValue):
        parse_decimal(raw)


@pytest.mark.parametrize("raw,expected", [("HI", "hi"), (" hi ", "hi"), ("Ta", "ta"), ("en", "en")])
def test_enum_case_insensitive_stores_lowercase(raw, expected):
    assert parse_enum(raw, ["en", "hi", "ta"]) == expected


@pytest.mark.parametrize("raw", ["Hindi", "english", "xx", ""])
def test_enum_invalid(raw):
    with pytest.raises(InvalidValue):
        parse_enum(raw, ["en", "hi", "ta"])


@pytest.mark.parametrize("raw,expected", [
    ("Customer_Name", "customer name"), ("  Policy   No  ", "policy no"), ("MOBILE__no", "mobile no"),
    ("phone\tnumber", "phone number"), ("a_ _b", "a b"),
])
def test_header_normalisation(raw, expected):
    assert normalize_header(raw) == expected
