#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
twse_watch.py  —  台股「注意 / 處置」監控與風險試算工具
=========================================================

功能
----
1. 抓取上市(TWSE)與上櫃(TPEx)最新的「注意股」與「處置股」清單(公開 OpenAPI)。
2. 解析注意清單裡的「連續 N 次 / 最近 W 個營業日已有 M 次」,換算離處置還差幾次,
   以及「最快幾個交易日會被處置」的情境。
3. 查詢特定個股目前狀態(是否被注意/處置、還剩多少緩衝)。
4. 把每次抓取的結果存進本機 SQLite,累積「處置/注意歷史」,可回查某檔的歷史紀錄。
5. rules 指令:把注意→處置的判定規則與處置措施用白話列出來。

免責 / 定位
-----------
- 這是「合規的風險控管 / 教育」工具:幫你看清楚一檔股票離處置還有多少緩衝、
  處置後交易會怎麼被限制(人工撮合、預收款券),以利判斷要不要進出或控管自己的下單。
- 每天「有沒有中注意」是由 TWSE/TPEx 依當日價量門檻認定並公告;本工具是「讀公告 + 數次數」,
  不是預測明天會不會中,也不涉及任何操縱價量的手法。

用法
----
    python twse_watch.py update            # 抓取最新資料並存檔(建議每個交易日收盤後跑)
    python twse_watch.py attention         # 列出注意股,依「離處置最近」排序
    python twse_watch.py disposition       # 列出目前處置中的股票(含處置期間與措施)
    python twse_watch.py stock 2330        # 查單一個股:目前狀態 + 緩衝 + 歷史
    python twse_watch.py rules             # 顯示注意/處置規則說明

