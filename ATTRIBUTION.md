# 授權與來源

`indicators.py` 改寫自 © Zeiierman 的 **Buyers & Sellers Profile + Dynamic S/R**（2026-10-17 讀取公開 440 行版本）。原始碼授權：Creative Commons Attribution–NonCommercial–ShareAlike 4.0 International。

- 原作：https://www.tradingview.com/script/bEcZR3nB-Buyers-Sellers-Profile-Dynamic-S-R-Zeiierman/
- 授權：https://creativecommons.org/licenses/by-nc-sa/4.0/

改寫內容：從 Pine Script 移植至 Python／NumPy；移除繪圖物件；新增逐日歷史狀態、資料調整與暖機檢查；交易、配置、庫存、資料與回測模組為本次新增。Python 改作以相同 CC BY-NC-SA 4.0 授權提供。未宣稱經原作者認可或逐數值認證。若要商業散布或商業服務用途，需另行處理原作授權。

公開資料與快取不因程式授權而取得新的轉售／再散布權；本地快取供使用者個人研究與重現結果。

## 資料來源

- FinMind：日 OHLCV、除權息結果、股利政策、產業資訊。https://finmind.github.io/quickstart/
- Yahoo Finance（透過 yfinance）：拆股及部分現金股利交叉參考。https://finance.yahoo.com/quote/0050.TW/history/
- 0050 2026-10-16 成分股快照：元大官網當時維護，採 JoJoRadar 的元大持股轉載，完整50檔。https://www.jojoradar.com/0050
- 已知 2026-09-21 換股：新增6446、移除3661，依新聞引述公告重建。https://money.udn.com/money/story/5607/9734823
- 0050 2025-06-18 一拆四：補正 Yahoo 公司行動漏值。https://wwwc.twse.com.tw/zh/ETFortune/announcement?company=A00005&date=20250509&fund=0050&seq=1&type=other
- 國泰證券公開網路下單費率：2.8折；個人帳戶最低費用未核實。https://www.cathaysec.com.tw/cathaysec/Products/TradeFee/TWS.aspx
- 國泰盤中零股說明：https://istockapp.cathaysec.com.tw/Marketing/oddlot/

9月換股已納入，但沒有取得完整官方逐日歷史持股檔，不能聲稱已排除所有成分股偏誤。
