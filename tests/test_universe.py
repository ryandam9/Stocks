"""Tests for instrument universe loading, classification and normalisation."""

import pytest

from universe import (
    COMMON_STOCK,
    ETF,
    EXCHANGE_UNKNOWN,
    filter_universe,
    infer_asset_type,
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
