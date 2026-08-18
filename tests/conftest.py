import pytest
from datetime import datetime, date
from kairos.models import (
    OHLCVCandle,
    OptionChainRow,
    PreviousDayLevels,
    ATMStrikes,
    StrikeCluster,
)

@pytest.fixture
def mock_now():
    return datetime(2026, 3, 22, 10, 0, 0)

@pytest.fixture
def mock_date():
    return date(2026, 3, 22)

@pytest.fixture
def make_candle(mock_now):
    def _make(close, volume=1000, high=None, low=None, vwap=None):
        return OHLCVCandle(
            timestamp=mock_now,
            symbol="NIFTY",
            open=close,
            high=high if high is not None else close,
            low=low if low is not None else close,
            close=close,
            volume=volume,
            vwap=vwap if vwap is not None else close
        )
    return _make

@pytest.fixture
def make_option_row(mock_now, mock_date):
    def _make(strike, opt_type, iv=0.15, delta=0.5, gamma=0.0, theta=0.0, vega=0.0, oi=1000, previous_oi=1100, oi_change=10, ltp=100.0):
        return OptionChainRow(
            timestamp=mock_now,
            symbol="NIFTY",
            expiry=mock_date,
            strike=strike,
            option_type=opt_type,
            iv=iv,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            oi=oi,
            previous_oi=previous_oi,
            oi_change=oi_change,
            volume=500,
            ltp=ltp,
            bid=ltp,
            ask=ltp
        )
    return _make

@pytest.fixture
def make_atm(make_option_row):
    def _make(spot_price, ce_iv=0.15, ce_gamma=0.0, ce_theta=0.0, ce_oi_change=0, pe_oi_change=0, ce_ltp=100.0, pe_ltp=100.0, pe_gamma=None, pe_theta=None):
        atm_strike = round(spot_price / 50) * 50
        # Default PE greeks to match CE if not provided, for simplicity in existing tests
        p_gamma = pe_gamma if pe_gamma is not None else ce_gamma
        p_theta = pe_theta if pe_theta is not None else ce_theta
        
        ce = make_option_row(atm_strike, "CE", iv=ce_iv, gamma=ce_gamma, theta=ce_theta, oi_change=ce_oi_change, ltp=ce_ltp)
        pe = make_option_row(atm_strike, "PE", gamma=p_gamma, theta=p_theta, oi_change=pe_oi_change, ltp=pe_ltp)
        return ATMStrikes(atm_strike=atm_strike, ce=ce, pe=pe, spot_price=spot_price)
    return _make

@pytest.fixture
def make_cluster():
    def _make(spot_price, ce_oi_change=0, pe_oi_change=0, **kwargs):
        atm_strike = round(spot_price / 50) * 50
        
        # Provide base values so percentage-based checks pass for legacy tests
        defaults = {
            "total_abs_gex": 6_000_000.0,     # 20% = 1.2M, tests use +/- 2M
            "total_abs_nde": 2_000_000.0,     # 20% = 400k, tests use +/- 500k
            "total_abs_vega": 60_000_000.0,   # 40% = 24M, tests use 30M
        }
        for k, v in defaults.items():
            if k not in kwargs:
                kwargs[k] = v
                
        return StrikeCluster(
            atm_strike=atm_strike,
            spot_price=spot_price,
            ce_cluster=[],
            pe_cluster=[],
            total_ce_oi_change=ce_oi_change,
            total_pe_oi_change=pe_oi_change,
            **kwargs,
        )
    return _make


@pytest.fixture(autouse=True)
def mock_test_settings(monkeypatch):
    from kairos.config import settings
    monkeypatch.setattr(settings, "oi_lookback_cycles", 6)
    monkeypatch.setattr(settings, "candle_buffer_size", 15)