需求:Python 3.8+,標準庫即可(urllib / sqlite3),不需額外安裝套件。
"""

import sys
import io
import os
import re
import json
import ssl
import sqlite3
import argparse
import datetime as dt
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# Windows 主控台常常不是 UTF-8,強制輸出 UTF-8 以免中文亂碼/報錯
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "surveillance.db")

# ---------------------------------------------------------------------------
# 資料來源(公開 OpenAPI)
# ---------------------------------------------------------------------------
SOURCES = {
    # 上市 注意交易資訊(含「連續/累積次數」文字)
    "twse_attention":   "https://openapi.twse.com.tw/v1/announcement/notetrans",
    # 上市 處置有價證券
    "twse_disposition": "https://openapi.twse.com.tw/v1/announcement/punish",
    # 上櫃 公布注意累計次數異常資訊(含「連續/累積次數」文字)
    "tpex_attention":   "https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_note",
    # 上櫃 處置有價證券
    "tpex_disposition": "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information",
    # 上市 當日注意公告(含觸發款別與數值)
    "twse_attention_daily": "https://openapi.twse.com.tw/v1/announcement/notice",
    # 上櫃 當日注意公告(含觸發款別與數值)
    "tpex_attention_daily": "https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information",
    # 興櫃 當日注意公告(含觸發款別與數值)
    "esb_attention_daily":  "https://www.tpex.org.tw/openapi/v1/tpex_esb_warning_information",
    # 興櫃 處置
    "esb_disposition":      "https://www.tpex.org.tw/openapi/v1/tpex_esb_disposal_information",
}

# ---------------------------------------------------------------------------
# 中文數字解析(支援 0~99,足夠涵蓋「連續五次 / 三十個營業日 / 十二次」)
# ---------------------------------------------------------------------------
_CN_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(s):
    """把中文數字字串轉成 int,失敗回傳 None。支援 十/二十三/三十 這類。"""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    # 已是阿拉伯數字
    if s.isdigit():
        return int(s)
    if "十" not in s:
        # 純個位或連寫(理論上不會出現多位無十的中文,取第一個字)
        return _CN_DIGIT.get(s[0])
    # 含「十」
    if s == "十":
        return 10
    parts = s.split("十")
    left, right = parts[0], parts[1] if len(parts) > 1 else ""
    tens = _CN_DIGIT.get(left, 1) if left else 1   # 「十」開頭 => 1x
    ones = _CN_DIGIT.get(right, 0) if right else 0
    return tens * 10 + ones


# ---------------------------------------------------------------------------
# 處置門檻(依「臺灣證券交易所/櫃買中心 公布或通知注意交易資訊暨處置作業要點」)
# ---------------------------------------------------------------------------
# 第一次處置的觸發(擇一即處置):
#   A. 連續 3 個營業日 達第四條第一項「第一款」(價量主要條件)          -> 最快路徑
#   B. 連續 5 個營業日,或最近 10 日內有 6 日,或最近 30 日內有 12 日
#      達第四條第一項「第一款至第八款」任一               -> 綜合計數
THRESH = {
    "consecutive_ruleA": 3,   # 連續3次(第一款)即處置 —— 這是「最快」的路徑
    "consecutive_ruleB": 5,   # 連續5次(綜合)
    "window10_count": 6,      # 最近10個營業日內6次
    "window30_count": 12,     # 最近30個營業日內12次
}


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def _make_context():
    """
    建立 TLS context:維持完整憑證驗證(驗信任鏈 + 主機名),
    但清除 OpenSSL 3.x 過度嚴格的 VERIFY_X509_STRICT 旗標。
    TWSE/TPEx 的政府憑證鏈缺少 Subject Key Identifier 等 RFC 選用擴充,
    在嚴格模式下會被判 'Missing Subject Key Identifier' 而失敗;
    清掉這個旗標即可正常驗證,而『不』降級為不驗證憑證。
    """
    ctx = ssl.create_default_context()
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict:
        ctx.verify_flags &= ~strict
    return ctx


_SSL_CTX = _make_context()


def fetch_json(url, timeout=20):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 twse-watch/1.0",
                                "Accept": "application/json"})
    with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        raw = resp.read().decode("utf-8-sig", errors="replace")
    return json.loads(raw)


def fetch_all():
    """回傳 {source_key: [records...]},抓不到的來源給空 list 並印警告。"""
    out = {}
    for key, url in SOURCES.items():
        try:
            data = fetch_json(url)
            out[key] = data if isinstance(data, list) else []
        except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, ValueError) as e:
            print(f"[警告] 無法取得 {key} ({url}):{e}", file=sys.stderr)
            out[key] = []
    return out


# ---------------------------------------------------------------------------
# 注意清單解析:抽出「連續次數」與「視窗內次數」
# ---------------------------------------------------------------------------
_CN_NUM = r"[〇零一二三四五六七八九十]+"


def parse_attention_text(text):
    """
    解析像這樣的字串:
      "115年7月2日至115年7月15日等九個營業日已有五次"      -> window_days=9, window_count=5
      "115年07月15日至115年07月16日連續二次"               -> consecutive=2
    回傳 dict: {consecutive, window_days, window_count, text}
    """
    res = {"consecutive": None, "window_days": None, "window_count": None, "text": text or ""}
    if not text:
        return res

    m = re.search(r"連續(" + _CN_NUM + r"|\d+)次", text)
    if m:
        res["consecutive"] = cn_to_int(m.group(1))

    m2 = re.search(r"(" + _CN_NUM + r"|\d+)個營業日", text)
    if m2:
        res["window_days"] = cn_to_int(m2.group(1))
    # 「已有M次 / 有M次 / 達M次」(排除已被上面連續抓走的情況)
    m3 = re.search(r"(?:已有|有|達)(" + _CN_NUM + r"|\d+)次", text)
    if m3:
        res["window_count"] = cn_to_int(m3.group(1))

    return res


def risk_from_attention(parsed):
    """
    依解析結果算出:
      - buffers: 各門檻還差幾次(dict)
      - min_days: 最快還需幾個「再中注意」的交易日就會被處置
      - level: 風險等級文字
    """
    c = parsed.get("consecutive")
    wc = parsed.get("window_count")
    wd = parsed.get("window_days")

    buffers = {}
    candidates = []

    if c is not None:
        b = max(0, THRESH["consecutive_ruleA"] - c)
        buffers["連續3次(第一款,最快)"] = b
        candidates.append(b)
        b5 = max(0, THRESH["consecutive_ruleB"] - c)
        buffers["連續5次(綜合)"] = b5
        candidates.append(b5)

    if wc is not None:
        # 視窗內次數:視 window_days 判斷比較貼近哪個門檻
        if wd is not None and wd <= 10:
            b10 = max(0, THRESH["window10_count"] - wc)
            buffers["最近10日內6次"] = b10
            candidates.append(b10)
        b30 = max(0, THRESH["window30_count"] - wc)
        buffers["最近30日內12次"] = b30
        candidates.append(b30)

    if not candidates:
        # 有被列注意但格式無法解析 —— 保守給「重新起算連續3次」
        min_days = THRESH["consecutive_ruleA"]
    else:
        min_days = min(candidates)

    if min_days <= 0:
        level = "🔴 已達/瀕臨處置門檻"
    elif min_days == 1:
        level = "🔴 極高(再中 1 次注意即處置)"
    elif min_days == 2:
        level = "🟠 高(再中 2 次注意即處置)"
    elif min_days <= 3:
        level = "🟡 中(約再 3 個交易日中注意即處置)"
    else:
        level = "🟢 觀察中"
    return buffers, min_days, level


# ---------------------------------------------------------------------------
# 正規化各來源記錄成統一格式
# ---------------------------------------------------------------------------
def normalize(source_key, rec):
    """回傳 dict: market, kind(attention/disposition), code, name, detail(dict/str)."""
    if source_key == "twse_attention":
        return {"market": "上市", "kind": "attention",
                "code": str(rec.get("Code", "")).strip(),
                "name": rec.get("Name", "").strip(),
                "detail": rec.get("RecentlyMetAttentionSecuritiesCriteria", "").strip()}
    if source_key == "tpex_attention":
        return {"market": "上櫃", "kind": "attention",
                "code": str(rec.get("SecuritiesCompanyCode", "")).strip(),
                "name": rec.get("CompanyName", "").strip(),
                "detail": rec.get("AccumulationSituation", "").strip()}
    if source_key == "twse_disposition":
        return {"market": "上市", "kind": "disposition",
                "code": str(rec.get("Code", "")).strip(),
                "name": rec.get("Name", "").strip(),
                "detail": {"reason": rec.get("ReasonsOfDisposition", "").strip(),
                           "period": rec.get("DispositionPeriod", "").strip(),
                           "measure": rec.get("DispositionMeasures", "").strip(),
                           "anndate": rec.get("Date", "").strip()}}
    if source_key == "tpex_disposition":
        return {"market": "上櫃", "kind": "disposition",
                "code": str(rec.get("SecuritiesCompanyCode", "")).strip(),
                "name": rec.get("CompanyName", "").strip(),
                "detail": {"reason": rec.get("DispositionReasons", "").strip(),
                           "period": rec.get("DispositionPeriod", "").strip(),
                           "measure": rec.get("DisposalCondition", "").strip(),
                           "anndate": rec.get("Date", "").strip()}}
    if source_key == "esb_disposition":
        return {"market": "興櫃", "kind": "disposition",
                "code": str(rec.get("證券代號", "")).strip(),
                "name": rec.get("證券名稱", "").strip(),
                "detail": {"reason": rec.get("處置原因", "").strip(),
                           "period": rec.get("處置起訖時間", "").strip(),
                           "measure": rec.get("處置內容", "").strip(),
                           "anndate": rec.get("公布日期", "").strip()}}
    if source_key == "twse_attention_daily":
        return {"market": "上市", "kind": "attention_daily",
                "code": str(rec.get("Code", "")).strip(),
                "name": rec.get("Name", "").strip(),
                "detail": {"info": rec.get("TradingInfoForAttention", "").strip(),
                           "date": rec.get("Date", "").strip(),
                           "close": rec.get("ClosingPrice", "").strip(),
                           "pe": rec.get("PE", "").strip()}}
    if source_key == "tpex_attention_daily":
        return {"market": "上櫃", "kind": "attention_daily",
                "code": str(rec.get("SecuritiesCompanyCode", "")).strip(),
                "name": rec.get("CompanyName", "").strip(),
                "detail": {"info": rec.get("TradingInformation", "").strip(),
                           "date": rec.get("Date", "").strip(),
                           "close": rec.get("ClosePrice", "").strip(),
                           "pe": rec.get("PriceEarningRatio", "").strip()}}
    if source_key == "esb_attention_daily":
        return {"market": "興櫃", "kind": "attention_daily",
                "code": str(rec.get("證券代號", "")).strip(),
                "name": rec.get("證券名稱", "").strip(),
                "detail": {"info": rec.get("注意交易資訊", "").strip(),
                           "date": rec.get("公告日期", "").strip(),
                           "close": rec.get("收盤價", "").strip(),
                           "pe": ""}}
    return None


# ---------------------------------------------------------------------------
# SQLite 儲存(累積歷史)
# ---------------------------------------------------------------------------
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots(
            fetch_date TEXT,   -- 抓取日 YYYY-MM-DD
            market     TEXT,
            kind       TEXT,   -- attention / disposition
            code       TEXT,
            name       TEXT,
            detail     TEXT,   -- JSON 或純文字
            UNIQUE(fetch_date, kind, code, detail)
        )""")
    conn.commit()
    return conn


