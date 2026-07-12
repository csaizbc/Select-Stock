from __future__ import annotations

import html
import json


def build_strength_html(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</script", "<\\/script")
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>短线强势股</title><style>
:root{{--bg:#f4f7fb;--card:#fff;--ink:#172033;--muted:#667085;--blue:#2563eb;--line:#e4e7ec;--up:#d92d20}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 system-ui,"Microsoft YaHei",sans-serif}}
header,main{{max-width:1500px;margin:auto;padding:22px}}header{{display:flex;justify-content:space-between;gap:20px;align-items:center}}
h1{{margin:0;font-size:27px}}a.btn,button{{border:1px solid var(--line);background:#fff;padding:9px 14px;border-radius:9px;color:var(--ink);text-decoration:none;cursor:pointer}}
.hero,.rules,.tablebox{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:16px;box-shadow:0 5px 18px #1018280a}}
.meta,.note{{color:var(--muted)}}.filters{{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}}select,input{{padding:9px;border:1px solid var(--line);border-radius:8px;background:#fff}}
.rules summary{{font-weight:700;cursor:pointer}}.rules-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-top:14px}}.rule{{background:#f8fafc;padding:12px;border-radius:10px}}
.tablebox{{overflow:auto;padding:0}}table{{width:100%;border-collapse:collapse;white-space:nowrap}}th,td{{padding:11px 10px;border-bottom:1px solid var(--line);text-align:right}}th{{position:sticky;top:0;background:#f8fafc;cursor:pointer}}th:nth-child(-n+5),td:nth-child(-n+5){{text-align:left}}
.score{{font-weight:800;color:var(--blue)}}.tag{{display:inline-block;padding:2px 7px;margin:1px;border-radius:99px;background:#eaf0ff;color:#2449a4}}.risk{{color:#b42318}}.empty{{padding:40px;text-align:center}}@media(max-width:700px){{header{{align-items:flex-start;flex-direction:column}}}}
</style></head><body><header><div><h1>短线强势股</h1><div class="meta">数据日 <span id="date"></span> · 下一交易日参考 <span id="target"></span> · 收盘后更新</div></div><a class="btn" href="tomorrow_stock_list.html">查看原全市场页面 →</a></header>
<main><section class="hero"><b>定位：</b>从全市场寻找趋势、突破与缩量回踩候选。所有指标已在生成时计算，网页只负责筛选排序，因此加载不会重复计算历史行情。<div class="filters"><input id="q" placeholder="代码或名称"><select id="pattern"><option value="">全部形态</option><option>趋势强势</option><option>平台突破</option><option>缩量回踩</option></select><select id="board"><option value="">全部板块</option><option>主板</option><option>创业板</option><option>科创板</option><option>北交所</option></select><span class="note">默认仅显示20元以内</span></div><span id="count"></span></section>
<details class="rules" open><summary>选股逻辑与评分口径（点击展开/收起）</summary><div class="rules-grid">
<div class="rule"><b>基础条件</b><br>非 ST、有最新行情、上市满120日、至少60日历史行情；剔除当日已涨停或接近涨停的股票；最终分数不低于55分。</div>
<div class="rule"><b>趋势强势 · 25分</b><br>收盘价 &gt; MA5 &gt; MA10 &gt; MA20；次一级趋势得15分。</div>
<div class="rule"><b>平台突破 · 15分</b><br>收盘接近/突破此前20日最高价，同时量比≥1.2；距20日高点3%内得10分。</div>
<div class="rule"><b>缩量回踩</b><br>此前发生突破，现价距MA5或MA10不超过±1.5%，量比≤0.85，站上MA20且收阳。形态本身不额外加分，避免重复奖励。</div>
<div class="rule"><b>动量 · 20分</b><br>综合5/10/20日涨幅；5日限定-5%～25%，20日超过45%剔除，避免追逐过度加速。</div>
<div class="rule"><b>量价与流动性 · 25分</b><br>量比1.2～2.5得15分；成交额≥1亿元得10分（Tushare amount单位为千元）。</div>
<div class="rule"><b>低价与仓位 · 10分</b><br>≤8元得10分，≤12元得8分，≤15元得6分，≤20元得3分。页面默认只展示20元以内。</div>
<div class="rule"><b>收盘质量 · 5分</b><br>按收盘价处在当日高低区间的位置计分，越靠近最高价越高。</div>
<div class="rule"><b>可交易性与风险</b><br>主板涨幅≥9.5%、创业板/科创板≥19.5%、北交所≥29.5%直接剔除；放量长上影-12，短期涨幅过大-10，量比超过3倍-8。结果仅用于研究，不构成投资建议。</div>
</div></details><section class="tablebox"><table><thead><tr><th>排名</th><th>代码</th><th>名称</th><th>形态</th><th>行业</th><th data-k="score">评分</th><th>评分明细</th><th data-k="pct_chg">当日%</th><th data-k="ret5">5日%</th><th data-k="ret20">20日%</th><th data-k="volume_ratio">量比</th><th data-k="distance_high20">距20日高%</th><th>成交额(亿)</th><th>风险</th></tr></thead><tbody id="body"></tbody></table></section></main>
<script id="data" type="application/json">{data}</script><script>
const p=JSON.parse(document.getElementById('data').textContent), all=(p.rows||[]).filter(x=>x.close<=20);let key='score',dir=-1;
date.textContent=p.meta.data_date;target.textContent=p.meta.target_trade_date;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
function render(){{let s=q.value.trim().toLowerCase(),rows=all.filter(x=>(!s||(x.ts_code+x.name).toLowerCase().includes(s))&&(!pattern.value||x.patterns.includes(pattern.value))&&(!board.value||x.board===board.value));rows.sort((a,b)=>dir*((a[key]??-999)-(b[key]??-999)));count.textContent=`当前 ${{rows.length}} 只（最多展示 ${{all.length}} 只候选）`;body.innerHTML=rows.map((x,i)=>`<tr><td>${{i+1}}</td><td>${{esc(x.ts_code)}}</td><td>${{esc(x.name)}}</td><td>${{x.patterns.map(v=>`<span class="tag">${{esc(v)}}</span>`).join('')}}</td><td>${{esc(x.industry)}}</td><td class="score">${{x.score}}</td><td title="${{esc(JSON.stringify(x.score_detail))}}">${{Object.entries(x.score_detail).map(([k,v])=>`${{k}}:${{v}}`).join(' / ')}}</td><td>${{x.pct_chg}}</td><td>${{x.ret5}}</td><td>${{x.ret20}}</td><td>${{x.volume_ratio}}</td><td>${{x.distance_high20}}</td><td>${{x.amount_yi}}</td><td class="risk">${{esc(x.risk_flags.join('、')||'—')}}</td></tr>`).join('')||'<tr><td class="empty" colspan="14">没有符合当前条件的候选</td></tr>'}}
[q,pattern,board].forEach(e=>e.addEventListener('input',render));document.querySelectorAll('th[data-k]').forEach(h=>h.onclick=()=>{{dir=key===h.dataset.k?-dir:-1;key=h.dataset.k;render()}});render();
</script></body></html>'''
