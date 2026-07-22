#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_data.py — 台股處置雷達 資料引擎

產出:
  docs/data.json     當日快照(注意/處置/自結/全市場目錄/觸發分析)
  docs/history.json  歷史庫(注意逐日、處置事件、自結公告;首次回補一年,之後增量)

資料源(全部公開):
  注意/處置(當日+歷史):TWSE rwd announcement/{notice,punish}、TPEx bulletin/{attention,disposal}
  興櫃注意/處置:TPEx openapi tpex_esb_{warning,disposal}_information
  全市場目錄:TWSE STOCK_DAY_ALL、TPEx mainboard_daily_close_quotes、tpex_esb_latest_statistics
  個股價量:TWSE STOCK_DAY、TPEx tradingStock(近4個月,算60日均量)
  重大訊息(自結):TWSE opendata t187ap04_L、TPEx mopsfin_t187ap04_O

只保留股票(4 碼;含 TDR 91xxxx),排除權證等衍生商品。
"""

import os
import re
import sys
import json
import time
import base64
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twse_watch as tw

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA_PATH = os.path.join(DOCS, "data.json")
HIST_PATH = os.path.join(DOCS, "history.json")

_CN_NUM = r"[〇零一二三四五六七八九十]+"

# 第一款門檻(依交易所「第四條詳細數據」官方原文,2026-07 查證):
#   上市:6日累積漲跌% >32%;或 ≥25% 且 6日起迄價差 ≥50元(均另須與全體/同類均值差幅 ≥20%)
#   上櫃:6日累積漲跌% >30%;或 ≥23% 且 6日起迄價差 ≥40元(同上差幅條件)
# 累積漲跌% = 6營業日每日漲跌%之加總(已與官方公告數字驗證一致)
CLAUSE1 = {"上市": {"hi": 32.0, "lo": 25.0, "diff": 50},
           "上櫃": {"hi": 30.0, "lo": 23.0, "diff": 40}}
# 第十一款(價差):起迄價差門檻 base 元,收盤每 lvl 元加一級距、每級距 +step 元;
# 且當日收盤須為6日內最高(或最低);前5個營業日已依第十一款公布者豁免。
CLAUSE11 = {"上市": {"base": 100, "step": 25, "lvl": 500},
            "上櫃": {"base": 70, "step": 15, "lvl": 300}}
# 第三款(量)/第四款(週轉率)之價格成分門檻與週轉率門檻(官方原文 2026-07 查證):
#   上市:cum6 >25% 且(量≥60日均量×5 或 週轉率≥10%)
#   上櫃:cum6 >27% 且(量≥60日均量×5[量<300張除外] 或 週轉率≥5%)
CLAUSE34 = {"上市": {"cum": 25.0, "to": 10.0}, "上櫃": {"cum": 27.0, "to": 5.0}}
# 第二款(30/60日起迄漲跌幅):30日起迄 >100%(兩市場);60日起迄 上市>130%/上櫃>140%
CLAUSE2 = {"上市": {"d30": 100.0, "d60": 130.0}, "上櫃": {"d30": 100.0, "d60": 140.0}}
VOL_MULT = 5           # 成交量異常門檻:當日量 ≥ 60日均量 × 5(估算值,詳細數據以交易所公告為準)
BACKFILL_YEARS_DAYS = 365
INCREMENTAL_DAYS = 10
SLEEP = 0.22


def is_stock(code):
    return bool(re.fullmatch(r"\d{4}", code) or re.fullmatch(r"91\d{4}", code))


def clean_name(s):
    """櫃買歷史 API 的名稱欄夾帶連結語法,如「風青(../../mainboard/...code=2061)」→ 清掉。"""
    return re.sub(r"\((?:\.\./)+[^)]*\)", "", s or "").strip()


def roc_to_iso(s):
    s = (s or "").strip()
    m = re.match(r"^(\d{3})[./](\d{1,2})[./](\d{1,2})$", s)
    if not m:
        m = re.match(r"^(\d{3})(\d{2})(\d{2})$", s)
    if not m:
        return None
    y, mo, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def parse_period(raw):
    if not raw:
        return None, None
    parts = re.split(r"[～~〜]", raw)
    if len(parts) != 2:
        return None, None
    return roc_to_iso(parts[0]), roc_to_iso(parts[1])


def parse_interval(measure_text, fallback=""):
    m = re.search(r"每(" + _CN_NUM + r"|\d+)分鐘", measure_text or "")
    if m:
        v = tw.cn_to_int(m.group(1))
        if v:
            return v
    fb = fallback or measure_text or ""
    if "第二次" in fb or "再次" in fb:
        return 20
    if "第一次" in fb:
        return 5
    return None


def parse_ndays(text):
    """從處置內容抓「N個營業日」(10/12/5...);抓不到回 None。"""
    m = re.search(r"起\s*(" + _CN_NUM + r"|\d+)\s*個營業日", text or "")
    if not m:
        return None
    return tw.cn_to_int(m.group(1))


def parse_clauses(info):
    out = []
    for m in re.findall(r"第(" + _CN_NUM + r"|\d+)款", info or ""):
        v = tw.cn_to_int(m)
        if v and v not in out:
            out.append(v)
    return out


def tick_down(p):
    """向下貼齊台股檔位。"""
    import math
    for hi, t in ((10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0), (float("inf"), 5.0)):
        if p < hi:
            return round(math.floor(round(p / t, 6) + 1e-9) * t, 2)
    return p


def tick_up(p):
    """向上貼齊台股檔位(掛單有效價):<10:0.01, 10-50:0.05, 50-100:0.1, 100-500:0.5, 500-1000:1, >=1000:5"""
    import math
    for hi, t in ((10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0), (float("inf"), 5.0)):
        if p < hi:
            v = math.ceil(round(p / t, 6) - 1e-9) * t
            return round(v, 2)
    return p


def _f(s):
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return None


def month_chunks(start, end):
    """[(chunk_start, chunk_end)] 以月為單位切割日期區間。"""
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
        yield cur, min(nxt, end)
        cur = nxt + dt.timedelta(days=1)


# ---------------------------------------------------------------------------
# 歷史回補
# ---------------------------------------------------------------------------
def backfill_attention(history, start, end):
    n = 0
    for s, e in month_chunks(start, end):
        try:
            u = (f"https://www.twse.com.tw/rwd/zh/announcement/notice"
                 f"?startDate={s:%Y%m%d}&endDate={e:%Y%m%d}&response=json")
            for row in tw.fetch_json(u).get("data") or []:
                code = str(row[1]).strip()
                iso = roc_to_iso(str(row[5]))
                if not is_stock(code) or not iso:
                    continue
                info = str(row[4]).strip()
                h = history.setdefault(code, {"name": "", "attention": {}, "dispositions": {}})
                h["name"] = clean_name(str(row[2])) or h.get("name", "")
                h["attention"][iso] = {"trig": info, "clauses": parse_clauses(info),
                                       "close": str(row[6]).strip(), "cum": f"累計{row[3]}次"}
                n += 1
        except Exception as ex:
            print(f"[警告] 上市注意回補 {s}~{e} 失敗:{ex}", file=sys.stderr)
        time.sleep(SLEEP)
        try:
            u = (f"https://www.tpex.org.tw/www/zh-tw/bulletin/attention"
                 f"?startDate={s:%Y/%m/%d}&endDate={e:%Y/%m/%d}&response=json")
            rows = (tw.fetch_json(u).get("tables") or [{}])[0].get("data") or []
            for row in rows:
                code = str(row[1]).strip()
                iso = roc_to_iso(str(row[5]))
                if not is_stock(code) or not iso:
                    continue
                info = str(row[4]).strip()
                h = history.setdefault(code, {"name": "", "attention": {}, "dispositions": {}})
                h["name"] = clean_name(str(row[2])) or h.get("name", "")
                h["attention"][iso] = {"trig": info, "clauses": parse_clauses(info),
                                       "close": str(row[6]).strip(), "cum": f"累計{row[3]}次"}
                n += 1
        except Exception as ex:
            print(f"[警告] 上櫃注意回補 {s}~{e} 失敗:{ex}", file=sys.stderr)
        time.sleep(SLEEP)
    return n


def backfill_disposition(history, start, end):
    n = 0
    for s, e in month_chunks(start, end):
        try:  # 上市:[編號,公布日期,代號,名稱,累計,處置條件,處置起迄時間,處置措施,處置內容,備註]
            u = (f"https://www.twse.com.tw/rwd/zh/announcement/punish"
                 f"?startDate={s:%Y%m%d}&endDate={e:%Y%m%d}&response=json")
            for row in tw.fetch_json(u).get("data") or []:
                code = str(row[2]).strip()
                if not is_stock(code):
                    continue
                period_raw = str(row[6]).strip()
                ps, pe = parse_period(period_raw)
                h = history.setdefault(code, {"name": "", "attention": {}, "dispositions": {}})
                h["name"] = clean_name(str(row[3])) or h.get("name", "")
                key = period_raw or roc_to_iso(str(row[1])) or ""
                if not key:
                    continue
                ent = h["dispositions"].get(key)
                if ent is None:
                    h["dispositions"][key] = {
                        "reason": str(row[5]).strip(), "period_start": ps, "period_end": pe,
                        "interval": parse_interval(str(row[8]), str(row[7])),
                        "ndays": parse_ndays(str(row[8])),
                        "market": "上市", "anndate": roc_to_iso(str(row[1])) or ""}
                    n += 1
                else:   # 舊版紀錄補公告日/分盤/天數
                    if not ent.get("anndate"):
                        ent["anndate"] = roc_to_iso(str(row[1])) or ""
                    if ent.get("interval") is None:
                        ent["interval"] = parse_interval(str(row[8]), str(row[7]))
                    if not ent.get("ndays"):
                        ent["ndays"] = parse_ndays(str(row[8]))
        except Exception as ex:
            print(f"[警告] 上市處置回補 {s}~{e} 失敗:{ex}", file=sys.stderr)
        time.sleep(SLEEP)
        try:  # 上櫃:[編號,公布日期,代號,名稱,累計,處置起訖時間,處置原因,處置內容,收盤價,本益比,'']
            u = (f"https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
                 f"?startDate={s:%Y/%m/%d}&endDate={e:%Y/%m/%d}&response=json")
            rows = (tw.fetch_json(u).get("tables") or [{}])[0].get("data") or []
            for row in rows:
                code = str(row[2]).strip()
                if not is_stock(code):
                    continue
                period_raw = str(row[5]).strip()
                ps, pe = parse_period(period_raw)
                h = history.setdefault(code, {"name": "", "attention": {}, "dispositions": {}})
                h["name"] = clean_name(str(row[3])) or h.get("name", "")
                key = period_raw or roc_to_iso(str(row[1])) or ""
                if not key:
                    continue
                ent = h["dispositions"].get(key)
                if ent is None:
                    h["dispositions"][key] = {
                        "reason": str(row[6]).strip(), "period_start": ps, "period_end": pe,
                        "interval": parse_interval(str(row[7])),
                        "ndays": parse_ndays(str(row[7])),
                        "market": "上櫃", "anndate": roc_to_iso(str(row[1])) or ""}
                    n += 1
                else:
                    if not ent.get("anndate"):
                        ent["anndate"] = roc_to_iso(str(row[1])) or ""
                    if ent.get("interval") is None:
                        ent["interval"] = parse_interval(str(row[7]))
                    if not ent.get("ndays"):
                        ent["ndays"] = parse_ndays(str(row[7]))
        except Exception as ex:
            print(f"[警告] 上櫃處置回補 {s}~{e} 失敗:{ex}", file=sys.stderr)
        time.sleep(SLEEP)
    return n


# ---------------------------------------------------------------------------
# 全市場目錄
# ---------------------------------------------------------------------------
def fetch_capital():
    """發行張數 {code: lots}(週轉率計算用)。"""
    lots = {}
    try:
        for r in tw.fetch_json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L"):
            c = str(r.get("公司代號", "")).strip()
            sh = _f(r.get("已發行普通股數或TDR原股發行股數"))
            if is_stock(c) and sh:
                lots[c] = round(sh / 1000)
    except Exception as e:
        print(f"[警告] 上市股本失敗:{e}", file=sys.stderr)
    try:
        for r in tw.fetch_json("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"):
            c = str(r.get("SecuritiesCompanyCode", "")).strip()
            sh = _f(r.get("IssueShares"))
            if is_stock(c) and sh and c not in lots:
                lots[c] = round(sh / 1000)
    except Exception as e:
        print(f"[警告] 上櫃股本失敗:{e}", file=sys.stderr)
    return lots


def fetch_directory():
    stocks = {}
    try:
        for r in tw.fetch_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"):
            c = str(r.get("Code", "")).strip()
            if is_stock(c):
                stocks[c] = [r.get("Name", "").strip(), "上市"]
    except Exception as e:
        print(f"[警告] 上市目錄失敗:{e}", file=sys.stderr)
    try:
        for r in tw.fetch_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"):
            c = str(r.get("SecuritiesCompanyCode", "")).strip()
            if is_stock(c) and c not in stocks:
                stocks[c] = [r.get("CompanyName", "").strip(), "上櫃"]
    except Exception as e:
        print(f"[警告] 上櫃目錄失敗:{e}", file=sys.stderr)
    try:
        for r in tw.fetch_json("https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics"):
            c = str(r.get("SecuritiesCompanyCode", "")).strip()
            if is_stock(c) and c not in stocks:
                stocks[c] = [r.get("CompanyName", "").strip(), "興櫃"]
    except Exception as e:
        print(f"[警告] 興櫃目錄失敗:{e}", file=sys.stderr)
    return stocks


# ---------------------------------------------------------------------------
# 自結公告(重大訊息)
# ---------------------------------------------------------------------------
def _post_json(url, payload):
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"User-Agent": "Mozilla/5.0 Chrome/126",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def backfill_zijie(history, start, end):
    """
    以 MOPS t05st02(歷史重大訊息,逐日)回補自結/達注意標準公告;
    「達注意標準」者再打 t05st02_detail 抓說明欄解析 EPS。
    只留股票、排除可轉債。
    """
    n = nd = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:   # 跳過週末
            try:
                j = _post_json("https://mops.twse.com.tw/mops/api/t05st02",
                               {"year": str(cur.year - 1911),
                                "month": str(cur.month), "day": str(cur.day)})
                for row in ((j.get("result") or {}).get("data") or []):
                    subj = str(row[4]).replace("\r\n", " ").strip()
                    forced = ("注意交易資訊" in subj)                         or ("異常" in subj and ("股價" in subj or "價量" in subj or "成交量" in subj))
                    if not forced and "自結" not in subj:
                        continue
                    if "轉換公司債" in subj or "公司債" in subj:
                        continue
                    code = str(row[2]).strip()
                    if not is_stock(code):
                        continue
                    iso = roc_to_iso(str(row[0]))
                    if not iso:
                        continue
                    h = history.setdefault(code, {"name": clean_name(str(row[3])),
                                                  "attention": {}, "dispositions": {}})
                    h["name"] = h.get("name") or clean_name(str(row[3]))
                    zj = h.setdefault("zijie", {})
                    if iso not in zj:   # 已有(含當日解析過 EPS 的)不覆蓋
                        zj[iso] = {"s": subj, "f": [],
                                   "k": "forced" if forced else "vol"}
                        n += 1
                    # 達注意標準者一律抓內文重新解析(增量僅涵蓋近7日,成本低;全量掃描於版本升級時)
                    if forced and isinstance(row[5], dict):
                        params = row[5].get("parameters")
                        if params:
                            try:
                                dj = _post_json(
                                    "https://mops.twse.com.tw/mops/api/t05st02_detail", params)
                                drows = (dj.get("result") or {}).get("data") or []
                                if drows and len(drows[0]) > 9:
                                    fin = parse_fin(str(drows[0][9]))
                                    if fin:
                                        zj[iso]["f"] = fin
                                        nd += 1
                            except Exception:
                                pass
                            time.sleep(SLEEP)
            except Exception as e:
                print(f"[警告] 自結回補 {cur} 失敗:{e}", file=sys.stderr)
            time.sleep(SLEEP)
        cur += dt.timedelta(days=1)
    print(f"  (其中解析出 EPS 明細 {nd} 筆)")
    return n


def parse_fin(text):
    """
    從重大訊息說明欄解析 EPS → [{"l":標籤,"eps":值,"yoy":同期增減%或None}]。
    支援兩種交易所模板:
      證交所:每股盈餘一行五欄(月EPS 月YoY% 季EPS 季YoY% 近四季EPS)
      櫃買:單月/最近一季/最近四季 三段,各自一行(本期 去年同期 YoY%)
    """
    if not text:
        return []
    mons = re.findall(r"(1\d{2})年(\d{1,2})月", text)
    mon_lab = (max(mons)[1] + "月") if mons else "單月"
    qs = re.findall(r"(1\d{2})年第(\d)季", text)
    q_lab = ("Q" + max(qs)[1]) if qs else "單季"

    out, seen, section = [], set(), ""

    def add(lab, eps, yoy):
        if lab in seen:
            return
        seen.add(lab)
        out.append({"l": lab, "eps": eps, "yoy": yoy})

    for ln in text.replace("　", " ").splitlines():
        low = ln.strip()
        if "單月" in low or "最近一月" in low:
            section = "m"
        if "單季" in low or "最近一季" in low:
            section = "q"
        if "累計" in low or "四季" in low:
            section = "y"
        if "每股盈餘" not in low:
            continue
        low = re.sub(r"[(（](\d[\d,]*\.?\d*)[)）]", r"-\1", low)   # 會計括號負數 (0.06) → -0.06
        toks = re.findall(r"-?[\d,]+\.?\d*%?", low.replace("每股盈餘(元)", "").replace("(元)", ""))
        if not toks:
            continue
        pcts = [t for t in toks if t.endswith("%")]
        cl = lambda t: t.rstrip("%").replace(",", "")
        if len(pcts) >= 2 and len(toks) >= 5:            # 證交所單行式
            add(mon_lab, cl(toks[0]), cl(toks[1]))
            add(q_lab, cl(toks[2]), cl(toks[3]))
            if not toks[4].endswith("%"):
                add("近四季", cl(toks[4]), None)
        elif section == "y":
            add("近四季", cl(toks[0]), None)
        else:                                            # 分段式(含無%及文字YoY變體,如德微)
            lab = mon_lab if section == "m" else q_lab if section == "q" else "近四季"
            yoy = None
            if pcts:
                yoy = cl(pcts[0])
            elif len(toks) >= 3:
                yoy = cl(toks[2])
            elif "由虧轉盈" in low:
                yoy = "由虧轉盈"
            elif "由盈轉虧" in low:
                yoy = "由盈轉虧"
            add(lab, cl(toks[0]), yoy)
        if len(out) >= 4:
            break
    return out


def fetch_zijie(history, today):
    """今日重大訊息:①達注意標準被要求公告之財務資訊 ②一般自結公告。"""
    out = []
    srcs = [("https://openapi.twse.com.tw/v1/opendata/t187ap04_L", "上市",
             "公司代號", "公司名稱", "主旨 ", "發言日期", "說明"),
            ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O", "上櫃",
             "SecuritiesCompanyCode", "CompanyName", "主旨", "發言日期", "說明")]
    for url, market, kc, kn, ks, kd, kx in srcs:
        try:
            for r in tw.fetch_json(url):
                subj = str(r.get(ks, "") or r.get(ks.strip(), "")).strip()
                forced = ("注意交易資訊" in subj) or ("處置" in subj and "標準" in subj)                     or ("異常" in subj and ("股價" in subj or "價量" in subj or "成交量" in subj))
                if not forced and "自結" not in subj:
                    continue
                if "公司債" in subj:   # 可轉債等債券類公告不納入
                    continue
                code = str(r.get(kc, "")).strip()
                if not is_stock(code):
                    continue
                iso = roc_to_iso(str(r.get(kd, ""))) or today
                name = str(r.get(kn, "")).strip()
                desc = str(r.get(kx, "") or "").strip()
                fin = parse_fin(desc)
                h = history.setdefault(code, {"name": name, "attention": {}, "dispositions": {}})
                h["name"] = name or h.get("name", "")
                h.setdefault("zijie", {})[iso] = {"s": subj, "f": fin,
                                                  "k": "forced" if forced else "vol"}
                out.append({"code": code, "name": name, "market": market,
                            "date": iso, "subject": subj,
                            "kind": "forced" if forced else "vol", "fin": fin})
        except Exception as e:
            print(f"[警告] {market}重大訊息失敗:{e}", file=sys.stderr)
    out.sort(key=lambda x: (x["kind"] != "forced", x["date"], x["code"]))
    return out


# ---------------------------------------------------------------------------
# 全市場價格快取(docs/prices.json[.enc]):{"s":{code:[[date,close,vol張],...]}, "_days":[...]}
# ---------------------------------------------------------------------------
PRICES_PATH = os.path.join(DOCS, "prices.json")
PRICE_LIMIT = 10.0     # 上市/上櫃每日漲跌幅限制(%)
PRICE_KEEP = 80        # 每檔保留交易日數(60日均量需 60+)
PRICE_BACKFILL = 130   # 首次回補日曆日數


def backfill_prices(prices, days_back=PRICE_BACKFILL):
    """逐日抓全市場行情(上市 MI_INDEX + 上櫃 dailyQuotes),補進快取。"""
    today = dt.date.today()
    allow_today = dt.datetime.now().hour >= 15   # 收盤後才抓當日(避免盤中價污染)
    # 清掉可能的盤中污染:未達收盤時間時,刪除當日資料
    if not allow_today:
        for code in list(prices["s"].keys()):
            prices["s"][code] = [r for r in prices["s"][code] if r[0] != today.isoformat()]
    # 交易日曆一律由「實際有資料的日子」重建(舊版曾把颱風休市日誤標為已抓)
    data_days = set()
    for rows in prices["s"].values():
        for r in rows:
            data_days.add(r[0])
    have = set(data_days)
    fetched = 0
    for off in range(days_back, -1, -1):
        d0 = today - dt.timedelta(days=off)
        if d0.weekday() >= 5:
            continue
        iso = d0.isoformat()
        if iso in have:
            continue
        if iso >= today.isoformat() and not allow_today:
            continue
        ok_twse = ok_tpex = False
        rows_added = 0
        try:
            j = tw.fetch_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
                              f"?date={d0:%Y%m%d}&type=ALLBUT0999&response=json")
            ok_twse = True
            for t in j.get("tables") or []:
                fl = t.get("fields") or []
                if "證券代號" in fl and "收盤價" in fl:
                    ci, cl, vv = fl.index("證券代號"), fl.index("收盤價"), fl.index("成交股數")
                    for r in t.get("data") or []:
                        code = str(r[ci]).strip()
                        if not is_stock(code):
                            continue
                        c = _f(r[cl])
                        if c:
                            prices["s"].setdefault(code, []).append(
                                [iso, c, round((_f(r[vv]) or 0) / 1000)])
                            rows_added += 1
                    break
        except Exception as e:
            print(f"[警告] 上市行情 {iso} 失敗:{e}", file=sys.stderr)
        time.sleep(SLEEP)
        try:
            j = tw.fetch_json(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
                              f"?date={d0:%Y/%m/%d}&response=json")
            ok_tpex = True
            for t in j.get("tables") or []:
                fl = t.get("fields") or []
                if "代號" in fl and "收盤" in fl:
                    ci, cl, vv = fl.index("代號"), fl.index("收盤"), fl.index("成交股數")
                    for r in t.get("data") or []:
                        code = str(r[ci]).strip()
                        if not is_stock(code):
                            continue
                        c = _f(r[cl])
                        if c:
                            prices["s"].setdefault(code, []).append(
                                [iso, c, round((_f(r[vv]) or 0) / 1000)])
                            rows_added += 1
                    break
        except Exception as e:
            print(f"[警告] 上櫃行情 {iso} 失敗:{e}", file=sys.stderr)
        time.sleep(SLEEP)
        if rows_added > 0:                       # 有實際行情 → 交易日
            have.add(iso)
            data_days.add(iso)
            fetched += 1
    # 去重、排序、修剪
    for code, lst in list(prices["s"].items()):
        m = {}
        for d, c, v in lst:
            m[d] = [c, v]
        rows = sorted([[d, cv[0], cv[1]] for d, cv in m.items()])[-PRICE_KEEP:]
        prices["s"][code] = rows
    prices["_days"] = sorted(data_days)[-(PRICE_KEEP + 30):]
    return fetched


def _counts_1to8(ent):
    """該日注意是否計入處置計數(第一款至第八款)。款別未知時保守視為計入。"""
    if not isinstance(ent, dict):
        return True
    cl = ent.get("clauses")
    if not cl:
        return True
    return any(1 <= c <= 8 for c in cl)


def _has_clause1(ent):
    if not isinstance(ent, dict):
        return True   # 未知 → 保守視為有
    cl = ent.get("clauses")
    if not cl:
        return True
    return 1 in cl


def analyze(code, market, rows, history, lots=None):
    if market not in CLAUSE1:
        return None
    if not rows or len(rows) < 8:
        return None
    dates = [r[0] for r in rows]
    cs = [r[1] for r in rows]
    vs = [r[2] for r in rows]

    pcts = [cs[i] / cs[i - 1] - 1 for i in range(1, len(cs))]
    cum6 = sum(pcts[-6:]) * 100
    diff6 = cs[-1] - cs[-6]
    sum5_next = sum(pcts[-5:]) * 100
    first_next = cs[-5]        # 明日視窗(t-4..t+1)之首日收盤 → 明日起迄價差基準
    certain_hit = False   # 於臨界價算出後判定(見下)

    # 第一款臨界價:兩分支取較低
    c1 = CLAUSE1[market]
    p_hi = cs[-1] * (1 + (c1["hi"] - sum5_next) / 100)
    p_lo = max(cs[-1] * (1 + (c1["lo"] - sum5_next) / 100), first_next + c1["diff"])
    crit = round(min(p_hi, p_lo), 2)
    crit_branch = ("%.0f%%" % c1["hi"]) if p_hi <= p_lo else ("%.0f%%+價差%d元" % (c1["lo"], c1["diff"]))

    # 第一款跌方向:cum6 ≤ -hi,或(cum6 ≤ -lo 且 起迄價差 ≤ -diff)
    pA_dn = cs[-1] * (1 + (-c1["hi"] - sum5_next) / 100)
    pB_dn = min(cs[-1] * (1 + (-c1["lo"] - sum5_next) / 100), first_next - c1["diff"])
    crit_dn = max(pA_dn, pB_dn)
    # 鎖死判定:漲方向臨界 ≤ 跌停價,或 跌方向臨界 ≥ 漲停價 → 無論收在哪都中第一款
    certain_hit = (crit <= cs[-1] * (1 - PRICE_LIMIT / 100) + 1e-9) or                   (crit_dn >= cs[-1] * (1 + PRICE_LIMIT / 100) - 1e-9)

    # 第三款(量)/第四款(週轉率)的價格成分(漲跌雙向)與量門檻
    c34 = CLAUSE34[market]
    crit34 = cs[-1] * (1 + (c34["cum"] - sum5_next) / 100)
    crit34_dn = cs[-1] * (1 + (-c34["cum"] - sum5_next) / 100)
    vol4 = round(lots * c34["to"] / 100) if lots else None   # 週轉率門檻換算張數

    # 第二款:明日視窗之起日(30日窗=cs[-29]、60日窗=cs[-59]),取較低臨界
    c2 = CLAUSE2[market]
    crit2 = None
    if len(cs) >= 29:
        crit2 = cs[-29] * (1 + c2["d30"] / 100)
    if len(cs) >= 59:
        c60 = cs[-59] * (1 + c2["d60"] / 100)
        crit2 = min(crit2, c60) if crit2 else c60

    # 第十一款臨界價(價差款,不計入處置計數):P - first_next ≥ base+級距,且 P 為6日內最高
    c11 = CLAUSE11[market]
    P = first_next + c11["base"]
    for _ in range(12):
        T = c11["base"] + int(P // c11["lvl"]) * c11["step"]
        P2 = first_next + T
        if abs(P2 - P) < 1e-9:
            break
        P = P2
    P = max(P, max(cs[-5:]))
    crit11 = round(P, 2)
    # 第十一款豁免:最近5個營業日內已依第十一款公布注意者
    att = (history.get(code) or {}).get("attention", {})
    exempt11 = any(isinstance(att.get(d), dict) and 11 in (att[d].get("clauses") or [])
                   for d in dates[-5:])
    # 第一款低價豁免:收盤未滿 5 元(上市/上櫃詳細數據除外情形)
    exempt1_lowprice = cs[-1] < 5

    v60 = vs[-61:-1] if len(vs) > 60 else vs[:-1]
    avg60 = sum(v60) / len(v60) if v60 else 0
    crit_vol = round(avg60 * VOL_MULT) if avg60 else None
    if crit_vol and market == "上櫃":
        crit_vol = max(crit_vol, 300)   # 除外:量未達300張不適用第三款

    # 處置期間(含公告日至處置迄日)之注意不納入處置計數(要點第六條)
    disp = (history.get(code) or {}).get("dispositions", {})
    disp_windows = []
    for v in disp.values():
        s0 = v.get("anndate") or v.get("period_start")
        e0 = v.get("period_end")
        if s0 and e0:
            disp_windows.append((s0, e0))

    def in_disp(d):
        return any(s0 <= d <= e0 for s0, e0 in disp_windows)

    # 第二次處置判定:最近30個營業日內曾有處置(迄日落在窗內或仍在處置中)
    win30_start = dates[-30] if len(dates) >= 30 else dates[0]
    second_next = any(e0 >= win30_start for _, e0 in disp_windows)

    # 視窗計數:只計第一款至第八款。
    # (要點第六條「處置期間交易資訊不納入計算」僅適用監視業務督導會報決議之處置,
    #  一般自動處置期間之注意照常計數 — 2026-07-17 經統懋2434案例驗證修正)
    def hit(d):
        return d in att and _counts_1to8(att[d])
    k10 = sum(1 for d in dates[-10:] if hit(d))
    k30 = sum(1 for d in dates[-30:] if hit(d))
    k10_if_hit = sum(1 for d in dates[-9:] if hit(d)) + 1
    k30_if_hit = sum(1 for d in dates[-29:] if hit(d)) + 1
    decay = []
    for f in range(1, 6):
        keep = 10 - f
        win = dates[-keep:] if keep > 0 else []
        decay.append(sum(1 for d in win if hit(d)))
    # 連續「第一款」天數(連三日處置通道;處置期間之注意同樣不計)
    streak1 = 0
    for d in reversed(dates):
        if d in att and _has_clause1(att[d]):
            streak1 += 1
        else:
            break
    streak18 = 0   # 連續「計數款(1~8)」注意天數(連5路徑用)
    for d in reversed(dates):
        if hit(d):
            streak18 += 1
        else:
            break

    # ---- 生存分析 ----
    def _hits_last(nkeep):
        win = dates[-nkeep:] if nkeep > 0 else []
        return sum(1 for d in win if hit(d))

    # 最快處置:假設明日起「天天漲停 +10%」的現實最快劇本
    # (每日推進收盤價,逐日檢查是否觸發第一款,漲停也未必天天中 → 比「假設天天中」誠實)
    fastest = None
    sim_p = list(pcts)
    sim_c = list(cs)
    fut_hits = []
    streak_sim = streak1
    for f in range(1, 21):
        c_new = sim_c[-1] * (1 + PRICE_LIMIT / 100)
        sim_c.append(c_new)
        sim_p.append(PRICE_LIMIT / 100)
        cum6_f = sum(sim_p[-6:]) * 100
        first_f = sim_c[-6]
        hit_f = (cum6_f > c1["hi"]) or (cum6_f >= c1["lo"] and (c_new - first_f) >= c1["diff"])
        if c_new < 5:
            hit_f = False        # 低價豁免
        fut_hits.append(1 if hit_f else 0)
        streak_sim = streak_sim + 1 if hit_f else 0
        w10 = _hits_last(max(0, 10 - f)) + sum(fut_hits[max(0, f - 10):])
        w30 = _hits_last(max(0, 30 - f)) + sum(fut_hits[max(0, f - 30):])
        if streak_sim >= 3 or w10 >= 6 or w30 >= 12:
            fastest = f
            break

    # 安全天數:未來第 f 天「即使單日再中一次」也不會處置,最多能撐幾天
    safe_days = 0
    for f in range(1, 11):
        w10 = _hits_last(10 - f) + 1
        w30 = _hits_last(30 - f) + 1
        if w10 >= 6 or w30 >= 12:
            break
        safe_days = f

    # 假設價格平盤,未來 1~3 日的第一款臨界收盤價
    flat_crits = []
    C = cs[-1]
    for f in range(1, 4):
        known = 6 - f            # 窗內已知的每日漲跌% 個數
        sumk = sum(pcts[-known:]) * 100 if known > 0 else 0.0
        first_f = cs[-(6 - f)] if (6 - f) >= 1 and (6 - f) <= len(cs) else C
        p_hi_f = C * (1 + (c1["hi"] - sumk) / 100)
        p_lo_f = max(C * (1 + (c1["lo"] - sumk) / 100), first_f + c1["diff"])
        flat_crits.append(round(min(p_hi_f, p_lo_f), 2))

    # 逐日表(近 15 個交易日):日期/收盤/漲跌%/6日累積%/中注意款別/是否處置期間
    day_rows = []
    for i in range(max(1, len(cs) - 15), len(cs)):
        d = dates[i]
        pct_i = (cs[i] / cs[i - 1] - 1) * 100
        cum_i = sum(pcts[max(0, i - 6):i]) * 100
        e = att.get(d)
        cl = (e.get("clauses") if isinstance(e, dict) else None) or []
        day_rows.append([d, cs[i], round(pct_i, 2), round(cum_i, 2),
                         cl, 1 if in_disp(d) else 0])

    return {
        "cum6": round(cum6, 2), "diff6": round(diff6, 2),
        "th_hi": c1["hi"], "th_lo": c1["lo"], "th_diff": c1["diff"],
        "sum5_next": round(sum5_next, 2), "crit": crit, "crit_branch": crit_branch,
        "crit11": crit11, "exempt11": exempt11,
        "exempt1_lowprice": exempt1_lowprice, "second_next": second_next,
        "avg60": round(avg60), "crit_vol": crit_vol, "vol_today": round(vs[-1]),
        "k10": k10, "k30": k30, "k10_if_hit": k10_if_hit, "k30_if_hit": k30_if_hit,
        "decay": decay,
        "streak1": streak1, "streak18": streak18,
        "fastest": fastest, "safe_days": safe_days, "flat_crits": flat_crits,
        "day_rows": day_rows,
        "close": cs[-1], "limit": PRICE_LIMIT,
        "crit_pct": round((crit / cs[-1] - 1) * 100, 2),
        "crit11_pct": round((crit11 / cs[-1] - 1) * 100, 2),
        "crit_tick": tick_up(crit), "crit11_tick": tick_up(crit11),
        "certain_hit": certain_hit,
        "crit34_tick": tick_up(crit34),
        "crit34_pct": round((crit34 / cs[-1] - 1) * 100, 2),
        "crit_dn_tick": tick_down(crit_dn),
        "crit_dn_pct": round((crit_dn / cs[-1] - 1) * 100, 2),
        "crit34_dn_tick": tick_down(crit34_dn),
        "crit34_dn_pct": round((crit34_dn / cs[-1] - 1) * 100, 2),
        "th34": c34["cum"], "to_th": c34["to"], "vol4": vol4,
        "crit2_tick": tick_up(crit2) if crit2 else None,
        "crit2_pct": round((crit2 / cs[-1] - 1) * 100, 2) if crit2 else None,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build():
    data = tw.fetch_all()
    recs = [r for r in tw.collect_records(data) if is_stock(r["code"])]
    if not recs:
        print("[錯誤] 各資料源皆無資料,放棄本次更新。", file=sys.stderr)
        sys.exit(1)

    today_d = dt.date.today()
    today = today_d.isoformat()

    # ---- 歷史載入(明文或 .enc;_meta 記錄回補範圍) ----
    history = {}
    if os.path.exists(HIST_PATH):
        try:
            with open(HIST_PATH, encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            history = {}
    elif os.path.exists(HIST_PATH + ".enc"):
        pw = get_password()
        if pw:
            try:
                with open(HIST_PATH + ".enc", encoding="ascii") as f:
                    history = json.loads(decrypt(f.read(), pw))
            except Exception as e:
                print(f"[警告] 歷史解密失敗(密碼變更?),重新累積:{e}", file=sys.stderr)
    meta = history.pop("_meta", {})
    history = {c: h for c, h in history.items() if is_stock(c)}
    for _h in history.values():
        _h["name"] = clean_name(_h.get("name", ""))

    bf_from = meta.get("backfill_from")
    year_ago = today_d - dt.timedelta(days=BACKFILL_YEARS_DAYS)
    if not bf_from or bf_from > year_ago.isoformat() or meta.get("disp_v") != 3:
        start = year_ago
        print(f"首次全量回補一年({start} ~ {today})…")
    else:
        start = today_d - dt.timedelta(days=INCREMENTAL_DAYS)
        print(f"增量回補({start} ~ {today})")
    na = backfill_attention(history, start, today_d)
    nd = backfill_disposition(history, start, today_d)
    meta["backfill_from"] = min(bf_from or today, year_ago.isoformat())
    meta["disp_v"] = 3
    print(f"回補:注意 {na} 筆、處置 {nd} 筆")

    # 自結/達注意標準公告回補(MOPS 逐日;首次一年,之後增量;v2 起含 EPS 內文)
    zj_from = meta.get("zj_from")
    if not zj_from or zj_from > year_ago.isoformat() or meta.get("zj_v") != 5:
        zj_start = year_ago
        print(f"全量回補自結公告一年含內文({zj_start} ~ {today})…約需十餘分鐘")
    else:
        zj_start = today_d - dt.timedelta(days=7)
    nz = backfill_zijie(history, zj_start, today_d)
    meta["zj_from"] = min(zj_from or today, year_ago.isoformat())
    meta["zj_v"] = 5
    print(f"自結回補 {nz} 筆")

    # ---- 全市場目錄 ----
    stocks = fetch_directory()
    print(f"全市場目錄 {len(stocks)} 檔")
    lots_map = fetch_capital()
    print(f"股本資料 {len(lots_map)} 檔")

    # ---- 全市場價格快取 ----
    prices = None
    if os.path.exists(PRICES_PATH):
        try:
            with open(PRICES_PATH, encoding="utf-8") as f:
                prices = json.load(f)
        except (json.JSONDecodeError, OSError):
            prices = None
    elif os.path.exists(PRICES_PATH + ".enc"):
        pw0 = get_password()
        if pw0:
            try:
                with open(PRICES_PATH + ".enc", encoding="ascii") as f:
                    prices = json.loads(decrypt(f.read(), pw0))
            except Exception as e:
                print(f"[警告] 價格快取解密失敗,重建:{e}", file=sys.stderr)
    if not prices or "s" not in prices:
        prices = {"s": {}, "_days": []}
        print(f"首次建立全市場價格快取(約 {PRICE_BACKFILL} 日曆日,需數分鐘)…")
    np_ = backfill_prices(prices)
    print(f"價格快取:新增 {np_} 個交易日(共 {len(prices['_days'])} 日 / {len(prices['s'])} 檔)")
    for c, h in history.items():   # 目錄補歷史裡的名稱
        if c in stocks and not h.get("name"):
            h["name"] = stocks[c][0]

    # ---- 自結公告 ----
    zijie_today = fetch_zijie(history, today)
    print(f"今日自結公告 {len(zijie_today)} 筆")

    # ---- 當日注意公告(興櫃靠這個;上市/上櫃回補已涵蓋) ----
    daily = {}
    for r in recs:
        if r["kind"] != "attention_daily":
            continue
        d = r["detail"]
        if not d.get("info"):
            continue
        iso = roc_to_iso(d.get("date", "")) or today
        daily[r["code"]] = {
            "code": r["code"], "name": r["name"], "market": r["market"],
            "info": d["info"], "clauses": parse_clauses(d["info"]),
            "close": d.get("close", ""), "date": iso,
        }
        h = history.setdefault(r["code"], {"name": r["name"], "attention": {}, "dispositions": {}})
        h["name"] = r["name"] or h.get("name", "")
        ent = h["attention"].get(iso) or {}
        ent.update({"trig": d["info"], "clauses": parse_clauses(d["info"]),
                    "close": d.get("close", "")})
        h["attention"][iso] = ent

    # ---- 注意股(累計清單 ∪ 當日公告)+ 風險試算 ----
    attention = {}
    for r in recs:
        if r["kind"] != "attention":
            continue
        parsed = tw.parse_attention_text(r["detail"])
        buffers, min_days, level = tw.risk_from_attention(parsed)
        attention[r["code"]] = {
            "code": r["code"], "name": r["name"], "market": r["market"],
            "text": parsed["text"], "min_days": min_days, "level": level,
            "buffers": buffers, "trig": None, "analysis": None,
        }
    for code, dy in daily.items():
        countable = (not dy["clauses"]) or any(1 <= c <= 8 for c in dy["clauses"])
        if code in attention:
            attention[code]["trig"] = dy
        elif not countable:
            # 純第九款以後(如第十一款價差):不計入處置計數的「無害型」注意
            attention[code] = {
                "code": code, "name": dy["name"], "market": dy["market"],
                "text": "當日公布注意(第9款以後,不計入處置計數)",
                "min_days": None, "level": "🟢 不計入處置計數(第9~13款)",
                "buffers": {}, "trig": dy, "analysis": None,
            }
        else:
            parsed = {"consecutive": 1, "window_days": None, "window_count": None,
                      "text": "當日公布注意(近期第 1 次)"}
            buffers, min_days, level = tw.risk_from_attention(parsed)
            attention[code] = {
                "code": code, "name": dy["name"], "market": dy["market"],
                "text": parsed["text"], "min_days": min_days, "level": level,
                "buffers": buffers, "trig": dy, "analysis": None,
            }

    # ---- 全市場觸發/生存分析(上市/上櫃全部股票) ----
    analysis_map = {}
    for code, (nm, mkt) in stocks.items():
        if mkt not in CLAUSE1:
            continue
        an = analyze(code, mkt, prices["s"].get(code), history, lots_map.get(code))
        if an:
            analysis_map[code] = an
    print(f"觸發分析 {len(analysis_map)} 檔")

    # 處置候選:全市場獨立掃描 —— 明日再中 1 次計數款注意即處置者
    # (交易所累計名單會漏「連續中斷但10日窗仍滿5次」的股票,如 2026-07-21 邁科/榮科案例)
    danger = []
    for code, an in analysis_map.items():
        will = (an["streak1"] >= 2) or (an.get("streak18", 0) >= 4) or                an["k10_if_hit"] >= 6 or an.get("k30_if_hit", 0) >= 12
        if not will:
            continue
        nm, mkt = (stocks.get(code) or [history.get(code, {}).get("name", ""), ""])
        danger.append({"code": code, "name": nm or history.get(code, {}).get("name", ""),
                       "market": mkt, "second": bool(an.get("second_next")),
                       "certain": bool(an.get("certain_hit"))})
    danger.sort(key=lambda x: (not x["certain"], x["code"]))
    print(f"處置候選(全市場掃描){len(danger)} 檔")
    for code, a in attention.items():
        a["analysis"] = analysis_map.get(code) or analyze(code, a["market"], prices["s"].get(code), history, lots_map.get(code))
        an = a["analysis"]
        # 連三日通道只認第一款:若「連續N次」中最新一天沒有第一款,連3路徑不成立
        if an is not None and a["min_days"] is not None and "連續3次(第一款,最快)" in a["buffers"]:
            if an["streak1"] == 0:
                a["buffers"].pop("連續3次(第一款,最快)", None)
                if a["buffers"]:
                    a["min_days"] = min(a["buffers"].values())
                _, _, a["level"] = None, None, a["level"]
                if a["min_days"] >= 2:
                    a["level"] = ("🟠 高(再中 %d 次注意即處置)" % a["min_days"]) if a["min_days"] == 2 \
                        else ("🟡 中(約再 %d 次注意即處置)" % a["min_days"]) if a["min_days"] == 3 \
                        else ("🟢 觀察中")
    # 自結公告機率因子:連續注意天數(以交易日曆回數)與近30日內是否已公告過
    _cal = prices.get("_days") or []
    _cidx = {d: i for i, d in enumerate(_cal)}
    _lastd = _cal[-1] if _cal else today
    for code, a in attention.items():
        att_days = (history.get(code) or {}).get("attention", {})
        zj_days = list(((history.get(code) or {}).get("zijie") or {}).keys())
        streak = 0
        if _lastd in att_days and _lastd in _cidx:
            streak = 1
            i = _cidx[_lastd]
            while i - streak >= 0 and _cal[i - streak] in att_days:
                streak += 1
        d0 = dt.date.fromisoformat(_lastd)
        recent = any(0 < (d0 - dt.date.fromisoformat(z)).days <= 30 for z in zj_days)
        a["zjs"] = streak
        a["zjr"] = recent

    attention = sorted(attention.values(),
                       key=lambda x: (x["min_days"] if x["min_days"] is not None else 99, x["code"]))

    # ---- 處置股(當日快照) ----
    dispositions = []
    for r in recs:
        if r["kind"] != "disposition":
            continue
        d = r["detail"]
        ps, pe = parse_period(d.get("period", ""))
        dispositions.append({
            "code": r["code"], "name": r["name"], "market": r["market"],
            "reason": d.get("reason", ""), "period_raw": d.get("period", ""),
            "period_start": ps, "period_end": pe,
            "measure": d.get("measure", ""),
            "interval": parse_interval(d.get("measure", "")),
            "anndate": roc_to_iso(d.get("anndate", "")) or "",
        })
        h = history.setdefault(r["code"], {"name": r["name"], "attention": {}, "dispositions": {}})
        h["name"] = r["name"] or h.get("name", "")
        key = d.get("period", "") or roc_to_iso(d.get("anndate", "")) or ""
        if key and key not in h["dispositions"]:
            h["dispositions"][key] = {
                "reason": d.get("reason", ""), "period_start": ps, "period_end": pe,
                "interval": parse_interval(d.get("measure", "")),
                "ndays": parse_ndays(d.get("measure", "")),
                "market": r["market"], "anndate": roc_to_iso(d.get("anndate", "")) or ""}
    # 真實處置迄日與出關日:公告期間遇休市(颱風等)順延 → 以實際交易日曆重算
    cal = [d for d in (prices.get("_days") or [])]
    def real_end_exit(ps, n):
        if not ps or not n:
            return None, None
        idx = [d for d in cal if d >= ps]
        if len(idx) >= n:
            rend = idx[n - 1]
        else:
            cnt = len(idx)
            if idx:
                cur = dt.date.fromisoformat(idx[-1])
            elif cal:
                cur = max(dt.date.fromisoformat(cal[-1]),
                          dt.date.fromisoformat(ps) - dt.timedelta(days=1))
            else:
                cur = dt.date.fromisoformat(ps) - dt.timedelta(days=1)
            while cnt < n:
                cur += dt.timedelta(days=1)
                if cur.weekday() < 5:
                    cnt += 1
            rend = cur.isoformat()
        e = dt.date.fromisoformat(rend)
        while True:
            e += dt.timedelta(days=1)
            if e.weekday() < 5:
                break
        return rend, e.isoformat()

    def weekday_count(ps, pe):
        try:
            d0, d1 = dt.date.fromisoformat(ps), dt.date.fromisoformat(pe)
        except (ValueError, TypeError):
            return None
        n = 0
        while d0 <= d1:
            if d0.weekday() < 5:
                n += 1
            d0 += dt.timedelta(days=1)
        return n or None

    for x in dispositions:
        n = parse_ndays(x.get("measure", "")) or weekday_count(x.get("period_start"), x.get("period_end")) or 10
        rend, exd = real_end_exit(x.get("period_start"), n)
        x["ndays"] = n
        x["period_end_real"] = rend or x.get("period_end")
        x["exit_date"] = exd

    # 出關清單(含已從當日快照輪換掉、但仍處置中的股票)
    exits = []
    for c, hh in history.items():
        if c == "_meta":
            continue
        for k, v in (hh.get("dispositions") or {}).items():
            ps = v.get("period_start")
            if not ps:
                continue
            n = v.get("ndays") or weekday_count(ps, v.get("period_end")) or 10
            rend, exd = real_end_exit(ps, n)
            if exd and exd >= today:
                exits.append({"code": c, "name": hh.get("name", ""),
                              "market": v.get("market", ""), "interval": v.get("interval"),
                              "reason": v.get("reason", ""), "period_start": ps,
                              "period_end": v.get("period_end"),
                              "period_end_real": rend, "exit_date": exd})
    # 同一檔取最近一次處置
    dedup = {}
    for e in sorted(exits, key=lambda x: x["period_start"]):
        dedup[e["code"]] = e
    exits = sorted(dedup.values(), key=lambda x: (x["exit_date"], x["code"]))

    dispositions.sort(key=lambda x: (x.get("anndate") or "", x["code"]), reverse=True)

    # (處置中股票已含在全市場分析內)

    # ---- 輸出(有密碼 → AES-GCM 加密 .enc;無密碼 → 明文,僅供本機測試) ----
    os.makedirs(DOCS, exist_ok=True)
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    # 下一交易日:從「最後有行情資料的交易日」起算(盤前執行=今天,收盤後執行=次一交易日)
    ref = prices["s"].get("2330") or next(iter(prices["s"].values()), None)
    last_td = ref[-1][0] if ref else today
    nds, d0 = [], dt.date.fromisoformat(last_td)
    while len(nds) < 3:
        d0 += dt.timedelta(days=1)
        if d0.weekday() < 5:
            nds.append(d0.isoformat())
    # 自結顯示清單:自最後交易日(含)以後公布者 → 週一早上看得到週五盤後那批
    zijie_recent = []
    for _c, _hh in history.items():
        if _c == "_meta":
            continue
        for _iso, _e in (_hh.get("zijie") or {}).items():
            if _iso >= last_td and isinstance(_e, dict):
                zijie_recent.append({"code": _c, "name": _hh.get("name", ""),
                                     "market": (stocks.get(_c) or ["", ""])[1],
                                     "date": _iso, "subject": _e.get("s", ""),
                                     "kind": _e.get("k", "vol"), "fin": _e.get("f") or []})
    zijie_recent.sort(key=lambda x: (x["date"], x["code"]), reverse=True)
    zijie_recent.sort(key=lambda x: x["kind"] != "forced")

    out = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "next_days": nds,
        "attention": attention,
        "dispositions": dispositions,
        "stocks": stocks,
        "zijie": zijie_recent,
        "analysis": analysis_map,
        "exits": exits,
        "danger": danger,
    }
    history["_meta"] = meta
    pw = get_password()
    if pw:
        for path, obj in ((DATA_PATH, out), (HIST_PATH, history), (PRICES_PATH, prices)):
            blob = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            with open(path + ".enc", "w", encoding="ascii") as f:
                f.write(encrypt(blob, pw))
            if os.path.exists(path):
                os.remove(path)   # 不留明文
        mode = "已加密(.enc)"
    else:
        with open(DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        with open(HIST_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=1)
        with open(PRICES_PATH, "w", encoding="utf-8") as f:
            json.dump(prices, f, ensure_ascii=False)
        mode = "明文(未設密碼)"
    print(f"OK 注意 {len(attention)} / 處置 {len(dispositions)} / 目錄 {len(stocks)} / "
          f"自結 {len(zijie_recent)} → docs/ {mode}(歷史 {len(history)-1} 檔)")


def get_password():
    """密碼來源:環境變數 SITE_PASSWORD(GitHub Actions secret)或 site_password.txt。"""
    pw = os.environ.get("SITE_PASSWORD", "").strip()
    if pw:
        return pw
    p = os.path.join(ROOT, "site_password.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return None


def encrypt(plaintext, password):
    """PBKDF2(SHA-256, 250k) + AES-256-GCM → base64(salt16+iv12+ct)。"""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=250000).derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, plaintext, None)
    return base64.b64encode(salt + iv + ct).decode("ascii")


def decrypt(b64, password):
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(b64)
    salt, iv, ct = raw[:16], raw[16:28], raw[28:]
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=250000).derive(password.encode("utf-8"))
    return AESGCM(key).decrypt(iv, ct, None).decode("utf-8")


if __name__ == "__main__":
    build()
