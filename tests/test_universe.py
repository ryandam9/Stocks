"""Tests for instrument universe loading, classification and normalisation."""

import pytest

from universe import (
    COMMON_STOCK,
    ETF,
    EXCHANGE_UNKNOWN,
    company_name,
    filter_universe,
    infer_asset_type,
    infer_category,
    infer_issuer,
    load_universe,
    normalise_exchange,
    write_universe,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("NasdaqGS", "NASDAQ"),
        ("NasdaqCM", "NASDAQ"),
        ("NMS", "NASDAQ"),
        ("NYQ", "NYSE"),
        ("NYSE", "NYSE"),
        # The provider returns this venue both spaced and unspaced; Google
        # Finance accepts only the unspaced code.
        ("NYSE AMERICAN", "NYSEAMERICAN"),
        ("NYSEAmerican", "NYSEAMERICAN"),
        ("ASX", "ASX"),
        ("NSI", "NSE"),
        (None, EXCHANGE_UNKNOWN),
        ("", EXCHANGE_UNKNOWN),
        ("   ", EXCHANGE_UNKNOWN),
    ],
)
def test_exchange_normalisation(raw, expected):
    assert normalise_exchange(raw) == expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Armada Acquisition Corp. III Warrant", "warrant"),
        ("Abony Acquisition Corp. I Units", "unit"),
        ("Some Corp Rights", "right"),
        ("Wheeler REIT 7.00% Series D Cumulative Preferred", "preferred"),
        ("Acme 5.5% Senior Notes", "note"),
        ("Alcoa Corporation", COMMON_STOCK),
        ("AbbVie Inc.", COMMON_STOCK),
        # Operating companies whose names contain fund-like words must not be
        # reclassified: matching "Trust"/"Fund" misclassified REITs.
        ("Arbor Realty Trust", COMMON_STOCK),
        ("American Assets Trust Inc.", COMMON_STOCK),
        ("abrdn Income Credit Strategies Fund", COMMON_STOCK),
    ],
)
def test_asset_type_inference(name, expected):
    assert infer_asset_type(name) == expected


def test_inference_respects_the_declared_default():
    """An ETF universe must not have its members labelled common stock."""
    assert infer_asset_type("Betashares AUS Top 20 EqYldMxmsrCmplxETF", default=ETF) == ETF
    assert infer_asset_type("Some Fund Warrant", default=ETF) == "warrant"


def test_legacy_file_loads_with_inferred_metadata(tmp_path):
    path = tmp_path / "legacy.csv"
    path.write_text(
        "AAPL~Apple Inc.\n"
        "\n"
        "# comment\n"
        "AACIW~Armada Acquisition Corp. III Warrant\n"
        "AAPL~Apple Inc.\n"
    )
    df = load_universe(str(path))

    assert list(df["ticker"]) == ["AAPL", "AACIW"]
    assert list(df["asset_type"]) == [COMMON_STOCK, "warrant"]
    # Exchange is unknown, never guessed from the config's exchange code.
    assert set(df["exchange"]) == {EXCHANGE_UNKNOWN}


def test_structured_file_is_used_as_given(tmp_path):
    path = tmp_path / "structured.csv"
    path.write_text(
        "ticker,name,exchange,asset_type,currency,source_date\n"
        "A,Agilent Technologies Inc.,NYSE,common_stock,USD,2026-08-22\n"
        "AAPL,Apple Inc.,NASDAQ,common_stock,USD,2026-08-22\n"
    )
    df = load_universe(str(path))
    assert dict(zip(df["ticker"], df["exchange"], strict=True)) == {
        "A": "NYSE",
        "AAPL": "NASDAQ",
    }


def test_filter_universe_selects_asset_types(tmp_path):
    path = tmp_path / "u.csv"
    path.write_text(
        "GOOD~Good Company Inc.\n"
        "GOODW~Good Company Inc. Warrant\n"
        "GOODU~Good Company Inc. Units\n"
        "GOODP~Good Company Inc. 6% Preferred\n"
    )
    df = load_universe(str(path))
    assert len(df) == 4
    assert list(filter_universe(df, ["common_stock"])["ticker"]) == ["GOOD"]


def test_round_trip_preserves_metadata(tmp_path):
    path = tmp_path / "u.csv"
    path.write_text("A,Agilent,NYSE,common_stock,USD,2026-08-22\n")
    path.write_text(
        "ticker,name,exchange,asset_type,currency,source_date\n"
        'AERO,"Grupo Aeromexico, S.A.B. de C.V",NYSE,common_stock,USD,2026-08-22\n'
    )
    df = load_universe(str(path))
    write_universe(df, str(path))

    reloaded = load_universe(str(path))
    assert reloaded.loc[0, "name"] == "Grupo Aeromexico, S.A.B. de C.V"
    assert reloaded.loc[0, "exchange"] == "NYSE"


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_universe(str(tmp_path / "nope.csv"))


