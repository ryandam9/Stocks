"""Parsing the exchange directories the universe refresh reads.

These files are downloaded from the exchanges by a developer before an image
build, never by the pipeline, so a change of shape shows up here rather than
in production.
"""

import pytest

from symbol_directory import parse_asx_directory, parse_asx_report_text, parse_nse_directory

# The static export the ASX used to publish: a title block, then a header
# leading with the company name.
LEGACY = """ASX listed companies as at 29-Aug-2026

Company name,ASX code,GICS industry group
BHP GROUP LIMITED,BHP,Materials
BETASHARES GLOBAL URANIUM ETF,URNM,Not Applic
"""

# What the research API behind the ASX website serves: code first, more
# columns, no title block.
RESEARCH_API = """ASX Code,Company Name,GICS industry group,Listing date,Market Cap
BHP,BHP GROUP LIMITED,Materials,1885-08-13,200000000
URNM,BETASHARES GLOBAL URANIUM ETF,Not Applic,2022-06-08,900000
"""


@pytest.mark.parametrize("text", [LEGACY, RESEARCH_API])
def test_either_shape_of_the_asx_directory_parses(text):
    """The file has changed shape before and will again."""
    frame = parse_asx_directory(text)

    assert list(frame["ticker"]) == ["BHP", "URNM"]
    assert frame.loc[0, "name"] == "BHP GROUP LIMITED"
    assert set(frame["exchange"]) == {"ASX"}


def test_the_directory_lists_funds_alongside_companies():
    """Which is why it can answer membership but never classification.

    Nothing in the row says URNM is a fund and BHP is not, so a new ASX code
    needs a provider lookup before it can enter an ETF universe.
    """
    frame = parse_asx_directory(RESEARCH_API).set_index("ticker")
    assert set(frame.columns) == {"name", "exchange"}


def test_a_file_with_no_recognisable_header_is_an_error():
    """Better a loud failure than a universe emptied against a login page."""
    with pytest.raises(ValueError, match="no header row"):
        parse_asx_directory("<html><body>Access denied</body></html>")


def test_a_header_with_no_rows_under_it_is_an_error():
    with pytest.raises(ValueError, match="parsed no rows"):
        parse_asx_directory("ASX Code,Company Name\n")


def test_blank_codes_and_duplicates_are_dropped():
    text = "ASX Code,Company Name\nBHP,BHP GROUP\n,Orphan Row\nBHP,BHP GROUP AGAIN\n"
    assert list(parse_asx_directory(text)["ticker"]) == ["BHP"]


# ------------------------------- ASX investment products report

# Real lines from the July 2026 report, in the shape pypdf extracts them:
# code, the security's form, its name, then the columns. The caret is a
# footnote marker. Every kind of row the document contains is here.
REPORT = """Investment Product Summary - July 2026
ASX Fund Segment Market Capitalisation Number Listed
A200 ETF Betashares Australia 200 ETF 0.04 10,544.59  444.12         279.02
^ DHHF ETF Betashares Diversified All Growth ETF 0.19 1,504.57    49.48
URNM ETF Betashares Global Uranium ETF 0.69 311.31       (17.38)          9.90
IMPQ Active Perennial Better Future Active ETF 0.99 24.22         (1.80)
ALFA Complex VanEck Australian Long Short Complex ETF 0.39 36.24         2.67
ETPMPM SP Global X Physical Precious Metals Basket 0.44 128.27       5.08
1GOV ETF VanEck 1-5 Year Australian Govt Bd ETF 0.22 312.40       12.10
USD ETF BetaShares US Dollar ETF 0.45 512.30       44.10
AFI Shares Australian Foundation Investment Company Limited 0.16 No 8,421.43
APA Stapled APA Group 13,489.03       (198.56)       706,136,489
OPH Units Ophir High Conviction Fund 1.23 Yes 565.3 (11.23)
XJOAI Index S&P/ASX 200 Accumulation n/a n/a n/a n/a 123,257.00
TOTAL 591
"""


def test_the_report_returns_funds_and_structured_products():
    frame = parse_asx_report_text(REPORT).set_index("ticker")

    assert list(frame.index) == ["A200", "DHHF", "URNM", "IMPQ", "ALFA", "ETPMPM", "1GOV", "USD"]
    assert set(frame["exchange"]) == {"ASX"}


def test_companies_reits_and_trusts_are_not_funds():
    """The report covers the whole ASX product suite, not just funds.

    Taking every row put Argo, Atlas Arteria, Arena REIT and APA Group into an
    ETF universe -- APA being the ticker removed from it by hand a week
    earlier. The form beside the code is what separates them.
    """
    tickers = set(parse_asx_report_text(REPORT)["ticker"])

    assert "AFI" not in tickers  # Shares -- a listed investment company
    assert "APA" not in tickers  # Stapled -- an infrastructure group
    assert "OPH" not in tickers  # Units -- a listed trust
    assert "XJOAI" not in tickers  # Index -- a benchmark, not a product