def store_snapshot(conn, records):
    today = dt.date.today().isoformat()
    n = 0
    for r in records:
        detail = r["detail"]
        detail_s = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else str(detail)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO snapshots(fetch_date,market,kind,code,name,detail) VALUES(?,?,?,?,?,?)",
                (today, r["market"], r["kind"], r["code"], r["name"], detail_s))
            if conn.total_changes:
                n += conn.total_changes
        except sqlite3.Error:
            pass
    conn.commit()
    return n


def collect_records(data):
    recs = []
    for key, rows in data.items():
        for rec in rows:
            nrec = normalize(key, rec)
            if nrec and nrec["code"]:
                recs.append(nrec)
    return recs


# ---------------------------------------------------------------------------
# 指令
# ---------------------------------------------------------------------------
def cmd_update(args):
    data = fetch_all()
    recs = collect_records(data)
    conn = db_conn()
    added = store_snapshot(conn, recs)
    att = [r for r in recs if r["kind"] == "attention"]
    dis = [r for r in recs if r["kind"] == "disposition"]
    print(f"抓取完成({dt.date.today().isoformat()}):注意 {len(att)} 檔、處置 {len(dis)} 檔;"
          f"本次新增歷史紀錄 {added} 筆 -> {DB_PATH}")


def _attention_ranked(recs):
    rows = []
    for r in recs:
        if r["kind"] != "attention":
            continue
        parsed = parse_attention_text(r["detail"])
        buffers, min_days, level = risk_from_attention(parsed)
        rows.append((min_days, r, parsed, buffers, level))
    rows.sort(key=lambda x: (x[0], x[1]["code"]))
    return rows


