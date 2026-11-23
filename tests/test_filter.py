"""Unit-Tests filter.py: Größen-/Prozent-Parser, Schwellwert-Logik, Gruppen."""

from __future__ import annotations

import pytest

from mail_quota_report.filter import (
    Threshold,
    apply_group_filter,
    classify,
    free_kib,
    free_percent,
    parse_size,
    parse_threshold,
    passes_threshold,
    used_percent,
)


# --- parse_size: kB/MB/GB, case-insensitiv, Randfälle -----------------------

@pytest.mark.parametrize("raw,expected", [
    ("5GB", 5_000_000),
    ("5gb", 5_000_000),
    ("5 GB", 5_000_000),
    ("5GiB", 5_000_000),
    ("512MB", 512_000),
    ("512 mb", 512_000),
    ("100kB", 100),
    ("100 KB", 100),
    ("100k", 100),
    ("1G", 1_000_000),
    ("2,5GB", 2_500_000),
    ("2.5GB", 2_500_000),
    (512_000, 512_000),
    (0, 0),
    ("0GB", 0),
])
def test_parse_size_valid(raw, expected):
    assert parse_size(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "  ", "abc", "5TB", "5XB", "-5GB", "5,5", "GB", "5 GB extra",
    "5.5.5GB", -5, 5.5, True,
])
def test_parse_size_invalid(raw):
    with pytest.raises(ValueError):
        parse_size(raw)


def test_parse_size_negative_number():
    with pytest.raises(ValueError, match="[Nn]egativ"):
        parse_size(-100)


# --- parse_threshold ---------------------------------------------------------

def test_threshold_percent():
    th = parse_threshold({"type": "percent", "value": 80})
    assert th.type == "percent"
    assert th.value == 80


def test_threshold_fixed_gb():
    th = parse_threshold({"type": "fixed", "value": "5GB"})
    assert th.type == "fixed"
    assert th.value == 5_000_000


def test_threshold_fixed_number_is_kib():
    th = parse_threshold({"type": "fixed", "value": 500000})
    assert th.value == 500000


@pytest.mark.parametrize("data", [
    {"type": "percent", "value": 101},
    {"type": "percent", "value": -1},
    {"type": "percent", "value": "x"},
    {"type": "fixed", "value": "5TB"},
    {"type": "liter", "value": 5},
    {"type": "fixed"},
])
def test_threshold_invalid(data):
    with pytest.raises(ValueError):
        parse_threshold(data)


# --- Prozent-/Frei-Berechnung ------------------------------------------------

def test_used_percent_basic():
    assert used_percent(8_000_000, 10_000_000) == pytest.approx(80.0)


def test_used_percent_limit_zero():
    assert used_percent(500, 0) is None


def test_used_percent_over_limit():
    # Überziehung: >100 % möglich (dovecot zählt über Limit hinaus)
    assert used_percent(11_000_000, 10_000_000) == pytest.approx(110.0)


def test_free_values():
    assert free_kib(2_000_000, 10_000_000) == 8_000_000
    assert free_percent(2_000_000, 10_000_000) == pytest.approx(80.0)
    assert free_percent(0, 0) is None


# --- classify: Hervorhebungs-Schwellwerte ------------------------------------

@pytest.mark.parametrize("pct,expected", [
    (0, "OK"),
    (79.9, "OK"),
    (80, "WARNUNG"),
    (94.9, "WARNUNG"),
    (95, "KRITISCH"),
    (100, "KRITISCH"),
    (110, "KRITISCH"),
])
def test_classify_defaults(pct, expected):
    assert classify(pct, 80, 95) == expected


def test_classify_configurable():
    assert classify(70, 70, 90) == "WARNUNG"
    assert classify(90, 70, 90) == "KRITISCH"


def test_classify_none_is_limit0():
    assert classify(None, 80, 95) == "LIMIT_0"


# --- below_threshold: Filter auf FREIEN Speicher ----------------------------

@pytest.mark.parametrize("used,limit,th_type,th_value,expected", [
    # frei = 20 % des Limits → percent 80: frei < 80 % → True
    (8_000_000, 10_000_000, "percent", 80, True),
    # frei = 80 % → nicht darunter → False
    (2_000_000, 10_000_000, "percent", 80, False),
    # exakt an der Grenze: frei = 20 % < 20 % → False (striktes <)
    (8_000_000, 10_000_000, "percent", 20, False),
    # fixed: frei = 8 GB < 5 GB? Nein → False
    (2_000_000, 10_000_000, "fixed", 5_000_000, False),
    # fixed: frei = 3 GB < 5 GB → True
    (7_000_000, 10_000_000, "fixed", 5_000_000, True),
    # Limit 0: nicht bewertbar → False
    (100, 0, "fixed", 5_000_000, False),
])
def test_passes_threshold(used, limit, th_type, th_value, expected):
    th = Threshold(type=th_type, value=th_value)
    assert passes_threshold(used, limit, th) is expected


def test_threshold_describe():
    assert "frei < 80" in parse_threshold({"type": "percent", "value": 80}).describe
    assert "5,00 GB" in parse_threshold({"type": "fixed", "value": "5GB"}).describe


# --- Gruppen-Filter ----------------------------------------------------------

class _Entry:
    def __init__(self, dn):
        self.dn = dn


def test_group_filter_include_empty_all():
    entries = [_Entry("cn=a,dc=x"), _Entry("cn=b,dc=x")]
    out = apply_group_filter(entries, set(), set())
    assert len(out) == 2


def test_group_filter_exclude_wins():
    entries = [_Entry("cn=a,dc=x"), _Entry("cn=b,dc=x")]
    out = apply_group_filter(entries, {"cn=a,dc=x"}, {"cn=b,dc=x"})
    assert [e.dn for e in out] == ["cn=a,dc=x"]


def test_group_filter_dn_normalization():
    entries = [_Entry("CN=A, DC=X")]
    out = apply_group_filter(entries, {"cn=a,dc=x"}, set())
    assert len(out) == 1
