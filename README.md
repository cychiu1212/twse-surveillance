# 台股處置雷達

台股上市/上櫃「注意股・處置股」查詢網站:處置月曆、出關倒數、分盤限制、
注意累計次數與處置風險試算、個股處置歷史。

資料來源:TWSE / TPEx 公開 OpenAPI(注意交易資訊、處置有價證券公告)。

## 架構

- `docs/index.html` — 靜態網站(GitHub Pages 直接服務)
- `docs/data.json` / `docs/history.json` — 由 `fetch_data.py` 產生的資料與累積歷史
- `fetch_data.py` — 抓公告 → 解析(分盤間隔、處置期間、注意累計次數)→ 風險試算 → 輸出 JSON
- `twse_watch.py` — 核心解析邏輯 + 本機 CLI(`attention` / `disposition` / `stock 代號` / `rules`)
- `twse_web.py` — 本機動態網頁版(選用)
- `.github/workflows/update.yml` — 每交易日台北時間 17:35 / 18:35 自動抓資料並 commit

## 密碼保護

網站資料以 AES-256-GCM 加密(PBKDF2 250k 迭代),瀏覽器端輸入密碼解密,
沒有密碼看不到任何資料(GitHub Pages 為靜態站,單純 JS 擋板無效,故採真加密)。

- 本機:密碼放 `site_password.txt`(已 gitignore,絕不 commit)。
- GitHub Actions:repo **Settings → Secrets and variables → Actions** 新增
  secret `SITE_PASSWORD`,值與本機一致。
- 換密碼:改 `site_password.txt` 與 secret 後重跑 `fetch_data.py`(歷史檔會用新密碼重加密)。

## 部署(GitHub Pages)

1. 推上 GitHub 之後,先到 Settings → Secrets 設好 `SITE_PASSWORD`。
2. **Settings → Pages**,Source 選 `main` branch 的 `/docs` 資料夾。
3. **Actions** 頁籤確認 workflow 已啟用(必要時按一次 Run workflow 手動跑第一次)。
4. 網站網址:`https://<帳號>.github.io/<repo名>/`,把網址+密碼給朋友即可。

## 本機預覽

```
python fetch_data.py
python -m http.server 8766 --directory docs
# 開 http://localhost:8766
```

## 免責

本站為公開監視資訊之整理與風險提示,非投資建議;實際注意/處置門檻以
臺灣證券交易所與證券櫃檯買賣中心最新公告為準。
