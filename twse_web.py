#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
twse_web.py — 台股「注意 / 處置」監控網頁版
============================================
本機小型網頁伺服器:重用 twse_watch.py 的抓取/解析/風險試算/歷史資料庫,
把結果做成一頁式儀表板。

    python twse_web.py            # 啟動後開 http://localhost:8765

- 資料快取 10 分鐘;按「重新整理資料」可強制更新。
- 每次抓取都會寫入 surveillance.db,歷史自動累積。
"""

import os
import sys
import json
import time
import threading
import datetime as dt
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# 讓「從任何工作目錄啟動」都能找到同資料夾的 twse_watch.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twse_watch as tw

PORT = 8765
CACHE_TTL = 600  # 秒

_cache = {"ts": 0.0, "recs": None}
_lock = threading.Lock()


def get_records(force=False):
    """帶快取的抓取;每次真正抓取都寫入歷史 DB。"""
    with _lock:
        now = time.time()
        if not force and _cache["recs"] is not None and now - _cache["ts"] < CACHE_TTL:
            return _cache["recs"], _cache["ts"]
        data = tw.fetch_all()
        recs = tw.collect_records(data)
        if recs:  # 全空(斷線)時保留舊快取
            conn = tw.db_conn()
            tw.store_snapshot(conn, recs)
            conn.close()
            _cache["recs"] = recs
            _cache["ts"] = now
        elif _cache["recs"] is None:
            _cache["recs"] = []
            _cache["ts"] = now
        return _cache["recs"], _cache["ts"]


def overview_payload(force=False):
    recs, ts = get_records(force)
    attention = []
    for r in recs:
        if r["kind"] != "attention":
            continue
        parsed = tw.parse_attention_text(r["detail"])
        buffers, min_days, level = tw.risk_from_attention(parsed)
        attention.append({
            "code": r["code"], "name": r["name"], "market": r["market"],
            "text": parsed["text"], "min_days": min_days, "level": level,
            "buffers": buffers,
        })
    attention.sort(key=lambda x: (x["min_days"], x["code"]))

    dispositions = []
    for r in recs:
        if r["kind"] != "disposition":
            continue
        d = r["detail"]
        dispositions.append({
            "code": r["code"], "name": r["name"], "market": r["market"],
            "reason": d.get("reason", ""), "period": d.get("period", ""),
            "measure": d.get("measure", ""),
        })
    dispositions.sort(key=lambda x: x["code"])

    return {
        "fetched_at": dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "",
        "attention": attention,
        "dispositions": dispositions,
    }


def stock_payload(code):
    recs, _ = get_records()
    code = code.strip()
    cur_att = [r for r in recs if r["kind"] == "attention" and r["code"] == code]
    cur_dis = [r for r in recs if r["kind"] == "disposition" and r["code"] == code]

    name = ""
    for r in cur_att + cur_dis:
        if r["name"]:
            name = r["name"]
            break

    out = {"code": code, "name": name, "status": "clear",
           "attention": None, "dispositions": [], "history": {}}

    if cur_dis:
        out["status"] = "disposed"
        for r in cur_dis:
            d = r["detail"]
            out["dispositions"].append({
                "reason": d.get("reason", ""), "period": d.get("period", ""),
                "measure": d.get("measure", ""), "market": r["market"],
            })
    elif cur_att:
        out["status"] = "attention"

    if cur_att:
        r = cur_att[0]
        parsed = tw.parse_attention_text(r["detail"])
        buffers, min_days, level = tw.risk_from_attention(parsed)
        out["attention"] = {"text": parsed["text"], "min_days": min_days,
                            "level": level, "buffers": buffers, "market": r["market"]}

    # 歷史(本機 DB)
    conn = tw.db_conn()
    hist = conn.execute(
        "SELECT fetch_date, kind, detail FROM snapshots WHERE code=? ORDER BY fetch_date",
        (code,)).fetchall()
    conn.close()
    seen_periods = set()
    dis_events = []
    att_days = []
    for fdate, kind, detail_s in hist:
        if kind == "disposition":
            try:
                d = json.loads(detail_s)
            except Exception:
                d = {"period": detail_s}
            p = d.get("period", "")
            if p and p not in seen_periods:
                seen_periods.add(p)
                dis_events.append({"first_seen": fdate, "period": p,
                                   "reason": d.get("reason", "")})
        else:
            if fdate not in att_days:
                att_days.append(fdate)
    out["history"] = {"disposition_events": dis_events, "attention_days": att_days}
    return out


# ---------------------------------------------------------------------------
# HTML(單頁,無外部資源)
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>台股注意/處置監控</title>
<style>
:root{
  --bg:#f7f7f8; --surface:#ffffff; --ink:#1a1a1e; --ink2:#5c5c66; --ink3:#8a8a94;
  --line:#e4e4e9; --accent:#2563eb;
  --good:#1a7f37; --good-bg:#e6f4ea;
  --warn:#9a6700; --warn-bg:#fff4d5;
  --serious:#bc4c00; --serious-bg:#ffe8d7;
  --critical:#c0362c; --critical-bg:#fdebe9;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#131316; --surface:#1d1d22; --ink:#ececf1; --ink2:#a6a6b0; --ink3:#77777f;
    --line:#2e2e35; --accent:#7ca7f7;
    --good:#57ab5a; --good-bg:#122117;
    --warn:#c69026; --warn-bg:#2a2214;
    --serious:#e0823d; --serious-bg:#2b1d12;
    --critical:#e5534b; --critical-bg:#2d1615;
  }
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);
  font-family:"Segoe UI","Microsoft JhengHei",system-ui,sans-serif;
  font-size:15px;line-height:1.55}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:1.35rem;font-weight:650}
#fetched{color:var(--ink3);font-size:.82rem}
#refresh{margin-left:auto;border:1px solid var(--line);background:var(--surface);
  color:var(--ink2);border-radius:8px;padding:6px 14px;cursor:pointer;font-size:.85rem}
#refresh:hover{border-color:var(--accent);color:var(--accent)}
.disclaimer{color:var(--ink3);font-size:.8rem;margin-bottom:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile .n{font-size:1.7rem;font-weight:700;font-variant-numeric:tabular-nums}
.tile .t{color:var(--ink2);font-size:.82rem;margin-top:2px}
nav{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:16px;flex-wrap:wrap}
nav button{border:none;background:none;color:var(--ink2);padding:9px 16px;cursor:pointer;
  font-size:.95rem;border-bottom:2px solid transparent}
nav button.on{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
section{display:none}section.on{display:block}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;margin-bottom:12px}
table{width:100%;border-collapse:collapse}
th{color:var(--ink3);font-size:.78rem;font-weight:600;text-align:left;
  padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
tr.rowlink{cursor:pointer}
tr.rowlink:hover td{background:color-mix(in srgb,var(--accent) 6%,transparent)}
.code{font-variant-numeric:tabular-nums;font-weight:600}
.mkt{color:var(--ink3);font-size:.8rem}
.crit-text{color:var(--ink2);font-size:.85rem}
.badge{display:inline-flex;align-items:center;gap:5px;border-radius:999px;
  padding:2px 10px;font-size:.78rem;font-weight:600;white-space:nowrap}
.b-critical{background:var(--critical-bg);color:var(--critical)}
.b-serious{background:var(--serious-bg);color:var(--serious)}
.b-warn{background:var(--warn-bg);color:var(--warn)}
.b-good{background:var(--good-bg);color:var(--good)}
.searchbar{display:flex;gap:8px;margin-bottom:14px}
.searchbar input{flex:0 1 260px;background:var(--surface);border:1px solid var(--line);
  color:var(--ink);border-radius:8px;padding:8px 12px;font-size:.95rem}
.searchbar button{border:1px solid var(--accent);background:var(--accent);color:#fff;
  border-radius:8px;padding:8px 18px;cursor:pointer;font-size:.9rem}
.kv{margin:6px 0 0;color:var(--ink2);font-size:.88rem}
.kv b{color:var(--ink)}
.bufline{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:.87rem;color:var(--ink2)}
.bufbar{height:8px;border-radius:4px;background:var(--line);flex:0 0 160px;overflow:hidden}
.bufbar i{display:block;height:100%;border-radius:4px}
.measure{color:var(--ink2);font-size:.84rem;overflow-wrap:anywhere}
.empty{color:var(--ink3);padding:24px;text-align:center}
h2{font-size:1.02rem;font-weight:650;margin-bottom:10px}
h3{font-size:.92rem;font-weight:650;margin:14px 0 6px}
.rules p,.rules li{color:var(--ink2);font-size:.9rem;margin-bottom:6px}
.rules b{color:var(--ink)}
.rules ul{padding-left:20px}
.note{background:var(--warn-bg);color:var(--warn);border-radius:8px;
  padding:10px 14px;font-size:.85rem;margin-top:14px}
#loading{color:var(--ink3);padding:30px;text-align:center}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>台股注意 / 處置監控</h1>
  <span id="fetched"></span>
  <button id="refresh">重新整理資料</button>
</header>
<p class="disclaimer">資料來源:TWSE / TPEx 公開 OpenAPI。本頁為風險資訊參考,非投資建議;門檻以主管機關最新公告為準。</p>

<div class="tiles">
  <div class="tile"><div class="n" id="t-att">–</div><div class="t">注意股(檔)</div></div>
  <div class="tile"><div class="n" id="t-crit">–</div><div class="t">再 1 次注意即處置</div></div>
  <div class="tile"><div class="n" id="t-dis">–</div><div class="t">處置中(檔)</div></div>
</div>

<nav>
  <button data-tab="attention" class="on">⚠️ 注意股</button>
  <button data-tab="disposition">⛔ 處置中</button>
  <button data-tab="stock">🔍 個股查詢</button>
  <button data-tab="rules">📖 規則說明</button>
</nav>

<section id="attention" class="on"><div id="loading">載入中…</div></section>

<section id="disposition"></section>

<section id="stock">
  <div class="searchbar">
    <input id="q" placeholder="輸入股票代號,例如 2330" inputmode="numeric">
    <button id="go">查詢</button>
  </div>
  <div id="stock-result"></div>
</section>

<section id="rules"><div class="card rules">
  <h2>注意 → 處置 規則速查</h2>
  <h3>一、什麼是注意股?</h3>
  <p>交易所每天收盤後依「注意交易資訊暨處置作業要點 第四條」檢查各股,當日價量觸及任一款即公布注意:</p>
  <ul>
    <li><b>第一款</b>:最近 6 個營業日累積漲跌幅過大且量能同步異常(最主要,也是最快通往處置的一款)</li>
    <li>第二~三款:週轉率過高;第四款:本益比為負/過高、股價淨值比異常</li>
    <li>第五款:成交量急遽放大;第六款:券資比異常;第七款:當沖比率過高</li>
    <li>第八款:券商買賣超集中度過高;第九款:其他價量與大盤背離</li>
  </ul>
  <h3>二、什麼時候被處置?(擇一即處置)</h3>
  <ul>
    <li><b>連續 3 個營業日</b>中「第一款」 ← <b>最快路徑(3 天)</b></li>
    <li><b>連續 5 個營業日</b>、或<b>最近 10 日內 6 次</b>、或<b>最近 30 日內 12 次</b>中「第一~八款」任一</li>
  </ul>
  <h3>三、處置後交易怎麼受限?</h3>
  <ul>
    <li><b>第一次處置</b>:10 個營業日改人工管制撮合(約每 5 分鐘一次);每日委託達一定數量須<b>預收全額款券</b></li>
    <li><b>30 日內第二次處置</b>:撮合間隔放寬至約每 20 分鐘一次,限制更嚴</li>
    <li>處置 ≠ 下市,但流動性明顯變差、資金卡預收,進出成本大增</li>
  </ul>
  <div class="note">⚠️ 本工具協助的是「掌握距離處置的緩衝、評估持股流動性風險」的合規風控。
  人為影響成交量價以規避監視門檻,屬證交法第 155 條操縱市場行為,不在本工具範圍。</div>
</div></section>

</div>
<script>
let DATA=null;
const $=s=>document.querySelector(s);

function badge(minDays){
  if(minDays<=0) return '<span class="badge b-critical">⛔ 已達門檻</span>';
  if(minDays===1) return '<span class="badge b-critical">🔴 再 1 次即處置</span>';
  if(minDays===2) return '<span class="badge b-serious">🟠 再 2 次即處置</span>';
  if(minDays===3) return '<span class="badge b-warn">🟡 再 3 次即處置</span>';
  return '<span class="badge b-good">🟢 觀察中(差 '+minDays+' 次)</span>';
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

function renderOverview(d){
  DATA=d;
  $('#fetched').textContent='資料時間 '+d.fetched_at;
  $('#t-att').textContent=d.attention.length;
  $('#t-crit').textContent=d.attention.filter(a=>a.min_days<=1).length;
  $('#t-dis').textContent=d.dispositions.length;

  // 注意股表
  let h='';
  if(!d.attention.length){h='<div class="card empty">目前沒有注意股 🎉</div>';}
  else{
    h='<div class="card"><table><thead><tr><th>代號</th><th>名稱</th><th>市場</th><th>處置風險</th><th>注意紀錄</th></tr></thead><tbody>';
    for(const a of d.attention){
      h+='<tr class="rowlink" onclick="openStock(\''+a.code+'\')">'
        +'<td class="code">'+esc(a.code)+'</td><td>'+esc(a.name)+'</td>'
        +'<td class="mkt">'+esc(a.market)+'</td><td>'+badge(a.min_days)+'</td>'
        +'<td class="crit-text">'+esc(a.text)+'</td></tr>';
    }
    h+='</tbody></table></div><p class="disclaimer">點任一列可看個股詳情。「處置風險」= 依目前連續/累積次數,最少再被列幾次注意就會處置;每天是否再中注意由當日價量決定。</p>';
  }
  $('#attention').innerHTML=h;

  // 處置表
  let g='';
  if(!d.dispositions.length){g='<div class="card empty">目前沒有處置中的證券</div>';}
  else{
    g='<div class="card"><table><thead><tr><th>代號</th><th>名稱</th><th>市場</th><th>處置期間</th><th>原因</th></tr></thead><tbody>';
    for(const x of d.dispositions){
      g+='<tr class="rowlink" onclick="openStock(\''+x.code+'\')">'
        +'<td class="code">'+esc(x.code)+'</td><td>'+esc(x.name)+'</td>'
        +'<td class="mkt">'+esc(x.market)+'</td><td class="crit-text">'+esc(x.period)+'</td>'
        +'<td class="crit-text">'+esc(x.reason)+'</td></tr>';
    }
    g+='</tbody></table></div>';
  }
  $('#disposition').innerHTML=g;
}

function bufHtml(buffers,minDays){
  let h='';
  const denom={'連續3次(第一款,最快)':3,'連續5次(綜合)':5,'最近10日內6次':6,'最近30日內12次':12};
  for(const k in buffers){
    const left=buffers[k], total=denom[k]||5, done=Math.max(0,total-left);
    const pct=Math.min(100,Math.round(done/total*100));
    const col=left<=1?'var(--critical)':(left<=2?'var(--serious)':'var(--warn)');
    h+='<div class="bufline"><span class="bufbar"><i style="width:'+pct+'%;background:'+col+'"></i></span>'
      +esc(k)+':已 '+done+'/'+total+',還差 <b>'+left+'</b> 次'+(left===minDays?' ← 最快':'')+'</div>';
  }
  return h;
}

function renderStock(s){
  let h='<div class="card"><h2>'+esc(s.code)+' '+esc(s.name||'')+'</h2>';
  if(s.status==='disposed'){
    h+='<p class="kv"><span class="badge b-critical">⛔ 處置中</span></p>';
    for(const d of s.dispositions){
      h+='<p class="kv"><b>期間</b>:'+esc(d.period)+'(' +esc(d.market)+')<br><b>原因</b>:'+esc(d.reason)
        +'</p><p class="measure">'+esc(d.measure)+'</p>';
    }
  }else if(s.status==='attention'){
    h+='<p class="kv"><span class="badge b-serious">⚠️ 被列注意(尚未處置)</span></p>';
  }else{
    h+='<p class="kv"><span class="badge b-good">✅ 不在注意/處置清單</span></p>';
  }
  if(s.attention){
    h+='<p class="kv"><b>注意內容</b>:'+esc(s.attention.text)+'</p>'
      +'<p class="kv">'+badge(s.attention.min_days)+'</p>'
      +(s.attention.min_days>0
        ?'<p class="kv">最快情境:接下來再被列注意 <b>'+s.attention.min_days+'</b> 次即觸發第一次處置。</p>'
        :'<p class="kv">已達處置門檻,次一交易日起很可能公告處置。</p>')
      +'<h3>各門檻進度</h3>'+bufHtml(s.attention.buffers,s.attention.min_days);
  }
  h+='</div>';

  // 歷史
  h+='<div class="card"><h2>歷史紀錄(本機累積)</h2>';
  const ev=s.history.disposition_events||[], ad=s.history.attention_days||[];
  if(!ev.length&&!ad.length){
    h+='<p class="kv">尚無歷史。此頁每次載入資料都會寫入本機資料庫,持續使用即可累積該檔的注意/處置紀錄。</p>';
  }else{
    if(ev.length){
      h+='<h3>處置事件</h3><table><thead><tr><th>處置期間</th><th>原因</th><th>首次記錄</th></tr></thead><tbody>';
      for(const e of ev){h+='<tr><td class="crit-text">'+esc(e.period)+'</td><td class="crit-text">'+esc(e.reason)+'</td><td class="mkt">'+esc(e.first_seen)+'</td></tr>';}
      h+='</tbody></table>';
    }else{h+='<p class="kv">處置事件:無</p>';}
    if(ad.length){h+='<p class="kv" style="margin-top:10px"><b>被列注意天數</b>:'+ad.length+' 天(最近:'+esc(ad.slice(-5).join('、'))+')</p>';}
  }
  h+='</div>';
  $('#stock-result').innerHTML=h;
}

async function loadOverview(force){
  $('#fetched').textContent='載入中…';
  const r=await fetch('/api/overview'+(force?'?force=1':''));
  renderOverview(await r.json());
}
async function query(code){
  if(!code)return;
  $('#stock-result').innerHTML='<div class="card empty">查詢中…</div>';
  const r=await fetch('/api/stock?code='+encodeURIComponent(code));
  renderStock(await r.json());
}
function openStock(code){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.tab==='stock'));
  document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id==='stock'));
  $('#q').value=code; query(code);
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');
  document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id===b.dataset.tab));
});
$('#go').onclick=()=>query($('#q').value.trim());
$('#q').addEventListener('keydown',e=>{if(e.key==='Enter')query($('#q').value.trim())});
$('#refresh').onclick=()=>loadOverview(true);
loadOverview(false);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json; charset=utf-8", status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/":
                self._send(PAGE, "text/html; charset=utf-8")
            elif u.path == "/api/overview":
                force = parse_qs(u.query).get("force", ["0"])[0] == "1"
                self._send(json.dumps(overview_payload(force), ensure_ascii=False))
            elif u.path == "/api/stock":
                code = parse_qs(u.query).get("code", [""])[0]
                self._send(json.dumps(stock_payload(code), ensure_ascii=False))
            else:
                self._send('{"error":"not found"}', status=404)
        except Exception as e:
            self._send(json.dumps({"error": str(e)}, ensure_ascii=False), status=500)

    def log_message(self, fmt, *args):
        pass  # 安靜模式


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"台股注意/處置監控 → http://localhost:{PORT}  (Ctrl+C 結束)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
