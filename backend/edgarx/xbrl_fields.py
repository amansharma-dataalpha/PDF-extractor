"""
# xbrl_fields.py - the list of financial fields and the XBRL tags that hold them

- Each field (e.g. `is.total_revenue`) has a label, a statement, a unit and a list of XBRL tags.
- The field ids are the same ones finx and the golden dataset use.
- Tags are tried in order, separately for every period, so a company that switched tags keeps a continuous series.
- `payment` marks cash outflows that XBRL stores as positive numbers but statements print as negative.
- `sign_ambiguous` marks lines that statements print either as a positive expense or as a negative deduction.
"""
from __future__ import annotations

from dataclasses import dataclass


# One financial field and where to find it in XBRL.
@dataclass(frozen=True)
class Concept:
    id: str
    label: str
    statement: str
    kind: str  # "duration" | "instant"
    unit: str  # "USD" | "USD/shares" | "shares"
    tags: tuple[str, ...]
    payment: bool = False
    note: str = ""
    sign_ambiguous: bool = False  # statements print this either as +expense or as a negative deduction


# Short builder so the table below stays readable.
def _c(id, label, statement, kind, tags, unit="USD", payment=False, note="", sign_ambiguous=False):
    return Concept(id, label, statement, kind, unit, tuple(tags), payment, note, sign_ambiguous)


