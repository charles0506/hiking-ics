#!/usr/bin/env python3
"""
登山團補位梯次 → ICS 訂閱行事曆

抓兩個 BVSHOP 站台的每個梯次剩餘名額，只留「已經有人報名、但還沒滿」的，
輸出 docs/hiking.ics（合併）與各站單獨的 ics。

兩站架構不同，取名額的方式也不同：

  荒野旅人 travelwildtw.com    Nuxt 3 SSR，商品資料內嵌在頁面的 __NUXT_DATA__，
                              直接抓 HTML 就有，不需要 session。

  台灣三六八 taiwan368368.com.tw  舊版 BVSHOP（Laravel + jQuery），HTML 裡沒有名額，
                              要打 /item/query/<route>。這個端點直接打回 403，
                              但先 GET 一次商品頁拿到 session cookie 再帶著打就給 200。

兩邊的 spec 結構一樣：size_name 是梯次日期、quantity 是剩餘名額。
"""

import datetime as dt
import http.cookiejar
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
OUT = Path(__file__).parent / "docs"
TZ = dt.timezone(dt.timedelta(hours=8))

SITES = [
    {"key": "wild", "name": "荒野", "base": "https://travelwildtw.com", "mode": "nuxt",
     # 只要台灣的團。海外團全掛在這個分類底下（滑雪、埃及、吉力馬扎羅、日本各線…），
     # 用分類排除比用關鍵字可靠 —— 新開的海外團會自動跟著被排掉。
     "exclude_categories": ["overseas-hiking-tours"]},
    {"key": "368", "name": "368", "base": "https://www.taiwan368368.com.tw", "mode": "query"},
    # 368 沒有海外分類，全部都是台灣線，不用排
]

# 這些選項不是固定開團日，沒有成團人數概念
SKIP_WORDS = ("包團", "私訊", "敬請期待", "規劃中", "額滿", "洽詢", "另享優惠",
              "Please register", "外籍", "檔期", "客製")

# 商品標題含這些字就整個商品跳過 —— 這份行事曆只要登山團，不要課程。
#   課        368 的裝備選購×輕量化打包課、離線地圖戶外實戰課
#   行進技巧  368 的登山行進技巧初階班、Day2 實戰班
# 合歡四峰高山入門班是刻意留著的：名字有「班」但實際是兩天一夜上合歡群峰。
# 海外團不靠標題排除，走分類（見 SITES 的 exclude_categories）。
SKIP_TITLE = ("課", "行進技巧")


def make_opener():
    """每個站一個 opener，帶自己的 cookie jar —— 368 的名額 API 要 session 才給。"""
    return build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))