def test_the_form_column_is_not_part_of_the_name():
    frame = parse_asx_report_text(REPORT).set_index("ticker")

    assert frame.loc["IMPQ", "name"] == "Perennial Better Future Active ETF"
    assert frame.loc["ALFA", "name"] == "VanEck Australian Long Short Complex ETF"
    assert frame.loc["ETPMPM", "name"] == "Global X Physical Precious Metals Basket"


def test_a_footnote_marker_is_not_part_of_the_code():
    """Some rows carry a leading caret."""
    assert "DHHF" in set(parse_asx_report_text(REPORT)["ticker"])


def test_a_code_may_carry_digits_and_run_to_six_characters():
    """A200, 1GOV and ETPMPM are all real. Letters-only-and-four lost 32 funds."""
    frame = parse_asx_report_text(REPORT).set_index("ticker")

    assert frame.loc["A200", "name"] == "Betashares Australia 200 ETF"
    assert frame.loc["1GOV", "name"] == "VanEck 1-5 Year Australian Govt Bd ETF"
    assert "ETPMPM" in frame.index


def test_a_number_inside_a_fund_name_is_not_the_start_of_the_figures():
    """Cutting at the first digit truncated these to "Betashares Australia"
    and "VanEck". A column figure has a decimal point or a percent sign."""
    frame = parse_asx_report_text(REPORT).set_index("ticker")

    assert "200 ETF" in frame.loc["A200", "name"]
    assert "1-5 Year" in frame.loc["1GOV", "name"]


def test_a_word_that_is_also_a_ticker_is_still_a_fund():
    """USD looks like a column label and is BetaShares' US Dollar ETF."""
    assert parse_asx_report_text(REPORT).set_index("ticker").loc["USD", "name"] == (
        "BetaShares US Dollar ETF"
    )


def test_titles_headings_and_totals_are_not_products():
    tickers = set(parse_asx_report_text(REPORT)["ticker"])

    assert "ASX" not in tickers
    assert not {"TOTAL", "Total"} & tickers


def test_a_wrapped_row_keeps_its_whole_name():
    """A long name can overflow, taking the figures onto the next line."""
    wrapped = (
        "BBFD Complex Betashares Geared Short US Treasury Bond Currency Hedged\n"
        "Complex ETF 0.68 168.0 78.0\n"
    )
    assert parse_asx_report_text(wrapped).iloc[0]["name"] == (
        "Betashares Geared Short US Treasury Bond Currency Hedged Complex ETF"
    )


def test_a_file_with_no_product_rows_is_an_error():
    """A cover page or a failed text extraction must not read as an empty ASX."""
    with pytest.raises(ValueError, match="no product rows"):
        parse_asx_report_text("Investment Product Summary - July 2026\nTOTAL 591\n")


# ------------------------------------- NSE

# EQUITY_L.csv as NSE publishes it: every column name after the first carries
# a leading space, which is why the parser matches headers stripped rather
# than exactly.
EQUITY_L = """SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
RELIANCE, Reliance Industries Limited, EQ, 29-NOV-1995, 10, 1, INE002A01018, 10
TCS, Tata Consultancy Services Limited, EQ, 25-AUG-2004, 1, 1, INE467B01029, 1
YAARI, Yaari Digital Integrated Services Limited, BE, 30-SEP-2011, 1, 1, INE719F01012, 1
SOMETHINGSME, A Small And Medium Enterprise Limited, SM, 01-JAN-2024, 10, 1000, INE000X01011, 10
"""


def test_nse_directory_keeps_ordinary_equity():
    frame = parse_nse_directory(EQUITY_L)

    assert list(frame["ticker"]) == ["RELIANCE", "TCS", "YAARI"]
    assert frame.iloc[0]["name"] == "Reliance Industries Limited"
    # The series already answered this, so nothing is inferred from the name.
    assert set(frame["asset_type"]) == {"common_stock"}
    assert set(frame["exchange"]) == {"NSE"}


def test_nse_keeps_trade_for_trade_shares():
    """BE is a surveillance state, not a different security.

    A stock moves between EQ and BE without ceasing to be ordinary equity, so
    excluding BE would silently drop a name for the weeks it sits there.
    """
    assert "YAARI" in set(parse_nse_directory(EQUITY_L)["ticker"])


def test_nse_excludes_the_sme_platform():
    """SME lots are thousands of shares, so screen volume is not comparable."""
    assert "SOMETHINGSME" not in set(parse_nse_directory(EQUITY_L)["ticker"])


def test_nse_series_filter_is_configurable():
    frame = parse_nse_directory(EQUITY_L, series={"SM"})
    assert list(frame["ticker"]) == ["SOMETHINGSME"]


def test_a_file_that_is_not_equity_l_is_an_error():
    """An index export or the wrong download must not read as an empty NSE."""
    with pytest.raises(ValueError, match="no SYMBOL column"):
        parse_nse_directory("Index Name,Open,High,Low,Close\nNIFTY 50,1,2,3,4\n")


def test_a_file_with_no_equity_series_is_an_error():
    with pytest.raises(ValueError, match="no rows in series"):
        parse_nse_directory("SYMBOL, NAME OF COMPANY, SERIES\nSMEONE, An SME Limited, SM\n")