def cmd_attention(args):
    data = fetch_all()
    recs = collect_records(data)
    conn = db_conn()
    store_snapshot(conn, recs)
    rows = _attention_ranked(recs)
    if not rows:
        print("目前沒有注意股(或資料源暫時無法取得)。")
        return
    print(f"== 注意股清單(共 {len(rows)} 檔,依『離處置最近』排序)==\n")
    print(f"{'代號':<7}{'名稱':<9}{'市場':<5}{'最快處置':<9}風險 / 說明")
    print("-" * 78)
    for min_days, r, parsed, buffers, level in rows:
        fastest = "已達門檻" if min_days <= 0 else f"再{min_days}次"
        print(f"{r['code']:<7}{r['name']:<9}{r['market']:<5}{fastest:<9}{level}")
        print(f"         └ {parsed['text']}")
    print("\n提示:『最快處置』= 依目前連續/累積次數,最少還要再被列幾次注意就會被處置。")
    print("      每天會不會再中注意,由當日價量是否觸及門檻決定(見 rules)。")


def cmd_disposition(args):
    data = fetch_all()
    recs = collect_records(data)
    conn = db_conn()
    store_snapshot(conn, recs)
    dis = [r for r in recs if r["kind"] == "disposition"]
    if not dis:
        print("目前沒有處置股(或資料源暫時無法取得)。")
        return
    dis.sort(key=lambda r: r["code"])
    print(f"== 處置中證券(共 {len(dis)} 檔)==\n")
    for r in dis:
        d = r["detail"]
        print(f"[{r['market']}] {r['code']} {r['name']}")
        print(f"    處置原因:{d.get('reason','')}")
        print(f"    處置期間:{d.get('period','')}")
        meas = d.get('measure', '')
        if len(meas) > 90:
            meas = meas[:90] + "…"
        print(f"    處置措施:{meas}")
        print()