def fetch(opener, url, referer=None, tries=3):
    headers = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9"}
    if referer:
        headers.update({"Referer": referer, "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/plain, */*"})
    for i in range(tries):
        try:
            with opener.open(Request(url, headers=headers), timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except (URLError, HTTPError) as e:
            if i == tries - 1:
                print(f"  ! 抓不到 {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))
    return None


def item_routes(opener, base):
    """從 sitemap 取商品 route。regex 要含連字號，
    否則 travelwildtwcomturkey-ski 這種會被截斷成 404。"""
    xml = fetch(opener, f"{base}/sitemap.xml")
    if not xml:
        print(f"  ! {base} sitemap 抓不到", file=sys.stderr)
        return []
    return sorted(set(re.findall(r"/item/([A-Za-z0-9\-_]+)", xml)))


def category_routes(opener, base, cat):
    """某個分類底下的所有商品 route。分類頁是 server-side 分頁，每頁 12 筆，
    ?page=N 直接吐該頁的 HTML（頁碼按鈕本身是 JS 的，但參數版是 SSR）。"""
    routes, page = set(), 1
    while page <= 12:
        url = f"{base}/category/{cat}" + ("" if page == 1 else f"?page={page}")
        html = fetch(opener, url)
        if not html:
            break
        found = set(re.findall(r"/item/([A-Za-z0-9\-_]+)", html))
        if not found - routes:          # 這頁沒有新東西，翻完了
            break
        routes |= found
        page += 1
        time.sleep(0.3)
    return routes


def parse_nuxt(html):
    """從 __NUXT_DATA__（devalue 扁平陣列）還原商品標題與各梯次名額。

    devalue 把欄位值存成索引：prod['specs'] 拿到的是索引，arr[索引] 才是 list，
    list 裡每個元素又是索引。注意 prod 本身也有 quantity/size_name（全商品加總的
    假數字），所以不能掃全陣列找 size_name，要從 prod.specs 走。
    """
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

    prods = [v for v in arr
             if isinstance(v, dict) and "specs" in v and "title" in v and "route" in v]
    if not prods:
        return None
    prod = prods[0]
    spec_idx = arr[prod["specs"]]
    if not isinstance(spec_idx, list):
        return None

    specs = []
    for si in spec_idx:
        sp = arr[si]
        if not isinstance(sp, dict) or "size_name" not in sp or "quantity" not in sp:
            continue
        name, qty = arr[sp["size_name"]], arr[sp["quantity"]]
        if isinstance(name, str) and isinstance(qty, int):
            specs.append((name, qty))
    title = arr[prod["title"]]
    return {"title": title if isinstance(title, str) else "", "specs": specs}


def parse_query(raw):
    """368 的 /item/query/<route> 回傳的 JSON。"""
    try:
        prod = json.loads(raw)["response"]["prod"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    specs = [(s["size_name"], s["quantity"]) for s in prod.get("specs") or []
             if isinstance(s.get("size_name"), str) and isinstance(s.get("quantity"), int)]
    return {"title": prod.get("title") or "", "specs": specs}


def load_product(opener, site, route):
    url = f"{site['base']}/item/{route}"
    if site["mode"] == "nuxt":
        html = fetch(opener, url)
        return (parse_nuxt(html) if html else None), url
    # query 模式：先開商品頁拿 session cookie，再打名額 API
    r = route.lower()
    url = f"{site['base']}/item/{r}"
    if not fetch(opener, url):
        return None, url
    raw = fetch(opener, f"{site['base']}/item/query/{r}", referer=url)
    return (parse_query(raw) if raw else None), url


DATE_HEAD = re.compile(r"(?:(\d{4})/)?(\d{1,2})/(\d{1,2})")
# 結束日必須緊接在出發日後面（中間只容許星期括號和空白），不能往後隨便找。
# 否則 368 課程「9/16平日 / 晚上場19-22」的上課時間 19-22 會被當成「到 22 號」。
DATE_TAIL = re.compile(r"\s*(?:\([^)]*\))?\s*-\s*(?:(\d{4})/)?(?:(\d{1,2})/)?(\d{1,2})")


def parse_dates(size_name, today):
    """把梯次名稱解析成 (出發日, 結束日)。解不出來回 None —— 寧可漏，也不要寫錯日期。

    看過的格式：
      11／13(五)-11／15(日) D0.11／12    前一晚集合
      10／19(一)-21(三)                  結束日省略月份
      12／31(四)-1／3(日)                跨年
      2026／12／16(三)-20(日)            明確年份
    """
    s = size_name.replace("／", "/").replace("（", "(").replace("）", ")")
    # D0 是前一晚集合日，不是梯次本身的日期，要整段拿掉。
    # 荒野寫在後面「11/13(五)-11/15(日) D0.11/12」，368 有時寫在前面
    # 「D0.10/2(五)_10/3(六)-10/4(日)」—— 用 split("D0") 會把 368 那種切成空字串。
    s = re.sub(r"D0\.?\s*\d{1,2}/\d{1,2}\s*(?:\([^)]*\))?_?", " ", s)
    s = s.split("*")[0]

    head = DATE_HEAD.search(s)
    if not head:
        return None
    y, mo, d = head.group(1), int(head.group(2)), int(head.group(3))
    year = int(y) if y else today.year
    try:
        start = dt.date(year, mo, d)
    except ValueError:
        return None
    # 沒寫年份時，若推出來的日期已經過去很久，代表講的是明年
    if not y and (start - today).days < -60:
        year += 1
        try:
            start = dt.date(year, mo, d)
        except ValueError:
            return None

    end = start
    tail = DATE_TAIL.match(s[head.end():])
    if tail:
        ey = int(tail.group(1)) if tail.group(1) else start.year
        emo = int(tail.group(2)) if tail.group(2) else start.month
        ed = int(tail.group(3))
        try:
            end = dt.date(ey, emo, ed)
        except ValueError:
            end = start
        if end < start:                       # 跨年梯次
            try:
                end = dt.date(ey + 1, emo, ed)
            except ValueError:
                end = start
        if (end - start).days > 30:           # 解過頭，當單日處理
            end = start
    return start, end


def short_title(t):
    """《雲海,絕壁,賞楓之旅》 鳶嘴捎來 一日縱走 → 鳶嘴捎來 一日縱走
    368 的標題後面掛英文和品牌名，切到第一個全形直線就好。"""
    t = re.sub(r"^《[^》]*》\s*", "", t.strip())
    t = t.split("｜")[0].split("|")[0]
    return re.sub(r"\s+", " ", t).strip()[:48]


def collect_site(site, today):
    opener = make_opener()
    routes = item_routes(opener, site["base"])
    excluded = set()
    for cat in site.get("exclude_categories", []):
        excluded |= category_routes(opener, site["base"], cat)
    if excluded:
        routes = [r for r in routes if r not in excluded]
    print(f"[{site['name']}] sitemap {len(routes)} 個商品"
          + (f"（排除海外 {len(excluded)}）" if excluded else ""))
    rows = []
    for n, route in enumerate(routes, 1):
        prod, url = load_product(opener, site, route)
        if not prod or not prod["specs"]:
            continue

        if any(w in prod["title"] for w in SKIP_TITLE):
            continue
        specs = [(name, q) for name, q in prod["specs"]
                 if not any(w in name for w in SKIP_WORDS)]
        if not specs:
            continue

        # 滿額基準＝出現次數最多的名額（多數梯次還沒人報名，會停在滿額）。
        # 不能用 max：同商品連假梯次容量常加倍（戒茂斯三天平日 7、連假 14），
        # 用 max 會把所有平日梯次誤判成「已報名 7 人」。次數相同時取大的。
        counts = Counter(q for _, q in specs)
        base = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if base <= 0:
            continue

        for name, q in specs:
            if q <= 0 or q >= base:           # 額滿/停售，或還沒人報名
                continue
            dates = parse_dates(name, today)
            if not dates:
                print(f"  ? 日期解不出來，跳過：{name}")
                continue
            start, end = dates
            if start < today:
                continue
            rows.append({
                "site": site["key"], "site_name": site["name"],
                "route": route, "url": url,
                "title": short_title(prod["title"]),
                "spec": name.strip(),
                "start": start.isoformat(), "end": end.isoformat(),
                "stock": q, "base": base, "signed": base - q,
                "single_spec": len(specs) == 1,
            })
        if n % 15 == 0:
            print(f"  …{n}/{len(routes)}")
        time.sleep(0.4)                       # 別打太快
    print(f"[{site['name']}] 補位梯次 {len(rows)}")
    return rows


def esc(s):
    return (s.replace("\\", "\\\\").replace(";", "\\;")
             .replace(",", "\\,").replace("\n", "\\n"))


def fold(line):
    """RFC5545 每行 75 octets，超過要折行（續行開頭一個空白）。"""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 73:
            out.append(cur.decode("utf-8"))
            cur = b" " + b
        else:
            cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n".join(out)


def build_ics(rows, stamp, calname):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//hiking-ics//TW//ZH",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{calname}",
        "X-WR-CALDESC:已有人報名但尚未成團的梯次。名額為抓取當下的值。",
        "X-WR-TIMEZONE:Asia/Taipei",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    dtstamp = stamp.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for r in rows:
        s = dt.date.fromisoformat(r["start"])
        e = dt.date.fromisoformat(r["end"]) + dt.timedelta(days=1)   # DTEND 是排他的
        note = "（單一梯次，滿額基準是推估的）" if r["single_spec"] else ""
        desc = (f"剩 {r['stock']} 位｜已報名 {r['signed']}/{r['base']} 人{note}\n"
                f"梯次：{r['spec']}\n{r['url']}\n\n"
                f"名額抓取時間 {stamp.strftime('%Y-%m-%d %H:%M')} (UTC+8)，實際以商品頁為準。")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{r['site']}-{r['route']}-{r['start']}-{r['end']}@hiking-ics",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;VALUE=DATE:{s.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{e.strftime('%Y%m%d')}",
            fold(f"SUMMARY:剩{r['stock']} {esc(r['title'])}｜{r['site_name']}"),
            fold(f"DESCRIPTION:{esc(desc)}"),
            f"URL:{r['url']}",
            f"CATEGORIES:登山補位,{r['site_name']}",
            "TRANSP:TRANSPARENT",             # 不讓它把你標成忙碌
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def dump(keyword=None):
    """列出每個商品的所有梯次（含 0 人報名和已額滿的），不寫檔。

    ICS 只收「有人報名但未滿」的梯次，所以問「某個連假還有什麼團」
    這種問題時 data.json 不夠用 —— 滿額（0 人報名）的梯次不在裡面。
    keyword 可以是行程名或日期片段，例如 10／10 要打 `10/10`。
    """
    today = dt.datetime.now(TZ).date()
    for site in SITES:
        opener = make_opener()
        for route in item_routes(opener, site["base"]):
            prod, url = load_product(opener, site, route)
            if not prod or not prod["specs"]:
                continue
            if any(w in prod["title"] for w in SKIP_TITLE):
                continue
            title = short_title(prod["title"])
            specs = [(n, q) for n, q in prod["specs"]
                     if not any(w in n for w in SKIP_WORDS)]
            if not specs:
                continue
            counts = Counter(q for _, q in specs)
            base = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
            shown = [(n, q) for n, q in specs
                     if not keyword
                     or keyword in title
                     or keyword in n.replace("／", "/")]
            if not shown:
                continue
            print(f"\n[{site['name']}] {title}  基準{base}  {url}")
            for n, q in shown:
                d = parse_dates(n, today)
                mark = "額滿" if q == 0 else ("空團" if q >= base else f"已{base - q}人")
                when = f"{d[0]}~{d[1]}" if d else "日期解不出"
                print(f"   剩{q:>2}  {mark:<6} {when}  {n}")
            time.sleep(0.4)


def main():
    if "--dump" in sys.argv:
        i = sys.argv.index("--dump")
        dump(sys.argv[i + 1] if len(sys.argv) > i + 1 else None)
        return

    stamp = dt.datetime.now(TZ)
    today = stamp.date()

    rows = []
    for site in SITES:
        rows += collect_site(site, today)
    rows.sort(key=lambda r: (r["start"], r["stock"]))

    OUT.mkdir(exist_ok=True)
    (OUT / "hiking.ics").write_text(
        build_ics(rows, stamp, "登山團補位"), encoding="utf-8")
    for site in SITES:
        sub = [r for r in rows if r["site"] == site["key"]]
        (OUT / f"hiking-{site['key']}.ics").write_text(
            build_ics(sub, stamp, f"登山團補位 {site['name']}"), encoding="utf-8")
    (OUT / "data.json").write_text(json.dumps(
        {"updated": stamp.isoformat(), "count": len(rows), "trips": rows},
        ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n合計補位梯次 {len(rows)}，寫出 {OUT/'hiking.ics'}")
    for r in rows[:12]:
        print(f"  {r['start']} 剩{r['stock']:>2} [{r['site_name']}] {r['title']}")


if __name__ == "__main__":
    main()
