#!/usr/bin/env python3
"""
荒野旅人（travelwildtw.com）補位梯次 → ICS 訂閱行事曆

抓全站商品頁的 Nuxt SSR payload，取出每個梯次的剩餘名額，
只留「已經有人報名、但還沒滿」的梯次，輸出 docs/hiking.ics。

用 requests 直接抓商品頁 HTML（不是 API），payload 就內嵌在頁面裡，
不需要瀏覽器 session。API 端點 /item/query/... 對外會 403，別打那個。
"""

import json
from collections import Counter
import re
import sys
import time
import datetime as dt
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

BASE = "https://travelwildtw.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
OUT = Path(__file__).parent / "docs"
TZ = dt.timezone(dt.timedelta(hours=8))

# 這些選項不是固定開團日，沒有成團人數概念
SKIP_WORDS = ("包團", "私訊", "敬請期待", "規劃中", "額滿", "洽詢", "另享優惠",
              "Please register", "外籍")


def fetch(url, tries=3):
    for i in range(tries):
        try:
            req = Request(url, headers={"User-Agent": UA,
                                        "Accept-Language": "zh-TW,zh;q=0.9"})
            with urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except (URLError, HTTPError) as e:
            if i == tries - 1:
                print(f"  ! 抓不到 {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))
    return None


def item_routes():
    """從 sitemap 取商品 route。regex 要含連字號，否則 travelwildtwcomturkey-ski 會被截斷成 404。"""
    xml = fetch(f"{BASE}/sitemap.xml")
    if not xml:
        sys.exit("sitemap 抓不到，中止")
    return sorted(set(re.findall(r"/item/([A-Za-z0-9\-_]+)", xml)))


def parse_product(html):
    """從 __NUXT_DATA__ (devalue 扁平陣列) 還原商品標題與各梯次名額。

    devalue 把物件的欄位值存成索引，所以 d['specs'] 拿到的是索引，
    arr[索引] 才是真正的 list，list 裡每個元素又是索引。
    注意 prod 本身也有 quantity/size_name 欄位（全商品加總），
    不能直接掃全陣列找 size_name，會混進那個假數字 —— 要從 prod.specs 走。
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
    title = arr[prod["title"]]
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
    return {"title": title if isinstance(title, str) else "", "specs": specs}


DATE_HEAD = re.compile(r"(?:(\d{4})/)?(\d{1,2})/(\d{1,2})")
DATE_TAIL = re.compile(r"-\s*(?:(\d{4})/)?(?:(\d{1,2})/)?(\d{1,2})")


def parse_dates(size_name, today):
    """把梯次名稱解析成 (出發日, 結束日)。解不出來就回 None，寧可漏也不要寫錯日期。

    看過的格式：
      11／13(五)-11／15(日) D0.11／12      前一晚集合
      10／19(一)-21(三)                    結束日省略月份
      12／31(四)-1／3(日)                  跨年
      2026／12／16(三)-20(日)              明確年份
    """
    s = size_name.replace("／", "/").replace("（", "(").replace("）", ")")
    s = s.split("D0")[0].split("*")[0]

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
        start = dt.date(year, mo, d)

    end = start
    tail = DATE_TAIL.search(s[head.end():])
    if tail:
        ey = int(tail.group(1)) if tail.group(1) else start.year
        emo = int(tail.group(2)) if tail.group(2) else start.month
        ed = int(tail.group(3))
        try:
            end = dt.date(ey, emo, ed)
        except ValueError:
            end = start
        if end < start:                      # 跨年梯次
            try:
                end = dt.date(ey + 1, emo, ed)
            except ValueError:
                end = start
    return start, end


def short_title(t):
    """《雲海,絕壁,賞楓之旅》 鳶嘴捎來 一日縱走 → 鳶嘴捎來 一日縱走"""
    return re.sub(r"^《[^》]*》\s*", "", t).strip()


def collect():
    today = dt.datetime.now(TZ).date()
    routes = item_routes()
    print(f"sitemap 找到 {len(routes)} 個商品")
    rows = []
    for n, route in enumerate(routes, 1):
        url = f"{BASE}/item/{route}"
        html = fetch(url)
        if not html:
            continue
        prod = parse_product(html)
        if not prod or not prod["specs"]:
            continue

        specs = [(name, q) for name, q in prod["specs"]
                 if not any(w in name for w in SKIP_WORDS)]
        if not specs:
            continue

        # 滿額基準＝出現次數最多的名額（多數梯次還沒人報名，會停在滿額）。
        # 不能用 max：同商品連假梯次容量常加倍（戒茂斯三天平日 7、連假 14），
        # 用 max 會把所有 7 人梯次誤判成「已報名 7 人」。次數相同時取大的。
        counts = Counter(q for _, q in specs)
        base = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if base <= 0:
            continue

        for name, q in specs:
            if q <= 0 or q >= base:          # 額滿/停售，或還沒人報名
                continue
            dates = parse_dates(name, today)
            if not dates:
                print(f"  ? 日期解不出來，跳過：{name}")
                continue
            start, end = dates
            if start < today:
                continue
            rows.append({
                "route": route, "url": url,
                "title": short_title(prod["title"]),
                "spec": name.strip(),
                "start": start.isoformat(), "end": end.isoformat(),
                "stock": q, "base": base, "signed": base - q,
                "single_spec": len(specs) == 1,
            })
        if n % 10 == 0:
            print(f"  …{n}/{len(routes)}")
        time.sleep(0.4)                      # 別打太快
    rows.sort(key=lambda r: (r["start"], r["stock"]))
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


def build_ics(rows, stamp):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//travelwild-hiking-ics//TW//ZH",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:荒野旅人 補位梯次",
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
            f"UID:{r['route']}-{r['start']}-{r['end']}@travelwild-hiking-ics",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;VALUE=DATE:{s.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{e.strftime('%Y%m%d')}",
            fold(f"SUMMARY:剩{r['stock']} {esc(r['title'])}"),
            fold(f"DESCRIPTION:{esc(desc)}"),
            f"URL:{r['url']}",
            "CATEGORIES:登山補位",
            "TRANSP:TRANSPARENT",          # 不讓它把你標成忙碌
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main():
    stamp = dt.datetime.now(TZ)
    rows = collect()
    print(f"有人報名、未滿的梯次：{len(rows)}")

    OUT.mkdir(exist_ok=True)
    (OUT / "hiking.ics").write_text(build_ics(rows, stamp), encoding="utf-8")
    (OUT / "data.json").write_text(json.dumps(
        {"updated": stamp.isoformat(), "count": len(rows), "trips": rows},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"寫出 {OUT/'hiking.ics'}")
    for r in rows[:10]:
        print(f"  {r['start']} 剩{r['stock']} {r['title']}")


if __name__ == "__main__":
    main()