def test_empty_file_raises(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("\n# only comments\n")
    with pytest.raises(ValueError, match="No usable rows"):
        load_universe(str(path))


def test_shipped_universes_are_structured_and_classified():
    """The checked-in universes must carry real metadata."""
    import config as cfg_mod

    for exchange, instrument, expected_type in [
        ("US", "stocks", COMMON_STOCK),
        ("ASX", "etf", ETF),
    ]:
        cfg = cfg_mod.load_config(exchange, instrument)
        df = load_universe(cfg.bundled_ticker_file)
        assert len(df) > 100
        screened = filter_universe(df, [expected_type])
        assert len(screened) > 100
        # Derivative classes must be excluded from the screened set.
        assert not screened["asset_type"].isin(["warrant", "unit", "right"]).any()


# ---------------------------------------------------- issuer and category


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # The ordinary case: the issuer is the first word of the title.
        ("Betashares Global Uranium ETF", "Betashares"),
        ("Vanguard Australian Shares Index ETF", "Vanguard"),
        # Capitalisation is the directory's, not ours: iShares and abrdn are
        # spelled that way on purpose and .title() would ruin both.
        ("iShares Core S&P/ASX 200 ETF", "iShares"),
        ("abrdn Sustainable Asian Opportunities Active ETF", "abrdn"),
        # Two-word brands, and the abbreviations the ASX directory uses.
        ("Global X Physical Gold Structured", "Global X"),
        ("Stt Strt SPDR S&P 500 ETF", "SPDR"),
        ("Russell Inv Australian Government Bd ETF", "Russell Investments"),
        ("Dimsnl Glbl Cor Eq Tr (UnH Cl)-Actv ETF", "Dimensional"),
        # ETF Securities was acquired by Global X in 2022; the ASX directory
        # still carries the old titles, and the issuer today is Global X.
        ("ETFS Global Lithium Miners ETF", "Global X"),
        # A title that opens with a description names no issuer, and a
        # fragment of one would be worse than nothing.
        ("Australian Major Bank Subordinated Debt ETF", ""),
        ("", ""),
    ],
)
def test_infer_issuer(name, expected):
    assert infer_issuer(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Betashares Bitcoin ETF", "crypto"),
        ("Betashares Ethereum ETF", "crypto"),
        ("Global X Physical Gold Structured", "precious metals"),
        ("Global X Silver Miners ETF", "precious metals"),
        ("Global X Copper Miners ETF AUD Inc", "industrial metals"),
        ("BetaShares Global Uranium ETF", "industrial metals"),
        ("iShares 15+ Year Australian Gov Bd ETF", "fixed income"),
        ("Quay Global Real Estate AUD Act ETF", "property"),
        ("Vanguard Australian Shares Index ETF", "equity"),
        # Named for a strategy, never an asset. Blank is the honest answer;
        # calling it equity would make the column look complete while being
        # unverified on a third of the universe.
        ("Aoris International B Managed Fund", ""),
    ],
)
def test_infer_category(name, expected):
    assert infer_category(name) == expected


def test_the_issuers_own_name_is_not_evidence_about_the_assets():
    """Platinum Asset Management runs both; neither holds an ounce of it."""
    assert infer_issuer("Platinum Asia ETF") == "Platinum"
    assert infer_category("Platinum Asia ETF") == ""
    assert infer_category("Platinum International ETF") == ""


def test_a_company_is_its_own_issuer_and_has_no_category(tmp_path):
    """A company issues its own shares, so the issuer is the company.

    Not a fragment of one: inferring a fund manager from a company name gives
    "ATA" for "ATA Creativity Global". Category stays empty because a stock's
    category is its sector, which its name does not carry.
    """
    path = tmp_path / "u.csv"
    path.write_text(
        "ticker,name,exchange,asset_type\n"
        "AACG,ATA Creativity Global - American Depositary Shares,NASDAQ,common_stock\n"
        "QAU,Betashares Gold Bullion ETF,ASX,etf\n"
    )
    df = load_universe(str(path)).set_index("ticker")
    assert df.loc["AACG", "issuer"] == "ATA Creativity Global"
    assert df.loc["AACG", "category"] == ""
    assert df.loc["QAU", "issuer"] == "Betashares"
    assert df.loc["QAU", "category"] == "precious metals"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # The directory's two shapes: a " - " separator, and a trailing phrase.
        (
            "ATA Creativity Global - American Depositary Shares, each representing two",
            "ATA Creativity Global",
        ),
        ("Agilent Technologies, Inc. Common Stock", "Agilent Technologies, Inc."),
        ("Alcoa Corporation Common Stock", "Alcoa Corporation"),
        (
            "Ares Acquisition Corporation III Units, each consisting of one Class A",
            "Ares Acquisition Corporation III",
        ),
    ],
)
def test_company_name_strips_the_security_description(name, expected):
    assert company_name(name) == expected


def test_a_spacs_three_lines_report_one_issuer(tmp_path):
    """Share, unit and warrant are one company, and should group as one."""
    path = tmp_path / "u.csv"
    path.write_text(
        "ticker,name,exchange,asset_type\n"
        "AACI,Armada Acquisition Corp. III - Class A Ordinary Share,NASDAQ,common_stock\n"
        "AACIU,Armada Acquisition Corp. III - Units,NASDAQ,unit\n"
        "AACIW,Armada Acquisition Corp. III - Warrant,NASDAQ,warrant\n"
    )
    assert set(load_universe(str(path))["issuer"]) == {"Armada Acquisition Corp. III"}


def test_a_value_in_the_file_wins_over_the_inference(tmp_path):
    """So a hand correction is not overwritten every time the file is read."""
    path = tmp_path / "u.csv"
    path.write_text(
        "ticker,name,exchange,asset_type,issuer,category\n"
        "XYZ,Some Ambiguous Active ETF,ASX,etf,Ellerston,property\n"
    )
    row = load_universe(str(path)).iloc[0]
    assert row["issuer"] == "Ellerston"
    assert row["category"] == "property"
