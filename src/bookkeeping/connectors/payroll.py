"""Payroll provider integration placeholders.

This module deliberately does not calculate payroll or post payroll entries.
It records the provider/report shapes that future importers should target:
downloaded provider reports first, API connectors later when a provider exposes
a stable payroll-results or general-ledger surface.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PayrollProviderSpec:
    provider: str
    display_name: str
    first_import_mode: str
    report_hint: str
    future_api_shape: str


PROVIDER_SPECS: dict[str, PayrollProviderSpec] = {
    "justworks": PayrollProviderSpec(
        provider="justworks",
        display_name="Justworks",
        first_import_mode="custom_report_csv",
        report_hint="Export the Justworks Custom Payroll Report CSV for the period.",
        future_api_shape="partner_or_unified_hris_api_if_available",
    ),
    "gusto": PayrollProviderSpec(
        provider="gusto",
        display_name="Gusto",
        first_import_mode="payroll_journal_or_general_ledger_report",
        report_hint="Export a payroll journal/general-ledger report for the period.",
        future_api_shape="async_general_ledger_report_api",
    ),
    "adp": PayrollProviderSpec(
        provider="adp",
        display_name="ADP",
        first_import_mode="payroll_output_report",
        report_hint="Export payroll output or payroll journal reports for the period.",
        future_api_shape="product_specific_payroll_output_api",
    ),
    "deel": PayrollProviderSpec(
        provider="deel",
        display_name="Deel",
        first_import_mode="gross_to_net_report",
        report_hint="Export gross-to-net/payroll results reports for the period.",
        future_api_shape="global_payroll_results_api",
    ),
    "other": PayrollProviderSpec(
        provider="other",
        display_name="Other payroll provider",
        first_import_mode="provider_summary_report",
        report_hint="Export payroll summary reports showing wages, taxes, benefits, deductions, net pay, and fees.",
        future_api_shape="provider_specific",
    ),
}


def normalize_provider(provider: str | None) -> str:
    key = str(provider or "").strip().lower()
    return key if key in PROVIDER_SPECS else "other"


def provider_spec(provider: str | None) -> PayrollProviderSpec:
    return PROVIDER_SPECS[normalize_provider(provider)]
