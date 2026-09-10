# 登山團補位行事曆

把 [荒野旅人](https://travelwildtw.com) 和 [台灣三六八](https://www.taiwan368368.com.tw)
上**已經有人報名、但還沒滿**的登山梯次，做成可以訂閱的 ICS 行事曆。
進這些團是補位，不用當開團第一人。

## 訂閱網址

兩站合併（事件標題後面標｜荒野 或｜368）：

```
https://charles0506.github.io/hiking-ics/hiking.ics
```

只要其中一站：

```
https://charles0506.github.io/hiking-ics/hiking-wild.ics
https://charles0506.github.io/hiking-ics/hiking-368.ics
```

Google 行事曆 → 左側「其他日曆」＋ → **以網址訂閱** → 貼上。
手機不用另外設定，訂閱是跟著帳號走的。

## 資料怎麼來的

兩站都是 BVSHOP 架的，但版本不同，取名額的方式也不同：

**荒野旅人**是 Nuxt 3 SSR，商品資料內嵌在頁面的 `__NUXT_DATA__`，直接抓 HTML 就有。
那是 devalue 編碼的扁平陣列，欄位值存的是索引，要從 `prod.specs` 一層層還原；
`prod` 自己也有 `quantity`（全商品加總的假數字），不能掃全陣列找 `size_name`。

**台灣三六八**是舊版 BVSHOP（Laravel + jQuery），HTML 裡沒有名額，要打
`/item/query/<route>`。這個端點直接打回 403，但**先 GET 一次商品頁拿 session cookie，
再帶著 cookie 打就給 200**。回傳 JSON 的 `prod.specs[]` 結構跟荒野旅人一樣。

兩邊都是取每個梯次的 `size_name`（日期）和 `quantity`（剩餘名額）。

滿額基準取「同商品出現次數最多的名額」，不是最大值：
連假梯次容量常加倍（戒茂斯三天平日 7 人、連假 14 人），
用最大值會把所有平日梯次誤判成「已報名 7 人」。

## 篩選規則

留下 `0 < 剩餘名額 < 滿額基準` 的梯次，也就是：

- 剩餘 = 基準 → 還沒人報名，不列（要當開團第一人的自己去官網看）
- 剩餘 = 0 → 額滿或停售，不列
- 「包團」「私訊官方LINE」「敬請期待下梯次」等非固定開團日的選項，不列

## 更新頻率

GitHub Actions 每天台灣時間 06:10 / 12:10 / 18:10 各跑一次，名額有變才 commit。
**但 Google 抓外部 ICS 的頻率自己決定，通常好幾小時到一天**，所以行事曆上的數字
會落後。看到剩 1–2 的直接點進商品頁確認，不要信行事曆上的數字。

## 事件長什麼樣

全天事件，標題 `剩1 雪山主東下翠池 三天三夜`，內文有已報名人數、原始梯次名稱
（含 `D0` 前一晚集合日）和商品連結。`TRANSP:TRANSPARENT` —— 不會把你標成忙碌。

## 手動跑

```bash
python build_ics.py
```

輸出 `docs/hiking.ics` 和 `docs/data.json`。無外部套件依賴，標準庫就夠。