def cmd_stock(args):
    code = args.code.strip()
    data = fetch_all()
    recs = collect_records(data)
    conn = db_conn()
    store_snapshot(conn, recs)

    cur_att = [r for r in recs if r["kind"] == "attention" and r["code"] == code]
    cur_dis = [r for r in recs if r["kind"] == "disposition" and r["code"] == code]

    name = ""
    for r in cur_att + cur_dis:
        if r["name"]:
            name = r["name"]
            break

    print(f"===== {code} {name} 監控狀態 =====\n")

    # 目前是否處置中
    if cur_dis:
        print("狀態:🔴 目前『處置中』")
        for r in cur_dis:
            d = r["detail"]
            print(f"  處置期間:{d.get('period','')}  ({d.get('measure','')[:40]})")
            print(f"  原因:{d.get('reason','')}")
        print()
    elif cur_att:
        print("狀態:🟠 目前被列『注意』(尚未處置)")
    else:
        print("狀態:🟢 目前不在注意/處置清單上")

    # 注意 -> 處置 緩衝試算
    if cur_att:
        for r in cur_att:
            parsed = parse_attention_text(r["detail"])
            buffers, min_days, level = risk_from_attention(parsed)
            print(f"\n注意內容:{parsed['text']}")
            print(f"風險等級:{level}")
            if min_days <= 0:
                print("情境:已達處置門檻,次一交易日起很可能公告處置。")
            else:
                print(f"最快情境:只要接下來連續交易日再被列注意 {min_days} 次,即觸發第一次處置。")
            if buffers:
                print("各門檻緩衝(還差幾次):")
                for k, v in buffers.items():
                    tag = "  ← 最近" if v == min_days else ""
                    print(f"    - {k}:還差 {v} 次{tag}")

    # 歷史(從本機 DB 累積)
    print("\n----- 歷史紀錄(本機累積)-----")
    hist = conn.execute(
        "SELECT fetch_date, kind, detail FROM snapshots WHERE code=? ORDER BY fetch_date", (code,)
    ).fetchall()
    if not hist:
        print("(尚無歷史。持續每日執行 update 後,這裡會累積該檔的注意/處置紀錄。)")
    else:
        # 處置事件:用 period 去重顯示
        seen_periods = set()
        dis_events = []
        att_days = set()
        for fdate, kind, detail_s in hist:
            if kind == "disposition":
                try:
                    d = json.loads(detail_s)
                    p = d.get("period", "")
                except Exception:
                    p = detail_s
                if p and p not in seen_periods:
                    seen_periods.add(p)
                    dis_events.append((fdate, d if isinstance(d, dict) else {"period": p}))
            else:
                att_days.add(fdate)
        if dis_events:
            print("處置歷史:")
            for fdate, d in dis_events:
                print(f"  • {d.get('period','')}  原因:{d.get('reason','')[:30]}  (首見於 {fdate})")
        else:
            print("處置歷史:無")
        if att_days:
            days = sorted(att_days)
            print(f"被列注意天數(本機觀察):共 {len(days)} 天,最近:{', '.join(days[-5:])}")


