"""Built-in report providers and local export formats."""

from app.reporting.catalog import CATALOG_REPORT_PROVIDERS
from app.reporting.discrepancy_matrix import DiscrepancyMatrixProvider
from app.reporting.formats import EXPORT_FORMATS
from app.reporting.test_plan import TestPlanProvider

REPORT_PROVIDERS = (
    *CATALOG_REPORT_PROVIDERS,
    DiscrepancyMatrixProvider(),
    TestPlanProvider(),
)

__all__ = ["EXPORT_FORMATS", "REPORT_PROVIDERS"]
