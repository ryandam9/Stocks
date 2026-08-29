"""Parsing the exchange directories the universe refresh reads.

These files are downloaded from the exchanges by a developer before an image
build, never by the pipeline, so a change of shape shows up here rather than
in production.
"""

import pytest

from symbol_directory import parse_asx_directory, parse_asx_report_text

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

# Every line here is a trap the real report actually contains, in the shape
# pypdf extracts it: a title with no figures, a section heading, a two-line
# column header, codes carrying digits, a six-character code, a fund whose
# name contains numbers, and a long name wrapped onto the next line.
REPORT = """ASX Investment Products
August 2026
Exchange Traded Funds
Code Fund Name Market Cap
($m) Turnover MER
ASAO abrdn Sustainable Asian Opportunities Active ETF 100.0 10.0 0.00%
A200 Betashares Australia 200 ETF 2,145.6 89.2 0.07%
1GOV VanEck 1-5 Year Australian Govt Bd ETF 312.4 12.1 0.22%
ETPMAG Global X Physical Silver Structured 118.9 9.4 0.49%
USD BetaShares US Dollar ETF 512.3 44.1 0.45%
BBFD Betashares Geared Short US Treasury Bond Currency Hedged Complex
ETF 168.0 78.0 0.68%
Total 452 products
"""


def test_the_report_reads_every_kind_of_row():
    frame = parse_asx_report_text(REPORT).set_index("ticker")

    assert list(frame.index) == ["ASAO", "A200", "1GOV", "ETPMAG", "USD", "BBFD"]
    assert set(frame["exchange"]) == {"ASX"}


def test_a_code_may_carry_digits_and_run_to_six_characters():
    """A200, 1GOV and ETPMAG are all real. Letters-only-and-four lost 32 funds."""
    frame = parse_asx_report_text(REPORT).set_index("ticker")

    assert frame.loc["A200", "name"] == "Betashares Australia 200 ETF"
    assert frame.loc["1GOV", "name"] == "VanEck 1-5 Year Australian Govt Bd ETF"
    assert frame.loc["ETPMAG", "name"] == "Global X Physical Silver Structured"


def test_a_number_inside_a_fund_name_is_not_the_start_of_the_figures():
    """Cutting at the first digit truncated these to "Betashares Australia"
    and "VanEck". A column figure has a decimal point or a percent sign."""
    frame = parse_asx_report_text(REPORT).set_index("ticker")

    assert "200 ETF" in frame.loc["A200", "name"]
    assert "1-5 Year" in frame.loc["1GOV", "name"]


def test_a_wrapped_row_keeps_its_whole_name():
    """The name overflows the column and the figures land on the next line."""
    frame = parse_asx_report_text(REPORT).set_index("ticker")

    assert frame.loc["BBFD", "name"] == (
        "Betashares Geared Short US Treasury Bond Currency Hedged Complex ETF"
    )


def test_the_row_under_a_column_header_is_not_swallowed():
    """ "($m) Turnover MER" carries no figures either, so a naive join ate the
    first fund beneath it."""
    assert "ASAO" in set(parse_asx_report_text(REPORT)["ticker"])


def test_titles_headings_and_totals_are_not_products():
    tickers = set(parse_asx_report_text(REPORT)["ticker"])

    assert "ASX" not in tickers  # the document's own title
    assert "Total" not in tickers and "TOTAL" not in tickers


def test_a_word_that_is_also_a_ticker_is_still_a_fund():
    """USD looks like a column label and is BetaShares' US Dollar ETF."""
    frame = parse_asx_report_text(REPORT).set_index("ticker")

    assert frame.loc["USD", "name"] == "BetaShares US Dollar ETF"


def test_a_file_with_no_product_rows_is_an_error():
    """A cover page or a failed text extraction must not read as an empty ASX."""
    with pytest.raises(ValueError, match="no product rows"):
        parse_asx_report_text("ASX Investment Products\nAugust 2026\n")