def cmd_rules(args):
    print(RULES_TEXT)


RULES_TEXT = """
============ 台股「注意 → 處置」規則速查 ============

【一、什麼是注意股?】
  交易所每天收盤後,依「注意交易資訊暨處置作業要點 第四條」檢查每檔股票。
  當日價量觸及下列任一款(擇一)就會被『公布注意交易資訊』,常見款別:
    第一款:最近 6 個營業日『累積漲跌幅』過大,且成交量/週轉率同步異常(最主要,也是最快通往處置的一款)
    第二~三款:當日或近期『週轉率』過高
    第四款:本益比為負或過高、股價淨值比異常
    第五款:當日成交量較近期大幅放大
    第六款:券資比 / 融券使用率異常
    第七款:當日沖銷(當沖)比率過高
    第八款:券商買賣超『集中度』過高
    第九款:其他價量與大盤背離等
  ※ 各款的實際數字門檻由交易所定期公布、且會微調;本工具不猜「明天會不會中」,
    而是讀取『已公告的注意紀錄』來數次數、算緩衝。

【二、什麼時候會被處置?(第五條,擇一即處置)】
  A. 連續 3 個營業日   達『第一款』                         ← 最快路徑(3 天)
  B. 連續 5 個營業日,或最近 10 日內有 6 日,或最近 30 日內有 12 日
     達『第一款至第八款』任一

  → 因此「最快幾天被處置」的答案通常是:從乾淨狀態起算，連續 3 個交易日中『第一款』注意。
    如果一檔已經是「連續 2 次」,那再中 1 次(1 個交易日)就會被處置。

【三、被處置後會怎樣?(交易受限)】
  第一次處置(最近 30 日內第一次):
    - 次一營業日起 10 個營業日,改『人工管制撮合』,約每 5 分鐘撮合一次(盤中不連續成交)。
    - 投資人每日委託買賣達一定數量(常見單筆或多筆累計 10 張以上),券商須『預收全額款/券』:
      買進要先付足價金、賣出要先有足額券;信用交易要收足融資自備款或融券保證金。
  第二次(30 日內再次)處置:
    - 期間拉長、撮合間隔放寬到『約每 20 分鐘一次』,預收款券門檻更嚴。

【四、對持有人的實務意義(合規角度)】
  - 被處置 ≠ 下市,但『流動性明顯變差、進出成本變高』:分盤撮合難成交、預收款券卡資金。
  - 若你已持有一檔『被注意』的股票:重點是掌握它離處置還差幾次(本工具會算),
    評估是否要在流動性還好時調整部位,而不是去干預價量 —— 人為影響成交量價以規避監視,
    屬證交法第 155 條操縱行為,不在本工具協助範圍。

  資料來源:TWSE / TPEx 公開 OpenAPI 之注意、處置公告。門檻以主管機關最新公告為準。
=====================================================
"""


def build_parser():
    p = argparse.ArgumentParser(
        description="台股注意/處置監控與風險試算工具",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("update", help="抓取最新注意/處置資料並存檔")
    sub.add_parser("attention", help="列出注意股(依離處置最近排序)")
    sub.add_parser("disposition", help="列出目前處置中的股票")
    sp = sub.add_parser("stock", help="查單一個股狀態+緩衝+歷史")
    sp.add_argument("code", help="股票代號,例如 2330")
    sub.add_parser("rules", help="顯示注意/處置規則說明")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        print("\n範例:python twse_watch.py attention")
        return
    {"update": cmd_update, "attention": cmd_attention,
     "disposition": cmd_disposition, "stock": cmd_stock,
     "rules": cmd_rules}[args.cmd](args)


if __name__ == "__main__":
    main()