# Every supported field, keyed by field id.
CONCEPTS: dict[str, Concept] = {c.id: c for c in [
    _c("is.total_revenue", "Total revenue", "income_statement", "duration",
       ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet"]),
    _c("is.cost_of_revenue", "Total cost of revenue", "income_statement", "duration",
       ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices"]),
    _c("is.gross_margin", "Gross margin", "income_statement", "duration", ["GrossProfit"]),
    _c("is.rnd", "Research and development", "income_statement", "duration",
       ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"]),
    _c("is.sales_marketing", "Sales and marketing", "income_statement", "duration",
       ["SellingAndMarketingExpense", "SellingExpense"]),
    _c("is.gna", "General and administrative", "income_statement", "duration", ["GeneralAndAdministrativeExpense"]),
    _c("is.operating_income", "Operating income", "income_statement", "duration", ["OperatingIncomeLoss"]),
    _c("is.other_income", "Other income (expense), net", "income_statement", "duration",
       ["NonoperatingIncomeExpense", "OtherNonoperatingIncomeExpense"], sign_ambiguous=True),
    _c("is.pretax_income", "Income before income taxes", "income_statement", "duration",
       ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
    _c("is.income_tax", "Provision for income taxes", "income_statement", "duration", ["IncomeTaxExpenseBenefit"], sign_ambiguous=True),
    _c("is.net_income", "Net income", "income_statement", "duration", ["NetIncomeLoss", "ProfitLoss"],
       note="NetIncomeLoss is attributable to the parent; ProfitLoss includes non-controlling interests. A filer with "
            "non-controlling interests prints both; the field targets the parent figure when it exists."),
    _c("is.eps_basic", "EPS - basic", "income_statement", "duration", ["EarningsPerShareBasic"], unit="USD/shares"),
    _c("is.eps_diluted", "EPS - diluted", "income_statement", "duration", ["EarningsPerShareDiluted"], unit="USD/shares"),
    _c("is.shares_basic", "Weighted average shares - basic", "income_statement", "duration",
       ["WeightedAverageNumberOfSharesOutstandingBasic"], unit="shares"),
    _c("is.shares_diluted", "Weighted average shares - diluted", "income_statement", "duration",
       ["WeightedAverageNumberOfDilutedSharesOutstanding"], unit="shares"),
    _c("ci.comprehensive_income", "Comprehensive income", "comprehensive_income", "duration",
       ["ComprehensiveIncomeNetOfTax", "ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest"]),
    _c("bs.cash", "Cash and cash equivalents", "balance_sheet", "instant",
       ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    _c("bs.short_term_investments", "Short-term investments", "balance_sheet", "instant",
       ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]),
    _c("bs.receivables", "Accounts receivable, net", "balance_sheet", "instant",
       ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent", "AccountsAndNotesReceivableNet"]),
    _c("bs.inventories", "Inventories", "balance_sheet", "instant", ["InventoryNet"]),
    _c("bs.total_current_assets", "Total current assets", "balance_sheet", "instant", ["AssetsCurrent"]),
    _c("bs.ppe", "Property and equipment, net", "balance_sheet", "instant", ["PropertyPlantAndEquipmentNet", "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"]),
    _c("bs.goodwill", "Goodwill", "balance_sheet", "instant", ["Goodwill"]),
    _c("bs.intangibles", "Intangible assets, net", "balance_sheet", "instant",
       ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"]),
    _c("bs.total_assets", "Total assets", "balance_sheet", "instant", ["Assets"]),
    _c("bs.accounts_payable", "Accounts payable", "balance_sheet", "instant", ["AccountsPayableCurrent", "AccountsPayableTradeCurrent", "AccountsPayableAndOtherAccruedLiabilitiesCurrent"]),
    _c("bs.current_debt", "Current portion of long-term debt", "balance_sheet", "instant",
       ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"]),
    _c("bs.total_current_liabilities", "Total current liabilities", "balance_sheet", "instant", ["LiabilitiesCurrent"]),
    _c("bs.long_term_debt", "Long-term debt", "balance_sheet", "instant", ["LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"]),
    _c("bs.total_liabilities", "Total liabilities", "balance_sheet", "instant", ["Liabilities"]),
    _c("bs.retained_earnings", "Retained earnings", "balance_sheet", "instant", ["RetainedEarningsAccumulatedDeficit"]),
    _c("bs.total_equity", "Total stockholders' equity", "balance_sheet", "instant",
       ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
       note="StockholdersEquity excludes non-controlling interests; a filer that prints only 'Total equity' (with NCI) "
            "matches the second tag."),
    _c("bs.total_liabilities_equity", "Total liabilities and stockholders' equity", "balance_sheet", "instant",
       ["LiabilitiesAndStockholdersEquity"]),
    _c("cf.operating", "Net cash from operating activities", "cash_flow", "duration",
       ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
    _c("cf.investing", "Net cash from investing activities", "cash_flow", "duration",
       ["NetCashProvidedByUsedInInvestingActivities", "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"]),
    _c("cf.financing", "Net cash from financing activities", "cash_flow", "duration",
       ["NetCashProvidedByUsedInFinancingActivities", "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"]),
    _c("cf.fx_effect", "Effect of exchange rates on cash", "cash_flow", "duration",
       ["EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "EffectOfExchangeRateOnCashAndCashEquivalents",
        "EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations"]),
    _c("cf.net_change", "Net change in cash", "cash_flow", "duration",
       ["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "CashAndCashEquivalentsPeriodIncreaseDecrease",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffectContinuingOperations"]),
    _c("cf.cash_beginning", "Cash at beginning of period", "cash_flow", "instant",
       ["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "CashAndCashEquivalentsAtCarryingValue"],
       note="Instant value at the day before the period starts."),
    _c("cf.cash_end", "Cash at end of period", "cash_flow", "instant",
       ["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "CashAndCashEquivalentsAtCarryingValue"],
       note="Instant value at the period end."),
    _c("cf.capex", "Additions to property and equipment", "cash_flow", "duration",
       ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"], payment=True),
    _c("cf.depreciation", "Depreciation and amortization", "cash_flow", "duration",
       ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization", "Depreciation"]),
    _c("cf.sbc", "Stock-based compensation expense", "cash_flow", "duration", ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]),
    _c("cf.dividends", "Cash dividends paid", "cash_flow", "duration",
       ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"], payment=True),
    _c("cf.buybacks", "Common stock repurchased", "cash_flow", "duration",
       ["PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"], payment=True),
]}

# Filing forms that hold annual numbers / quarterly numbers.
ANNUAL_FORMS = {"10-K", "10-K405", "10-KT", "10-K/A", "10-KT/A", "20-F", "40-F"}
QUARTERLY_FORMS = {"10-Q", "10-Q/A", "10-QT"}
