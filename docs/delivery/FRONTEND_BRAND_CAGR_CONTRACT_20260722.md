# Portal frontend handoff: exclusive brand CAGR keys

Date: 2026-07-22

Backend contract: additive brand CAGR keys; existing market CAGR keys remain unchanged

Frontend repository: `/Users/rexxa/github/jw_portal_react`

This backend round does not modify frontend source.

## Cause KPI card

Read the selected brand CAGR from `data.kpi`:

1. If `brand_cagr_5y_pct` is non-null, render that value with `CAGR (5Y)`.
2. Otherwise, if `brand_cagr_3y_pct` is non-null, render that value with `CAGR (3Y)`.
3. Otherwise render `—` and keep the existing null guard.

Do not use `data.kpi.market_cagr_5y_pct` for this card. That key remains the
market CAGR used by market comparisons and chart tooltips. Do not coerce any
null CAGR to zero.

## Market-status brand card

Read the brand CAGR from `brand_cards[].back_extended`:

1. If `brand_cagr_5y_pct` is non-null, render it with `CAGR (5Y)`.
2. Otherwise, if `brand_cagr_3y_pct` is non-null, render it with `CAGR (3Y)`.
3. Otherwise render `—`.

`brand_cards[].back.cagr_5y_pct` remains temporarily for backward
compatibility. It can contain the legacy 5Y-to-3Y fallback and therefore must
not be used after the frontend migration.

## Out of scope in this frontend round

- Keep `market_cagr_5y_pct` and `market_cagr_3y_pct`; chart market comparisons
  still use those keys.
- `AnalyzePage.tsx` currently renders a missing market CAGR as `0` through
  `?? 0`. Replacing that tooltip value with `—` is a separate change.
- Do not remove or hide the `mom_growth_pct` growth line, labels, right axis,
  tooltip, or Excel growth columns.

## Browser verification checklist

- Cause, Livaro: card shows `3.9056%` from `data.kpi.brand_cagr_5y_pct`, label
  `CAGR (5Y)`; market CAGR remains `9.37%` in its existing comparison context.
- Cause, Actemra: `brand_cagr_5y_pct=null`,
  `brand_cagr_3y_pct=-3.9362`; card shows `-3.9362%` and `CAGR (3Y)`.
- A new brand with neither endpoint: both keys null; card shows `—`, not `0%`.
- Market status repeats the same horizon-selection behavior from
  `brand_cards[].back_extended`.
- Network responses never have both brand CAGR keys non-null for one selected
  brand.
- General view, strategic ML view, and strategic CD view expose the same two
  keys and the same selected-brand value for an equivalent scope.
- IQVIA with exactly 19 quarters uses the 4.75-year exponent in the 5Y slot;
  18 quarters or fewer never enter that slot. UBIST monthly behavior remains
  unchanged.
- Chart tooltip still reads market CAGR and the Market Size & Growth growth
  overlay remains visible.
