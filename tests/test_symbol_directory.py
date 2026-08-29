"""Parsing the exchange directories the universe refresh reads.

These files are downloaded from the exchanges by a developer before an image
build, never by the pipeline, so a change of shape shows up here rather than
in production.
"""

import pytest

from symbol_directory import parse_asx_directory

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
