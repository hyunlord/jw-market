from __future__ import annotations

import sys
import types

import numpy as np
import pandas as pd

from pipeline.scripts.forecast import forecast_runner


def test_phase302_prophet_uses_native_uncertainty_interval(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeProphet:
        def __init__(self, **kwargs):
            calls["init_kwargs"] = kwargs
            self._history_y: list[float] = []

        def fit(self, df):
            self._history_y = [float(value) for value in df["y"].tolist()]
            return self

        def make_future_dataframe(self, periods, freq, include_history):
            calls["future_args"] = {"periods": periods, "freq": freq, "include_history": include_history}
            return pd.DataFrame({"ds": pd.date_range("2026-05-01", periods=periods, freq=freq)})

        def predict(self, df):
            n = len(df)
            if n == len(self._history_y):
                yhat = np.asarray(self._history_y, dtype=float)
                return pd.DataFrame({"yhat": yhat, "yhat_lower": yhat - 1.0, "yhat_upper": yhat + 1.0})
            point = np.full(n, 100.0)
            return pd.DataFrame(
                {
                    "yhat": point,
                    "yhat_lower": np.linspace(70.0, 75.0, n),
                    "yhat_upper": np.linspace(130.0, 140.0, n),
                }
            )

    fake_module = types.ModuleType("prophet")
    fake_module.Prophet = FakeProphet
    monkeypatch.setitem(sys.modules, "prophet", fake_module)

    periods = [f"2021-{month:02d}" for month in range(1, 13)] * 5
    values = [100.0] * len(periods)
    result = forecast_runner.build_forecast_result(periods, values, "UBIST", steps=6)

    assert calls["init_kwargs"]["uncertainty_samples"] == 1000
    assert calls["init_kwargs"]["interval_width"] == 0.95
    assert result["ci"]["ci_lower_95"] == [100.0, 70.0, 71.0, 72.0, 73.0, 74.0, 75.0]
    assert result["ci"]["ci_upper_95"] == [100.0, 130.0, 132.0, 134.0, 136.0, 138.0, 140.0]
    assert result["actual_model"]["params"]["uncertainty_samples"] == 1000


def test_phase302_sarimax_uses_get_forecast_conf_int(monkeypatch) -> None:
    calls = {"get_forecast": False}

    class FakeForecast:
        def __init__(self, steps):
            self.predicted_mean = pd.Series(np.linspace(100.0, 120.0, steps))

        def conf_int(self, alpha):
            assert alpha == 0.05
            return pd.DataFrame(
                {
                    0: np.linspace(80.0, 90.0, len(self.predicted_mean)),
                    1: np.linspace(130.0, 150.0, len(self.predicted_mean)),
                }
            )

    class FakeResult:
        fittedvalues = pd.Series([100.0] * 40)

        def forecast(self, steps):
            return pd.Series(np.linspace(100.0, 120.0, steps))

        def get_forecast(self, steps):
            calls["get_forecast"] = True
            return FakeForecast(steps)

    class FakeSARIMAX:
        def __init__(self, *args, **kwargs):
            calls["init_kwargs"] = kwargs

        def fit(self, **kwargs):
            calls["fit_kwargs"] = kwargs
            return FakeResult()

    monkeypatch.setattr(forecast_runner.sm.tsa.statespace, "SARIMAX", FakeSARIMAX)

    periods = [f"2021-{month:02d}" for month in range(1, 13)] * 4
    values = [100.0] * 40
    result = forecast_runner.build_forecast_result(periods, values, "UBIST", steps=4)

    assert calls["get_forecast"] is True
    assert result["ci"]["ci_lower_95"] == [100.0, 80.0, 83.33333333333333, 86.66666666666667, 90.0]
    assert result["ci"]["ci_upper_95"] == [100.0, 130.0, 136.66666666666666, 143.33333333333334, 150.0]
