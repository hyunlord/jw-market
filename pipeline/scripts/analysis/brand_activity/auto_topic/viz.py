from __future__ import annotations

import html
import json

from .models import JsonValue


def build_viz_payload(report_payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Create the measured-only JSON payload embedded in the static HTML."""
    quality = _dict(report_payload.get("quality_summary"))
    markets = _list(quality.get("markets"))
    brand_results = [
        {**_dict(value), "sample_key": key}
        for key, value in _dict(report_payload.get("brand_results")).items()
        if not key.endswith(":pro") and not key.endswith(":lite")
    ]
    return {
        "generated_from": "measured GenOS/audit payload only",
        "execution_mode": report_payload.get("execution_mode"),
        "models": sorted({str(_dict(log).get("model_key")) for log in _list(report_payload.get("call_logs")) if _dict(log).get("model_key")}) or ["flash"],
        "markets": markets,
        "brand_results": brand_results,
        "label_quality": quality.get("label_quality"),
        "group_map": report_payload.get("group_map"),
        "scope_metadata": report_payload.get("scope_metadata"),
        "tier_axis_similarity": report_payload.get("tier_axis_similarity"),
    }


def render_html(viz_payload: dict[str, JsonValue]) -> str:
    """Render a self-contained measured-only HTML visualization."""
    payload = json.dumps(viz_payload, ensure_ascii=False, sort_keys=True)
    escaped_payload = html.escape(payload, quote=False)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Brand Activity Auto Topic Viz</title>
  <style>
    :root {{ color-scheme: light; --ink:#1f2933; --muted:#607080; --line:#d8dee6; --bg:#f6f8fb; --panel:#ffffff; --blue:#2563eb; --green:#16845b; --amber:#b7791f; --red:#c2410c; --violet:#6d5bd0; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family: Inter, Arial, sans-serif; background:var(--bg); color:var(--ink); }}
    header {{ display:flex; align-items:center; justify-content:space-between; gap:16px; padding:18px 24px; border-bottom:1px solid var(--line); background:var(--panel); }}
    h1 {{ margin:0; font-size:20px; letter-spacing:0; }}
    main {{ max-width:1280px; margin:0 auto; padding:20px; display:grid; grid-template-columns:280px 1fr; gap:18px; }}
    .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    label {{ display:block; font-size:12px; font-weight:700; color:var(--muted); margin-bottom:6px; }}
    select {{ width:100%; min-height:38px; border:1px solid var(--line); border-radius:6px; background:#fff; color:var(--ink); padding:8px; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-bottom:14px; }}
    .metric {{ border:1px solid var(--line); border-radius:8px; background:#fff; padding:12px; min-height:76px; }}
    .metric b {{ display:block; font-size:22px; margin-top:6px; }}
    .grade {{ display:inline-flex; align-items:center; justify-content:center; width:32px; height:32px; border-radius:16px; color:#fff; font-weight:800; }}
    .A {{ background:var(--green); }} .B {{ background:var(--blue); }} .C {{ background:var(--amber); }} .D {{ background:var(--red); }}
    .brand {{ border-top:1px solid var(--line); padding:14px 0; }}
    .brand:first-child {{ border-top:0; }}
    .brand h3 {{ margin:0 0 8px; font-size:15px; }}
    .bar {{ height:26px; width:100%; display:flex; overflow:hidden; border-radius:6px; border:1px solid var(--line); background:#eef2f7; }}
    .seg {{ height:100%; min-width:2px; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:8px 14px; margin-top:8px; font-size:12px; color:var(--muted); }}
    .dot {{ display:inline-block; width:10px; height:10px; border-radius:5px; margin-right:5px; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th,td {{ border-bottom:1px solid var(--line); text-align:left; padding:8px; vertical-align:top; }}
    th {{ color:var(--muted); font-size:12px; }}
    @media (max-width: 820px) {{ main {{ grid-template-columns:1fr; padding:12px; }} header {{ align-items:flex-start; flex-direction:column; }} .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
  </style>
</head>
<body>
  <header>
    <h1>브랜드 활동 자동 토픽 품질</h1>
    <span id="scopeLabel"></span>
  </header>
  <main>
    <aside class="panel">
      <label for="marketSelect">시장</label>
      <select id="marketSelect"></select>
      <div style="height:14px"></div>
      <label for="scopeTypeSelect">범위</label>
      <select id="scopeTypeSelect">
        <option value="all">전체</option>
        <option value="market_group">그룹</option>
        <option value="submarket">하위시장</option>
        <option value="standalone">단일시장</option>
      </select>
      <div style="height:14px"></div>
      <label for="modelSelect">모델</label>
      <select id="modelSelect"></select>
    </aside>
    <section>
      <div class="metrics">
        <div class="metric"><span>품질 등급</span><b id="gradeBox"></b></div>
        <div class="metric"><span>분류 브랜드</span><b id="brandCount"></b></div>
        <div class="metric"><span>평균 기타</span><b id="etcAvg"></b></div>
        <div class="metric"><span>실측 모델</span><b id="modelCount"></b></div>
      </div>
      <div class="panel" id="brandPanel"></div>
      <div style="height:16px"></div>
      <div class="panel"><table><thead><tr><th>시장</th><th>범위</th><th>등급</th><th>사유</th></tr></thead><tbody id="marketRows"></tbody></table></div>
    </section>
  </main>
  <script id="AUTO_TOPIC_DATA" type="application/json">{escaped_payload}</script>
  <script>
    const data = JSON.parse(document.getElementById('AUTO_TOPIC_DATA').textContent);
    const colors = ['#2563eb','#16845b','#b7791f','#6d5bd0','#c2410c','#0f766e','#7c3aed','#64748b'];
    const marketSelect = document.getElementById('marketSelect');
    const scopeTypeSelect = document.getElementById('scopeTypeSelect');
    const modelSelect = document.getElementById('modelSelect');
    function visibleMarkets() {{
      const type = scopeTypeSelect.value || 'all';
      return (data.markets || []).filter(m => type === 'all' || m.scope_type === type);
    }}
    function refreshMarketOptions() {{
      const current = marketSelect.value;
      marketSelect.innerHTML = '';
      visibleMarkets().forEach(m => marketSelect.add(new Option(m.display_name || m.atc4, m.scope_key || m.atc4)));
      if ([...marketSelect.options].some(o => o.value === current)) marketSelect.value = current;
    }}
    refreshMarketOptions();
    data.models.forEach(m => modelSelect.add(new Option(m, m)));
    function render() {{
      refreshMarketOptions();
      const scopeKey = marketSelect.value || (visibleMarkets()[0] && (visibleMarkets()[0].scope_key || visibleMarkets()[0].atc4));
      const market = (data.markets || []).find(m => (m.scope_key || m.atc4) === scopeKey) || {{}};
      const brands = (data.brand_results || []).filter(b => (b.scope_key || b.atc4) === scopeKey);
      document.getElementById('scopeLabel').textContent = data.execution_mode || '';
      document.getElementById('gradeBox').innerHTML = `<span class="grade ${{market.quality_grade || 'D'}}">${{market.quality_grade || 'D'}}</span>`;
      document.getElementById('brandCount').textContent = brands.length;
      document.getElementById('etcAvg').textContent = (market.avg_etc_pct ?? '-') + '%';
      document.getElementById('modelCount').textContent = data.models.length;
      document.getElementById('brandPanel').innerHTML = brands.map(renderBrand).join('') || '<p>이 시장은 분류 브랜드 실측값이 없습니다.</p>';
      document.getElementById('marketRows').innerHTML = visibleMarkets().map(m => `<tr><td>${{m.display_name || m.atc4}}</td><td>${{m.scope_id || m.scope_key || m.atc4}}</td><td><span class="grade ${{m.quality_grade}}">${{m.quality_grade}}</span></td><td>${{(m.reasons || []).join(', ') || '-'}}</td></tr>`).join('');
    }}
    function renderBrand(brand) {{
      const shares = (brand.topic_shares || []).concat((brand.brand_specific_topics || []).map(s => ({{...s, label:`특화: ${{s.label}}`}})));
      const total = shares.reduce((sum, s) => sum + Number(s.share_pct || 0), 0) + Number(brand.etc_pct || 0);
      const segs = shares.concat([{{label:'기타', share_pct:brand.etc_pct || 0}}]).map((s, i) => `<div class="seg" title="${{s.label}} ${{s.share_pct}}%" style="width:${{Math.max(0, Number(s.share_pct || 0))}}%; background:${{colors[i % colors.length]}}"></div>`).join('');
      const legend = shares.concat([{{label:'기타', share_pct:brand.etc_pct || 0}}]).map((s, i) => `<span><i class="dot" style="background:${{colors[i % colors.length]}}"></i>${{s.label}} ${{s.share_pct}}%</span>`).join('');
      return `<div class="brand"><h3>${{brand.brand}} <span style="color:#607080;font-weight:500">n=${{brand.row_count || 0}}, total=${{Math.round(total * 10) / 10}}%</span></h3><div class="bar">${{segs}}</div><div class="legend">${{legend}}</div></div>`;
    }}
    marketSelect.addEventListener('change', render);
    scopeTypeSelect.addEventListener('change', render);
    modelSelect.addEventListener('change', render);
    render();
  </script>
</body>
</html>
"""


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    """Return a JSON object or an empty object."""
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue) -> list[JsonValue]:
    """Return a JSON array or an empty array."""
    return value if isinstance(value, list) else []
