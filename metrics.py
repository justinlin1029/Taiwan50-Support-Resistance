"""Portfolio performance metrics using all observed trading sessions."""
import math
import numpy as np


def sharpe_ratio(equity, initial_equity, annual_risk_free_rate=0.0, periods_per_year=252):
    """Annualized arithmetic excess-return Sharpe; undefined ratios return None.

    Include the first session's return against starting capital, and all flat
    cash days. Constant risk-free daily rate is compounded from the annual rate.
    Sample standard deviation uses ddof=1; no serial-correlation correction.
    """
    if not math.isfinite(annual_risk_free_rate) or annual_risk_free_rate <= -1:
        raise ValueError('年無風險利率必須為有限值且大於 -100%')
    if not isinstance(periods_per_year, int) or isinstance(periods_per_year, bool) or periods_per_year <= 0:
        raise ValueError('年化交易日數必須為正整數')
    values = np.r_[float(initial_equity), np.asarray(equity, dtype=float)]
    if not np.all(np.isfinite(values)) or np.any(values <= 0):
        raise ValueError('計算 Sharpe 的淨值必須為正的有限數值，不可跳過缺漏日')
    returns = values[1:] / values[:-1] - 1
    if len(returns) < 2:
        return None
    daily_rf = math.expm1(math.log1p(annual_risk_free_rate) / periods_per_year)
    excess = returns - daily_rf
    volatility = float(np.std(excess, ddof=1))
    if volatility <= 10 * np.finfo(float).eps:
        return None
    return float(np.mean(excess) / volatility * math.sqrt(periods_per_year))
