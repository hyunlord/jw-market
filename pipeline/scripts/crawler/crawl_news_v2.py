"""drug_profiles 키워드 기반 5년치 뉴스 일회 수집(news_5years/ingredient_5years)."""

from __future__ import annotations

import argparse
import calendar
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import hashlib
import json
import unicodedata
import trafilatura
from bs4 import BeautifulSoup, NavigableString

import requests
from dotenv import load_dotenv


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
DEFAULT_DRUG_PROFILE_DIR = os.path.join(BASE_DIR, "drug_profiles")


def _normalize_disease_listing_token(s: str) -> str:
    """사이트 검색용 질환 토큰 정리. 예: 메디칼타임즈에서 '혈우병 A'는 'A' 부분일치로 노이즈가 커서 '혈우병'만 쓴다."""
    t = (s or "").strip()
    if not t:
        return t
    if re.match(r"^혈우병\s+[AB]$", t, re.I):
        return "혈우병"
    return t


def normalize_profile_disease_keywords(p: dict) -> dict:
    """프로필의 질환명을 뉴스 검색에 맞게 정규화(제자리 수정)."""
    dis = p.get("질환명")
    if not isinstance(dis, list):
        return p
    seen: set[str] = set()
    out: list[str] = []
    for x in dis:
        if x is None:
            continue
        s = _normalize_disease_listing_token(str(x).strip())
        if not s:
            continue
        low = s.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(s)
    p["질환명"] = out
    return p


def listing_keywords_for_search(site_name: str, seed_kw_list: list[str], drug_profile: dict | None) -> list[str]:
    if drug_profile:
        normalize_profile_disease_keywords(drug_profile)
    out: list[str] = []
    seen: set[str] = set()

    def add(val: object) -> None:
        if val is None:
            return
        if isinstance(val, (list, tuple)):
            for x in val:
                add(x)
            return
        s = str(val).strip()
        if not s:
            return
        low = s.lower()
        if low in seen:
            return
        seen.add(low)
        out.append(s)

    for k in seed_kw_list:
        add(k)
    if drug_profile:
        for key in (
            "약 한글명",
            "약 영문명",
            "질환명",
            "경쟁사 약 한글명",
            "경쟁사 약 영문명",
        ):
            add(drug_profile.get(key))
    if site_name in FIERCE_SITE_NAMES:
        filtered = [k for k in out if re.search(r"[A-Za-z]", k)]
        return filtered or out
    filtered = [k for k in out if re.search(r"[가-힣]", k)]
    return filtered or out


DATASET_DIR = BASE_DIR
HISTORY_FILE = os.path.join(DATASET_DIR, "scraped_urls.txt")


def _split_ko_ingredient_terms(raw: str) -> list[str]:
    s = (raw or "").strip()
    if not s:
        return []
    return [p.strip() for p in s.split("/") if p.strip()]


def _split_en_ingredient_terms(raw: str) -> list[str]:
    s = (raw or "").strip()
    if not s:
        return []
    return [p.strip() for p in s.split("+") if p.strip()]


def _dedupe_terms_preserve_order(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        low = t.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(t)
    return out


def ingredient_search_terms_from_profile(profile: dict) -> list[str]:
    """성분명 한글은 '/', 영문은 '+' 기준으로 나눈 뒤 중복 없이 순서 유지."""
    ko = _split_ko_ingredient_terms(str(profile.get("성분명 한글") or ""))
    en = _split_en_ingredient_terms(str(profile.get("성분명 영문") or ""))
    return _dedupe_terms_preserve_order(ko + en)


def _list_cached_drug_profiles(profile_dir: str) -> list[tuple[str, dict]]:
    """drug_profiles의 (약 한글명, 프로필 dict) 목록. 동일 한글명은 첫 파일만 사용."""
    by_name: dict[str, dict] = {}
    try:
        filenames = os.listdir(profile_dir)
    except OSError:
        return []
    for fn in sorted(filenames):
        if not (fn.startswith("drug_profile_") and fn.endswith(".json")):
            continue
        path = os.path.join(profile_dir, fn)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict):
            continue
        name = str(d.get("약 한글명") or "").strip()
        if not name:
            continue
        if name not in by_name:
            by_name[name] = d
    return [(n, by_name[n]) for n in sorted(by_name.keys())]


def _format_elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    h = s // 3600
    m = (s % 3600) // 60
    r = s % 60
    return f"{h}시간 {m}분 {r}초"


DAILYPHARM_YEARS_BACK_DEFAULT = 5
DAILYPHARM_YEARS_BACK = DAILYPHARM_YEARS_BACK_DEFAULT

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


def _normalize_yakup_news_url_for_dedupe(url: str) -> str:
    s = (url or "").strip()
    if not s or "yakup.com" not in s.lower():
        return s
    if "/news/index.html" not in s:
        return s
    m = re.search(r"(?i)[?&]nid=(\d+)", s)
    if not m:
        return s
    return f"https://www.yakup.com/news/index.html?mode=view&nid={m.group(1)}"


def load_scraped_urls(history_file: str | None = None) -> set[str]:
    hf = history_file if history_file is not None else HISTORY_FILE
    if not os.path.exists(hf):
        return set()
    with open(hf, "r", encoding="utf-8") as f:
        return set(
            _normalize_yakup_news_url_for_dedupe(line.strip())
            for line in f
            if line.strip()
        )


def append_scraped_url(url: str, history_file: str | None = None) -> None:
    hf = history_file if history_file is not None else HISTORY_FILE
    url = _normalize_yakup_news_url_for_dedupe(url)
    os.makedirs(os.path.dirname(hf), exist_ok=True)
    with open(hf, "a", encoding="utf-8") as f:
        f.write(f"{url}\n")


def _url_hitnews(keyword: str, page_1based: int) -> str:
    q = urlencode(
        {"page": str(page_1based), "sc_area": "A", "view_type": "sm", "sc_word": keyword},
        encoding="utf-8",
    )
    return f"https://www.hitnews.co.kr/news/articleList.html?{q}"


def _url_yakup(keyword: str, page_1based: int) -> str:
    num_start = (page_1based - 1) * 15
    return (
        f"https://www.yakup.com/search/index.html?num_start={num_start}"
        f"&keyword=&csearch_word={quote(keyword)}&csearch_type=news&cs_scope=&mode=&pmode="
    )


def _url_biospectator(keyword: str, page_1based: int) -> str:
    return (
        f"https://www.biospectator.com/section/search_list?"
        f"searchkey={quote(keyword)}&page={page_1based:02d}"
    )


def _url_dailypharm(keyword: str, page_1based: int) -> str:
    end_d = datetime.now().date()
    years_back = int(DAILYPHARM_YEARS_BACK)
    target_year = end_d.year - years_back
    try:
        start_d = end_d.replace(year=target_year)
    except ValueError:
        last_day = calendar.monthrange(target_year, end_d.month)[1]
        start_d = end_d.replace(year=target_year, day=min(end_d.day, last_day))

    q = urlencode(
        {
            "dropBarMode": "search",
            "searchOption": "any",
            "searchStartDate": start_d.strftime("%Y.%m.%d"),
            "searchEndDate": end_d.strftime("%Y.%m.%d"),
            "searchKeyword": keyword,
            "page": str(page_1based),
        },
        encoding="utf-8",
    )
    return f"https://www.dailypharm.com/user/news/search?{q}"


def _url_medicaltimes(keyword: str, page_0based: int) -> str:
    q = urlencode(
        {"page": str(page_0based), "keyword": keyword},
        encoding="utf-8",
    )
    return f"https://www.medicaltimes.com/Main/Search.php?{q}"


def _url_pharmnews(keyword: str, page_1based: int) -> str:
    q = urlencode(
        {
            "page": str(page_1based),
            "sc_area": "A",
            "view_type": "sm",
            "sc_word": keyword,
            "box_idxno": "0",
        },
        encoding="utf-8",
    )
    return f"https://www.pharmnews.com/news/articleList.html?{q}"


def _url_bosa(keyword: str, page_1based: int) -> str:
    q = urlencode(
        {"sc_area": "A", "view_type": "sm", "sc_word": keyword, "page": str(page_1based)},
        encoding="utf-8",
    )
    return f"https://www.bosa.co.kr/news/articleList.html?{q}"


def _url_hankyung(keyword: str, page_1based: int) -> str:
    q = urlencode(
        {"query": keyword, "exact": keyword, "page": str(page_1based)},
        encoding="utf-8",
    )
    return f"https://search.hankyung.com/search/news?{q}"


def _url_monews(keyword: str, page_1based: int) -> str:
    q = urlencode(
        {"sc_area": "A", "view_type": "sm", "sc_word": keyword, "page": str(page_1based)},
        encoding="utf-8",
    )
    return f"https://www.monews.co.kr/news/articleList.html?{q}"


def _url_kpanews(keyword: str, page_1based: int) -> str:
    q = urlencode(
        {"sc_area": "A", "view_type": "sm", "sc_word": keyword, "page": str(page_1based)},
        encoding="utf-8",
    )
    return f"https://www.kpanews.co.kr/news/articleList.html?{q}"


def _url_newsmp(keyword: str, page_1based: int) -> str:
    q = urlencode(
        {
            "page": str(page_1based),
            "sc_area": "A",
            "view_type": "sm",
            "sc_word": keyword,
            "box_idxno": "",
        },
        encoding="utf-8",
    )
    return f"https://www.newsmp.com/news/articleList.html?{q}"


def _url_medipana(keyword: str, page_1based: int) -> str:
    q = urlencode(
        {
            "page": str(page_1based),
            "sc_area": "A",
            "view_type": "sm",
            "news_search_type": "1",
            "sc_word": keyword,
            "box_idxno": "0",
        },
        encoding="utf-8",
    )
    return f"https://www.medipana.com/news/articleList.html?{q}"


def _url_fierce_search(keyword: str, _page_1based: int) -> str:
    q = urlencode(
        {
            "fulltext_search": keyword,
            "dns": "fiercehealthcare_com,fiercebiotech_com,fiercepharma_com",
        },
        encoding="utf-8",
    )
    return f"https://www.fiercebiotech.com/search-results?{q}"


SITE_CONFIGS = {
    "바이오스펙테이터": {
        "type": "search",
        "base_url": "https://www.biospectator.com",
        "list_fetch": "requests",
        "link_extract": "biospectator_search",
        "paging": "page_1based",
        "url_builder": _url_biospectator,
    },
    "히트뉴스": {
        "type": "search",
        "base_url": "https://www.hitnews.co.kr",
        "list_fetch": "hitnews_search_post",
        "link_extract": "hitnews_search",
        "paging": "page_1based",
        "url_builder": _url_hitnews,
    },
    "약업신문": {
        "type": "search",
        "base_url": "https://www.yakup.com",
        "list_fetch": "requests",
        "link_extract": "yakup_search",
        "paging": "page_1based",
        "url_builder": _url_yakup,
    },
    "데일리팜": {
        "type": "search",
        "base_url": "https://www.dailypharm.com",
        "list_fetch": "requests",
        "link_extract": "dailypharm_act_list",
        "paging": "page_1based",
        "url_builder": _url_dailypharm,
    },
    "메디칼타임즈": {
        "type": "search",
        "base_url": "https://www.medicaltimes.com",
        "list_fetch": "requests",
        "link_extract": "medicaltimes_newsview",
        "paging": "page_0based",
        "url_builder": _url_medicaltimes,
    },
    "팜뉴스": {
        "type": "search",
        "base_url": "https://www.pharmnews.com",
        "list_fetch": "hitnews_search_post",
        "link_extract": "altlist_webzine",
        "paging": "page_1based",
        "url_builder": _url_pharmnews,
    },
    "의학신문": {
        "type": "search",
        "base_url": "https://www.bosa.co.kr",
        "list_fetch": "hitnews_search_post",
        "link_extract": "altlist_webzine",
        "paging": "page_1based",
        "url_builder": _url_bosa,
    },
    "한경바이오인사이트": {
        "type": "search",
        "base_url": "https://www.hankyung.com",
        "list_fetch": "requests",
        "link_extract": "hankyung_article",
        "paging": "page_1based",
        "url_builder": _url_hankyung,
    },
    "메디칼업저버": {
        "type": "search",
        "base_url": "https://www.monews.co.kr",
        "list_fetch": "requests",
        "link_extract": "monews_section_type2",
        "paging": "page_1based",
        "url_builder": _url_monews,
    },
    "약사공론": {
        "type": "search",
        "base_url": "https://www.kpanews.co.kr",
        "list_fetch": "hitnews_search_post",
        "link_extract": "kpanews_altlist",
        "paging": "page_1based",
        "url_builder": _url_kpanews,
    },
    "의약뉴스": {
        "type": "search",
        "base_url": "https://www.newsmp.com",
        "list_fetch": "hitnews_search_post",
        "link_extract": "newsmp_article_list",
        "paging": "page_1based",
        "url_builder": _url_newsmp,
    },
    "메디파나뉴스": {
        "type": "search",
        "base_url": "https://www.medipana.com",
        "list_fetch": "hitnews_search_post",
        "link_extract": "kpanews_altlist",
        "paging": "page_1based",
        "url_builder": _url_medipana,
    },
    # FiercePharma removed in v2 — GCP crawling blocked.
    # "FiercePharma": {
    #     "type": "search",
    #     "list_url": _url_fierce_search("Livalo", 1),
    #     "base_url": "https://www.fiercepharma.com",
    #     "list_fetch": "playwright_fierce_search",
    #     "link_extract": "fierce_search",
    #     "paging": "page_1based",
    #     "url_builder": _url_fierce_search,
    # },
}

FIERCE_SITE_NAMES = frozenset()


def _normalized_source_name(site_name: str) -> str:
    if site_name in FIERCE_SITE_NAMES:
        return "Fierce"
    return site_name


_KNOWN_OUTLET_TITLE_TOKENS: frozenset[str] = frozenset(
    set(SITE_CONFIGS)
    | {_normalized_source_name(k) for k in SITE_CONFIGS}
    | {
        "Fierce Pharma",
        "Fierce Healthcare",
        "Fierce Biotech",
    }
)


def _title_token_matches_outlet(token: str) -> bool:
    tok = (token or "").strip()
    if not tok:
        return False
    low = tok.casefold()
    for o in _KNOWN_OUTLET_TITLE_TOKENS:
        if o.casefold() == low:
            return True
    return False


def canonical_article_title(title: str) -> str:
    """파일명·병합·JSON에 쓸 제목. 앞의 [언론사]·끝의 ' - 언론사' 등 매체명 표기를 뗀다."""
    t = normalize_article_title(title)
    if not t:
        return t
    m = re.match(r"^\[([^\]]+)\]\s*(.+)$", t)
    if m:
        inner, rest = m.group(1).strip(), m.group(2).strip()
        if rest and _title_token_matches_outlet(inner):
            t = rest
    while " - " in t:
        head, tail = t.rsplit(" - ", 1)
        tt = tail.strip()
        if tt and _title_token_matches_outlet(tt):
            t = head.strip()
        else:
            break
    return re.sub(r"\s+", " ", t).strip()


def build_search_list_urls(site_name: str, keyword: str, max_pages: int) -> list[str]:
    if site_name not in SITE_CONFIGS:
        return []
    cfg = SITE_CONFIGS[site_name]
    builder = cfg.get("url_builder")
    paging = cfg.get("paging")
    if not builder or not paging:
        return []
    if paging == "page_0based":
        urls = [builder(keyword, p) for p in range(0, max(0, max_pages))]
    else:
        urls = [builder(keyword, p) for p in range(1, max_pages + 1)]
    # url_builder가 page 인자를 무시해서 동일 URL이 반복 생성되는 사이트(FiercePharma)
    # 처리: 순서를 유지한 채 중복 제거. 페이지마다 URL이 다른 사이트에는 영향 없음.
    return list(dict.fromkeys(urls))


def iter_site_paginated_configs(site_name: str, keyword: str, max_pages: int):
    import copy

    if site_name not in SITE_CONFIGS:
        return
    for i, list_url in enumerate(build_search_list_urls(site_name, keyword, max_pages), start=1):
        config = copy.deepcopy(SITE_CONFIGS[site_name])
        config["list_url"] = list_url
        yield config, i


def _load_list_html(list_url: str, list_fetch: str) -> str:
    if list_fetch == "hitnews_search_post":
        try:
            parsed = urlparse(list_url)
            form: dict[str, str] = {}
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
                form[key] = values[0] if values else ""
            path = parsed.path or "/"
            post_url = f"{parsed.scheme}://{parsed.netloc}{path}"
            resp = requests.post(post_url, data=form, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            return resp.text or ""
        except Exception:
            return ""

    if list_fetch == "playwright_fierce_search":
        return _load_list_html_fierce_playwright(list_url)

    try:
        resp = requests.get(list_url, headers=HEADERS, timeout=45)
        resp.raise_for_status()
        return resp.text or ""
    except Exception:
        return ""


# Fierce 통합 검색은 Drupal+Solr 백엔드를 호출하는 SPA라 requests로는 첫 10건만
# 받음. Playwright headless가 자동으로 보내는 sec-ch-ua: "HeadlessChrome" 가
# Cloudflare에서 두번째 page[offset]=10 호출부터 cf-mitigated: challenge 로
# 차단된다. 모든 요청에서 sec-ch-ua를 정상 Chrome 값으로 강제 교체하고
# "See more articles"를 클릭해 누적 HTML을 반환한다. 검증된 기준값: livalo
# 키워드에서 32건.
_FIERCE_PW_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
_FIERCE_PW_SEC_CH_UA = (
    '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="24"'
)
_FIERCE_PW_STEALTH_INIT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});"
    "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});"
    "window.chrome = {runtime: {}};"
)


def _load_list_html_fierce_playwright(list_url: str) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return ""

    html = ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            try:
                ctx = browser.new_context(
                    user_agent=_FIERCE_PW_UA,
                    locale="en-US",
                    viewport={"width": 1366, "height": 900},
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "sec-ch-ua": _FIERCE_PW_SEC_CH_UA,
                        "sec-ch-ua-mobile": "?0",
                        "sec-ch-ua-platform": '"macOS"',
                    },
                )
                ctx.add_init_script(_FIERCE_PW_STEALTH_INIT)

                def _route_handler(route, request):
                    try:
                        headers = dict(request.headers)
                        headers["sec-ch-ua"] = _FIERCE_PW_SEC_CH_UA
                        headers["sec-ch-ua-mobile"] = "?0"
                        headers["sec-ch-ua-platform"] = '"macOS"'
                        headers["user-agent"] = _FIERCE_PW_UA
                        route.continue_(headers=headers)
                    except Exception:
                        try:
                            route.continue_()
                        except Exception:
                            pass

                ctx.route("**/*", _route_handler)
                page = ctx.new_page()
                page.goto(list_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(2_000)
                try:
                    page.wait_for_selector("div.search-item", timeout=15_000)
                except Exception:
                    pass

                prev_items = -1
                stable_rounds = 0
                for _ in range(160):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(700)
                    try:
                        more = page.get_by_text("See more articles", exact=False)
                        if more.count() > 0:
                            target = more.last
                            target.scroll_into_view_if_needed(timeout=2_500)
                            page.wait_for_timeout(200)
                            target.click(timeout=3_000)
                            page.wait_for_timeout(4_500)
                            try:
                                page.wait_for_load_state("networkidle", timeout=4_000)
                            except Exception:
                                pass
                    except Exception:
                        pass

                    try:
                        item_count = page.locator("div.search-item").count()
                    except Exception:
                        item_count = -1
                    if item_count <= prev_items:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0
                    prev_items = item_count
                    if stable_rounds >= 3:
                        break

                html = page.content()
            finally:
                browser.close()
    except Exception:
        return html or ""
    return html or ""


def _extract_ndsoft_idxno(soup: BeautifulSoup, base: str, cap: int) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if "articleView.html" not in href:
            continue
        if "idxno=" not in href and "nid=" not in href:
            continue
        full = urljoin(base, href)
        if full in seen:
            continue
        seen.add(full)
        title = (a.get_text(strip=True) or "제목 없음")[:200]
        out.append((full, title))
        if len(out) >= cap:
            break
    return out


def _extract_altlist_webzine(soup: BeautifulSoup, base: str, cap: int) -> list[tuple[str, str]]:
    root = None
    for wrap in soup.select("div#sections.altlist"):
        cand = wrap.select_one("ul.altlist-webzine")
        if cand is None or not cand.select(":scope > li"):
            continue
        root = cand
        break
    if root is None:
        cand = soup.select_one("ul.altlist-webzine")
        if cand is not None and cand.select(":scope > li"):
            root = cand
    if root is None:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for li in root.select(":scope > li"):
        a = li.select_one('a[href*="articleView.html"]')
        if not a:
            continue
        href = (a.get("href") or "").strip()
        if "idxno=" not in href:
            continue
        full = urljoin(base, href)
        if full in seen:
            continue
        seen.add(full)
        title = (a.get_text(strip=True) or "제목 없음")[:200]
        out.append((full, title))
        if len(out) >= cap:
            break
    return out


def _extract_newsmp_article_list(soup: BeautifulSoup, base: str, cap: int) -> list[tuple[str, str]]:
    root = soup.select_one("div.article-list")
    if root is None:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for block in root.select("div.list-block"):
        a = block.select_one('a[href*="articleView.html"]')
        if not a:
            continue
        href = (a.get("href") or "").strip()
        if "idxno=" not in href:
            continue
        full = urljoin(base, href)
        if full in seen:
            continue
        seen.add(full)
        title = (a.get_text(strip=True) or "제목 없음")[:200]
        out.append((full, title))
        if len(out) >= cap:
            break
    return out


def _extract_kpanews_altlist(soup: BeautifulSoup, base: str, cap: int) -> list[tuple[str, str]]:
    ul = None
    for wrap in soup.select("div#sections.altlist"):
        cand = wrap.select_one("ul.altlist-webzine")
        if cand is None or not cand.select(":scope > li"):
            continue
        ul = cand
        break
    if ul is None:
        wrap = soup.select_one("div#sections")
        if wrap is not None:
            ul = wrap.select_one("ul.altlist-webzine")
    if ul is None:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for li in ul.select(":scope > li"):
        a = li.select_one('a[href*="articleView.html"]')
        if not a:
            continue
        href = (a.get("href") or "").strip()
        if "idxno=" not in href:
            continue
        full = urljoin(base, href)
        if full in seen:
            continue
        seen.add(full)
        title = (a.get_text(strip=True) or "제목 없음")[:200]
        out.append((full, title))
        if len(out) >= cap:
            break
    return out


def _extract_monews_section_type2(soup: BeautifulSoup, base: str, cap: int) -> list[tuple[str, str]]:
    sec = soup.select_one("section#section-list")
    if sec is None:
        return []
    ul = sec.select_one("ul.type2")
    if ul is None:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for li in ul.select(":scope > li"):
        a = li.select_one('a[href*="articleView.html"]')
        if not a:
            continue
        href = (a.get("href") or "").strip()
        if "idxno=" not in href:
            continue
        full = urljoin(base, href)
        if full in seen:
            continue
        seen.add(full)
        title = (a.get_text(strip=True) or "제목 없음")[:200]
        out.append((full, title))
        if len(out) >= cap:
            break
    return out


def _extract_dailypharm_act_list(soup: BeautifulSoup, cap: int) -> list[tuple[str, str]]:
    base = "https://www.dailypharm.com"
    ul = soup.select_one("main.act.search ul.act_list_sty2.margin_l")
    if ul is None:
        ul = soup.select_one("ul.act_list_sty2.margin_l")
    if ul is None:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for li in ul.select(":scope > li"):
        a = li.select_one('a[href*="/user/news/"]')
        if not a:
            continue
        href = (a.get("href") or "").strip()
        if not href:
            continue
        full = urljoin(base, href)
        m = re.search(r"/user/news/(\d{4,8})", full)
        if not m:
            continue
        canon = f"{base}/user/news/{m.group(1)}"
        if canon in seen:
            continue
        seen.add(canon)
        title = (a.get_text(strip=True) or "제목 없음")[:200]
        out.append((canon, title))
        if len(out) >= cap:
            break
    return out


def _hitnews_byline_em_to_iso_date(text: str | None) -> str | None:
    if not text or not str(text).strip():
        return None
    s = str(text).strip()
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})(?:\s+\d{1,2}:\d{2})?", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _extract_hitnews_search(html: str, base: str, cap: int) -> list[tuple[str, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#section-list") or soup.select_one("#sections")
    if not root:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    for li in root.select(":scope > ul > li"):
        a = li.select_one("h4.titles a[href]")
        if not a:
            continue
        href = (a.get("href") or "").strip()
        if "articleView.html" not in href or "idxno=" not in href:
            continue
        full = urljoin(base, href)
        if full in seen:
            continue
        seen.add(full)
        title = (a.get_text(strip=True) or "제목 없음")[:200]
        list_date = ""
        byline = li.select_one("span.byline")
        if byline:
            ems = byline.find_all("em")
            if ems:
                cand = (ems[-1].get_text() or "").strip()
                iso = _hitnews_byline_em_to_iso_date(cand)
                if iso:
                    list_date = iso
        out.append((full, title, list_date))
        if len(out) >= cap:
            break
    return out


def _extract_regex_template(html: str, regex: str, template: str, cap: int) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in re.finditer(regex, html):
        url = template.format(id=m.group(1))
        if url not in seen:
            seen.add(url)
            out.append((url, "제목 없음"))
            if len(out) >= cap:
                break
    return out


def _extract_biospectator_search(html: str, base: str, cap: int) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("div.article_list")
    if not root:
        return []

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for li in root.select(":scope > ul > li"):
        if li.select_one(".pay-icon") is not None:
            continue
        a = li.select_one("strong.article_tit a[href]")
        if not a:
            a = li.select_one("a[href*='/news/view/']")
        if not a:
            continue
        href = (a.get("href") or "").strip()
        if not href or "/news/view/" not in href:
            continue
        full = urljoin(base, href)
        parsed = urlparse(full)
        if not re.search(r"^/news/view/\d+", parsed.path or ""):
            continue
        norm = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if norm in seen:
            continue
        seen.add(norm)
        title = (a.get_text(strip=True) or "제목 없음")[:200]
        out.append((norm, title))
        if len(out) >= cap:
            break
    return out


def _extract_yakup_search(html: str, base: str, cap: int) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    base_root = (base or "https://www.yakup.com").rstrip("/")
    for block in soup.select("div.info_con"):
        lis = block.select(":scope > ul > li")
        if len(lis) < 5:
            continue
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for li in lis:
            a = li.select_one('a[href*="/news/index.html"][href*="mode=view"][href*="nid="]')
            if not a:
                continue
            href = (a.get("href") or "").strip()
            full = urljoin(base_root + "/", href)
            m = re.search(r"(?i)[?&]nid=(\d+)", full)
            if not m:
                continue
            canon = f"{base_root}/news/index.html?mode=view&nid={m.group(1)}"
            if canon in seen:
                continue
            seen.add(canon)
            title = (a.get_text(strip=True) or "제목 없음")[:200]
            out.append((canon, title))
            if len(out) >= cap:
                return out
        if out:
            return out
    return []


def _extract_medicaltimes_newsview(html: str, cap: int) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    wrap = soup.select_one("div.newsList_wrap.subPage")
    if wrap is None:
        return []

    base = "https://www.medicaltimes.com"
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    for art in wrap.select("article.newsList_cont"):
        a = art.select_one('a[href*="NewsView.html?ID="]')
        if not a:
            continue
        href = (a.get("href") or "").strip()
        if not href:
            continue
        full = urljoin(base, href)
        m = re.search(r"[?&]ID=(\d+)", full, re.I)
        if not m:
            continue
        url = f"{base}/Main/News/NewsView.html?ID={m.group(1)}"
        if url in seen:
            continue
        seen.add(url)
        title = (a.get_text(strip=True) or "제목 없음")[:200]
        out.append((url, title))
        if len(out) >= cap:
            break
    return out


def _hankyung_list_item_is_premium(anchor) -> bool:
    """한경 검색 목록에서 유료(프리미엄) 기사 여부."""
    em = anchor.select_one("em.tit") or anchor.select_one("em")
    if em is None:
        return False
    if (em.get("data-pm") or "").strip().upper() == "Y":
        return True
    for img in em.select("img"):
        alt = (img.get("alt") or "").strip()
        if alt == "프리미엄":
            return True
    return False


def _extract_hankyung_links(soup: BeautifulSoup, cap: int) -> list[tuple[str, str]]:
    root = soup.select_one("ul.article")
    if root is None:
        return []
    base = "https://www.hankyung.com"
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for li in root.select(":scope > li"):
        tw = li.select_one("div.txt_wrap")
        if not tw:
            continue
        a = tw.select_one("a[href]")
        if not a:
            continue
        if _hankyung_list_item_is_premium(a):
            continue
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        full = urljoin(base, href)
        parsed = urlparse(full)
        if "hankyung.com" not in (parsed.netloc or "").lower():
            continue
        path = (parsed.path or "").lower()
        if "/article/" not in path:
            continue
        if full in seen:
            continue
        seen.add(full)
        title = (a.get_text(strip=True) or "제목 없음")[:200]
        out.append((full, title))
        if len(out) >= cap:
            break
    return out


def _parse_fierce_date_to_iso(text: str | None) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    for fmt in ("%b %d, %Y %I:%M%p", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    m = re.search(r"([A-Za-z]{3}\s+\d{1,2},\s+\d{4})", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()
        except Exception:
            pass
    return ""


def _extract_fierce_search(html: str, cap: int) -> list[tuple[str, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for row in soup.select("div.search-item"):
        a = row.select_one(".row.mt-2.mt-md-3 .element-title.small a[href]")
        if not a:
            continue
        href = (a.get("href") or "").strip()
        if not href:
            continue
        full = urljoin("https://www.fiercebiotech.com", href)
        parsed = urlparse(full)
        host = (parsed.netloc or "").lower()
        if "fiercepharma.com" not in host and "fiercebiotech.com" not in host:
            continue
        if full in seen:
            continue
        seen.add(full)
        title = (a.get_text(" ", strip=True) or "제목 없음")[:250]
        date_el = row.select_one(".date.d-block.d-md-inline-block")
        date_iso = _parse_fierce_date_to_iso(date_el.get_text(" ", strip=True) if date_el else "")
        out.append((full, title, date_iso))
        if len(out) >= cap:
            break
    return out


def get_article_links(site_name: str, config: dict, max_links: int | None = 30) -> list[tuple[str, ...]]:
    cap = 10**6 if max_links is None else max_links
    list_url = config.get("list_url")
    if not list_url:
        return []

    list_fetch = config.get("list_fetch", "requests")
    link_extract = config.get("link_extract")
    base = config.get("base_url", "")

    html = _load_list_html(list_url, list_fetch)
    if not html:
        print(
            f"[{site_name}] 목록 페이지 로드 실패: {list_url[:120]}"
            + ("..." if len(list_url) > 120 else "")
        )
        return []

    if link_extract == "ndsoft_idxno":
        soup = BeautifulSoup(html, "html.parser")
        return _extract_ndsoft_idxno(soup, base, cap)

    if link_extract == "altlist_webzine":
        soup = BeautifulSoup(html, "html.parser")
        return _extract_altlist_webzine(soup, base, cap)

    if link_extract == "monews_section_type2":
        soup = BeautifulSoup(html, "html.parser")
        return _extract_monews_section_type2(soup, base, cap)

    if link_extract == "kpanews_altlist":
        soup = BeautifulSoup(html, "html.parser")
        return _extract_kpanews_altlist(soup, base, cap)

    if link_extract == "newsmp_article_list":
        soup = BeautifulSoup(html, "html.parser")
        return _extract_newsmp_article_list(soup, base, cap)

    if link_extract == "hitnews_search":
        return _extract_hitnews_search(html, base, cap)

    if link_extract == "yakup_search":
        return _extract_yakup_search(html, base, cap)

    if link_extract == "biospectator_search":
        return _extract_biospectator_search(html, base, cap)

    if link_extract == "regex_template":
        regex = config.get("article_url_regex")
        template = config.get("article_url_template")
        if regex and template:
            return _extract_regex_template(html, regex, template, cap)
        return []

    if link_extract == "dailypharm_regex":
        regex = config.get("article_url_regex")
        template = config.get("article_url_template")
        if regex and template:
            return _extract_regex_template(html, regex, template, cap)
        return []

    if link_extract == "dailypharm_act_list":
        soup = BeautifulSoup(html, "html.parser")
        return _extract_dailypharm_act_list(soup, cap)

    if link_extract == "medicaltimes_newsview":
        return _extract_medicaltimes_newsview(html, cap)

    if link_extract == "hankyung_article":
        soup = BeautifulSoup(html, "html.parser")
        return _extract_hankyung_links(soup, cap)

    if link_extract == "fierce_search":
        return _extract_fierce_search(html, cap)

    return []


def fetch_html_requests(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception:
        return None


def clean_article_content(text: str) -> str:
    if not text:
        return text
    lines = text.split("\n")
    out = []
    skip_until_empty = False
    for line in lines:
        s = line.strip()
        if skip_until_empty:
            if not s:
                skip_until_empty = False
            continue
        if len(s) <= 1 and s != "" and s.isascii() is False and s in "가나다라":
            continue
        if s in ("가", "PR", "전국 지역별") or s.startswith("데일리팜맵 바로가기"):
            continue
        if "관련기사" in s or "오늘의 TOP" in s or "댓글을 남겨주세요" in s:
            skip_until_empty = True
            continue
        out.append(line)
    return "\n".join(out).strip()


_DAILYPHARM_BYLINE_RE = re.compile(r"^\s*\[데일리팜=[^\]]+\]\s*")
_MEDICAL_NEWS_BYLINE_RE = re.compile(r"^\s*\[의학신문[^]]+\]\s*")
_MEDIPANA_BYLINE_RE = re.compile(r"\[메디파나뉴스[^\]]*?\]\s*")
_MEDICAL_OBSERVER_BYLINE_RE = re.compile(r"\[메디칼업저버[^\]]*?\]\s*")
_YAKUP_INPUT_RE = re.compile(r"^입력\s+\d{4}\.\d{2}\.\d{2}(?:\s+\d{2}:\d{2})?\s*$")
_YAKUP_MODIFY_RE = re.compile(r"^수정\s+\d{4}\.\d{2}\.\d{2}(?:\s+\d{2}:\d{2})?\s*$")


def _normalize_yakup_compare_text(s: str) -> str:
    t = (s or "").strip()
    t = t.replace("…", "...").replace("‘", "'").replace("’", "'")
    t = t.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", "", t)


def _yakup_line_matches_title(line: str, title: str) -> bool:
    title = (title or "").strip()
    if not title:
        return False
    line_cmp = _normalize_yakup_compare_text(line)
    title_cmp = _normalize_yakup_compare_text(title)
    if not line_cmp or not title_cmp:
        return False
    if line_cmp == title_cmp:
        return True
    if line_cmp in title_cmp or title_cmp in line_cmp:
        return True
    return line_cmp[:25] == title_cmp[:25]


def _strip_yakup_boilerplate(text: str, title: str | None = None) -> str:
    if not text:
        return text
    lines = text.split("\n")
    input_idx = next(
        (i for i, line in enumerate(lines) if _YAKUP_INPUT_RE.match(line.strip())),
        -1,
    )
    if input_idx >= 0:
        lines = lines[input_idx + 1 :]
        if lines and _YAKUP_MODIFY_RE.match(lines[0].strip()):
            lines = lines[1:]
    else:
        while lines and _yakup_line_matches_title(lines[0].strip(), title or ""):
            lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()


def _strip_leading_byline(text: str, pattern: re.Pattern[str]) -> str:
    if not text:
        return text
    text = pattern.sub("", text, count=1)
    return text.lstrip()


def _strip_medical_observer_boilerplate(text: str) -> str:
    if not text:
        return text
    m = _MEDICAL_OBSERVER_BYLINE_RE.search(text)
    if not m:
        return text
    return text[m.end() :].strip()


def _strip_medipana_boilerplate(text: str) -> str:
    if not text:
        return text
    m = _MEDIPANA_BYLINE_RE.search(text)
    if not m:
        return text
    return text[m.end() :].strip()


def postprocess_content_by_source(
    text: str,
    site_name: str,
    *,
    title: str | None = None,
) -> str:
    if site_name == "데일리팜":
        return _strip_leading_byline(text, _DAILYPHARM_BYLINE_RE)
    if site_name == "의학신문":
        return _strip_leading_byline(text, _MEDICAL_NEWS_BYLINE_RE)
    if site_name == "메디파나뉴스":
        return _strip_medipana_boilerplate(text)
    if site_name == "메디칼업저버":
        return _strip_medical_observer_boilerplate(text)
    if site_name == "약업신문":
        return _strip_yakup_boilerplate(text, title)
    return text


def _biospectator_html_is_paywalled(html: str) -> bool:
    if "유료 뉴스서비스 BioS+" in html:
        return True
    if "pay_article_tit" in html:
        return True
    return False


def _extract_biospectator_date_from_html(html: str) -> str | None:
    m = re.search(r"기사입력\s*[:：]\s*(\d{4}-\d{2}-\d{2})", html)
    if m:
        return m.group(1)
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one("div.datetime")
    if el:
        m2 = re.search(r"(\d{4}-\d{2}-\d{2})", el.get_text())
        if m2:
            return m2.group(1)
    return None


def _extract_medicaltimes_date_from_html(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one("div.date_info")
    if el:
        m = re.search(r"발행날짜\s*:\s*(\d{4}-\d{2}-\d{2})", el.get_text())
        if m:
            return m.group(1)
    m = re.search(r'"Publish_date"\s*:\s*"(\d{4}-\d{2}-\d{2})', html)
    if m:
        return m.group(1)
    return None


def _extract_yakup_date_from_html(html: str) -> str | None:
    m = re.search(r"입력\s*(\d{4})\.(\d{2})\.(\d{2})", html)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _meta_article_published_date(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find("meta", property="article:published_time")
    if not el:
        return None
    raw = (el.get("content") or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else None


def _extract_dailypharm_news_content(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("div#newsContent.news_content.ck-content") or soup.select_one(
        "div#newsContent"
    )
    if root is None:
        return None
    chunks: list[str] = []
    for p in root.find_all("p", recursive=True):
        t = p.get_text(separator=" ", strip=True)
        if t:
            chunks.append(t)
    if not chunks:
        return None
    return "\n\n".join(chunks).strip()


NDSOFT_ARTICLE_VIEW_SITES = frozenset({"약사공론", "메디파나뉴스", "메디칼업저버"})


def _extract_ndsoft_article_view_content(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#article-view-content-div")
    if root is None:
        return None
    for tag in root.find_all(["script", "style"]):
        tag.decompose()
    for ad in root.select("div.ad-template"):
        ad.decompose()
    for ad in root.find_all(id=re.compile(r"^AD\d")):
        ad.decompose()
    chunks: list[str] = []
    for el in root.descendants:
        if isinstance(el, NavigableString):
            parent = el.parent
            if parent is None or parent.name in {"p", "figcaption", "script", "style"}:
                continue
            t = str(el).strip()
            if t:
                chunks.append(t)
            continue
        if el.name in {"p", "figcaption"}:
            t = el.get_text(separator=" ", strip=True)
            if t:
                chunks.append(t)
    if not chunks:
        t = root.get_text(separator="\n", strip=True)
        return t if len(t) >= 50 else None
    return "\n\n".join(chunks).strip()


def _hankyung_skip_body_paragraph(para: str) -> bool:
    t = para.strip()
    if not t:
        return True
    if t.upper() == "ADVERTISEMENT":
        return True
    if re.search(r"@hankyung\.com", t, re.I) and re.search(
        r"(기자|특파원|객원|논설위원|칼럼니스트)", t
    ):
        return True
    if re.fullmatch(r"[\w.+-]+@hankyung\.com", t, re.I):
        return True
    return False


def _extract_hankyung_news_content(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    wrap = soup.select_one("div.article-body-wrap")
    if wrap:
        summ = wrap.select_one("div.summary")
        if summ:
            st = summ.get_text(separator=" ", strip=True)
            if st:
                parts.append(st)
    body = soup.select_one("div#articletxt.article-body") or soup.select_one("#articletxt")
    if body is not None:
        for bad in body.find_all(["script", "style"]):
            bad.decompose()
        for br in body.find_all("br"):
            br.replace_with("\n")
        raw = body.get_text(separator="", strip=False)
        paras = [p.strip() for p in re.split(r"\n+", raw) if p.strip()]
        kept = [p for p in paras if not _hankyung_skip_body_paragraph(p)]
        if kept:
            parts.append("\n\n".join(kept))
    out = "\n\n".join(parts).strip()
    return out if out else None


def extract_news_content(url: str, html: str, site_name: str | None = None) -> dict | None:
    if not html:
        return None
    if site_name == "바이오스펙테이터" and _biospectator_html_is_paywalled(html):
        return None
    out = trafilatura.extract(html, output_format="json", with_metadata=True, include_comments=False, include_tables=False)
    custom_body: str | None = None
    if site_name == "데일리팜":
        custom_body = _extract_dailypharm_news_content(html)
    elif site_name == "한경바이오인사이트":
        custom_body = _extract_hankyung_news_content(html)
    elif site_name in NDSOFT_ARTICLE_VIEW_SITES:
        custom_body = _extract_ndsoft_article_view_content(html)

    if not out:
        if not custom_body or len(custom_body.strip()) < 50:
            return None
        data: dict = {}
        text = custom_body.strip()
    else:
        data = json.loads(out)
        if custom_body and len(custom_body.strip()) >= 50:
            text = custom_body.strip()
        else:
            text = (data.get("text") or "").strip()

    text = clean_article_content(text)
    if site_name:
        title_for_post = (data.get("title") or "").strip() if out else ""
        text = postprocess_content_by_source(text, site_name, title=title_for_post)
    if not text or len(text) < 50:
        return None
    date_str = (data.get("date") or "").strip()
    if site_name == "바이오스펙테이터":
        fixed = _extract_biospectator_date_from_html(html)
        if fixed:
            date_str = fixed
    elif site_name == "메디칼타임즈":
        fixed = _extract_medicaltimes_date_from_html(html)
        if fixed:
            date_str = fixed
    elif site_name == "약업신문":
        fixed = _extract_yakup_date_from_html(html)
        if fixed:
            date_str = fixed
    else:
        meta_d = _meta_article_published_date(html)
        if meta_d:
            date_str = meta_d
    title = (data.get("title") or "").strip() or "제목 없음"
    if title == "제목 없음" and site_name == "데일리팜":
        soup = BeautifulSoup(html, "html.parser")
        og = soup.find("meta", property="og:title")
        if og and (og.get("content") or "").strip():
            title = (og.get("content") or "").strip()
        else:
            h = soup.select_one("div.news_title h1") or soup.select_one("h1")
            if h:
                title = h.get_text(strip=True) or title
    elif title == "제목 없음" and site_name == "한경바이오인사이트":
        soup = BeautifulSoup(html, "html.parser")
        og = soup.find("meta", property="og:title")
        if og and (og.get("content") or "").strip():
            title = (og.get("content") or "").strip()
        else:
            h = soup.select_one("h1.headline") or soup.select_one("h1")
            if h:
                title = h.get_text(strip=True) or title
    elif title == "제목 없음" and site_name in NDSOFT_ARTICLE_VIEW_SITES:
        soup = BeautifulSoup(html, "html.parser")
        og = soup.find("meta", property="og:title")
        if og and (og.get("content") or "").strip():
            title = (og.get("content") or "").strip()
        else:
            h = soup.select_one("#article-view h1.heading") or soup.select_one("h1.heading") or soup.select_one("h1")
            if h:
                title = h.get_text(strip=True) or title
    return {
        "title": title,
        "author": (data.get("author") or "").strip(),
        "date": date_str,
        "content": text,
    }


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    s = date_str.strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        m = re.search(r"(\d{4})[./](\d{2})[./](\d{2})", s)
    if not m:
        m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime(y, mo, d)
    except ValueError:
        return None


def _cutoff_dt(months: int) -> datetime:
    now = datetime.now()
    target_month = now.month - months
    target_year = now.year + (target_month - 1) // 12
    target_month = (target_month - 1) % 12 + 1
    last_day = calendar.monthrange(target_year, target_month)[1]
    target_day = min(now.day, last_day)
    return datetime(target_year, target_month, target_day)


def _is_within_cutoff(date_str: str | None, cutoff: datetime) -> bool:
    dt = _parse_date(date_str)
    if dt is None:
        return False
    # 기사 쪽은 날짜만 있는 경우가 많아 calendar day로 맞춘다(경계일 누락 방지)
    return dt.date() >= cutoff.date()


_article_save_lock = threading.RLock()


def normalize_article_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", (title or "").strip())
    return re.sub(r"\s+", " ", t).strip()


def article_date_iso_key(date_str: str | None) -> str:
    dt = _parse_date(date_str)
    if dt is not None:
        return dt.date().isoformat()
    return "unknown"


def article_json_filename(title: str, date_str: str | None, url: str | None = None) -> str:
    norm = canonical_article_title(title) or normalize_article_title(title) or "제목 없음"
    iso = article_date_iso_key(date_str)
    u = (url or "").strip()
    if u:
        hkey = f"{norm}\0{iso}\0{_normalize_yakup_news_url_for_dedupe(u)}"
    else:
        hkey = f"{norm}\0{iso}"
    h = hashlib.sha256(hkey.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r'[<>:"/\\|?*\n\r\t\0]', "", norm)
    slug = (slug.strip() or "untitled")[:48]
    return f"{slug}__{iso}__{h}.json"


# 유사 기사 병합: 전수 비교 대신 날짜 ±N일 안의 JSON만 제목·본문 유사도 검사
SIMILAR_ARTICLE_WINDOW_DAYS = 30
SIMILAR_TITLE_RATIO_MIN = 0.88
SIMILAR_CONTENT_RATIO_MIN = 0.76
SIMILAR_CONTENT_SNIPPET_CHARS = 2400

_FILENAME_DATE_RE = re.compile(r"__(\d{4}-\d{2}-\d{2})__")


def _article_date_from_filename(basename: str) -> date | None:
    m = _FILENAME_DATE_RE.search(basename)
    if not m:
        return None
    try:
        y, mo, d = map(int, m.group(1).split("-"))
        return date(y, mo, d)
    except ValueError:
        return None


def _doc_effective_date(doc: dict) -> date | None:
    dt = _parse_date((doc.get("date") or "").strip())
    if dt is not None:
        return dt.date()
    return None


def _date_within_similarity_window(d: date, center: date, days: int = SIMILAR_ARTICLE_WINDOW_DAYS) -> bool:
    lo = center - timedelta(days=days)
    hi = center + timedelta(days=days)
    return lo <= d <= hi


def _content_snippet_for_similarity(text: str | None, max_len: int = SIMILAR_CONTENT_SNIPPET_CHARS) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t[:max_len]


def _similarity_ratio(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _list_article_json_paths_in_date_window(output_dir: str, center: date) -> list[str]:
    paths: list[str] = []
    try:
        names = os.listdir(output_dir)
    except OSError:
        return paths
    for name in names:
        if not name.endswith(".json"):
            continue
        if name.startswith("_title_mismatch__"):
            continue
        fd = _article_date_from_filename(name)
        if fd is None or not _date_within_similarity_window(fd, center):
            continue
        paths.append(os.path.join(output_dir, name))
    return paths


def _attempt_similar_article_merge(
    output_dir: str,
    site_name: str,
    url: str,
    *,
    canon_title: str,
    date_raw: str,
    content: str,
    search_keyword: str | None = None,
    keyword_contexts: dict[str, list[dict]] | None = None,
) -> str | None:
    """저장 디렉터리 안에서 날짜 ±SIMILAR_ARTICLE_WINDOW_DAYS 범위의 기사만 보고 유사하면 병합."""
    center_dt = _parse_date(date_raw)
    if center_dt is None:
        return None
    center = center_dt.date()
    new_snip = _content_snippet_for_similarity(content)
    candidates = _list_article_json_paths_in_date_window(output_dir, center)
    best_path: str | None = None
    best_score = 0.0
    for cpath in candidates:
        try:
            with open(cpath, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        doc_d = _doc_effective_date(doc)
        if doc_d is not None and not _date_within_similarity_window(doc_d, center):
            continue
        old_canon = canonical_article_title((doc.get("title") or "").strip()) or (doc.get("title") or "").strip()
        title_r = _similarity_ratio(canon_title, old_canon)
        old_snip = _content_snippet_for_similarity(doc.get("content") or "")
        body_r = _similarity_ratio(new_snip, old_snip)
        if title_r < SIMILAR_TITLE_RATIO_MIN or body_r < SIMILAR_CONTENT_RATIO_MIN:
            continue
        combined = min(title_r, body_r)
        if combined > best_score:
            best_score = combined
            best_path = cpath
    if not best_path:
        return None
    try:
        with open(best_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    payload = _apply_keyword_context(
        _payload_for_keyword(canon_title, date_raw, content, search_keyword),
        search_keyword,
        keyword_contexts,
    )
    merged_new_url = _append_source_url(doc, site_name, url)
    _merge_article_meta(doc, payload)
    doc["title"] = canon_title
    doc["date"] = date_raw
    if (content or "").strip():
        doc["content"] = content
    with open(best_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    action = "유사 기사 병합(±%d일·제목·본문)" % SIMILAR_ARTICLE_WINDOW_DAYS
    if merged_new_url:
        print(f"[{site_name}] {action}: {canon_title[:52]!r} → {os.path.basename(best_path)}")
    else:
        print(f"[{site_name}] {action}(동일 URL·메타만 갱신): {canon_title[:52]!r} → {os.path.basename(best_path)}")
    return best_path


def _sources_list_from_doc(doc: dict) -> list[dict]:
    src = doc.get("sources")
    if isinstance(src, list) and src:
        return [dict(x) for x in src if isinstance(x, dict)]
    out: list[dict] = []
    u = doc.get("url")
    if u:
        out.append({"source": str(doc.get("source") or ""), "url": str(u)})
    return out


def _append_source_url(doc: dict, site_name: str, url: str) -> bool:
    sources = _sources_list_from_doc(doc)
    seen = {s.get("url") for s in sources}
    if url in seen:
        return False
    sources.append({"source": site_name, "url": url})
    doc["sources"] = sources
    doc.pop("url", None)
    doc.pop("source", None)
    return True


def _dedupe_string_list(values: object) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, list):
        return out
    for value in values:
        s = str(value or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _merge_matched_context_fields(doc: dict, payload: dict) -> None:
    """Merge v2 crawl keyword provenance into an existing article document."""
    flat = set(_dedupe_string_list(doc.get("matched_search_keywords")))
    flat.update(_dedupe_string_list(payload.get("matched_search_keywords")))
    skw = (payload.get("search_keyword") or "").strip()
    if skw:
        flat.add(skw)
    if flat:
        doc["matched_search_keywords"] = sorted(flat)

    merged_by_jw: dict[str, set[str]] = {}
    for source in (doc.get("matched_jw_search_contexts"), payload.get("matched_jw_search_contexts")):
        if not isinstance(source, list):
            continue
        for ctx in source:
            if not isinstance(ctx, dict):
                continue
            jw = str(ctx.get("jw_brand") or "").strip()
            if not jw:
                continue
            merged_by_jw.setdefault(jw, set()).update(_dedupe_string_list(ctx.get("matched_keywords")))
    if merged_by_jw:
        doc["matched_jw_search_contexts"] = [
            {"jw_brand": jw, "matched_keywords": sorted(kws)}
            for jw, kws in sorted(merged_by_jw.items())
        ]


def _payload_for_keyword(title: str, date_raw: str, content: str, search_keyword: str | None) -> dict:
    payload = {
        "title": title,
        "date": date_raw,
        "content": content,
        "search_keyword": search_keyword,
    }
    if search_keyword:
        payload["matched_search_keywords"] = [search_keyword]
    return payload


def _apply_keyword_context(payload: dict, search_keyword: str | None, keyword_contexts: dict[str, list[dict]] | None) -> dict:
    if not search_keyword:
        return payload
    contexts = keyword_contexts.get(search_keyword, []) if keyword_contexts else []
    if not contexts:
        contexts = [{"jw_brand": "", "matched_keywords": [search_keyword]}]
    payload["matched_search_keywords"] = sorted(
        set(_dedupe_string_list(payload.get("matched_search_keywords"))) | {search_keyword}
    )
    payload["matched_jw_search_contexts"] = [
        {"jw_brand": str(ctx.get("jw_brand") or "").strip(), "matched_keywords": _dedupe_string_list(ctx.get("matched_keywords"))}
        for ctx in contexts
        if str(ctx.get("jw_brand") or "").strip()
    ]
    return payload


def _find_json_paths_same_canonical_title(output_dir: str, canon: str) -> list[str]:
    """출력 디렉터리에서 정규화 제목이 canon 과 같은 JSON 경로(중복 없이)."""
    if not canon:
        return []
    out: list[str] = []
    try:
        names = os.listdir(output_dir)
    except OSError:
        return []
    for name in names:
        if not name.endswith(".json"):
            continue
        if name.startswith("_title_mismatch__"):
            continue
        p = os.path.join(output_dir, name)
        try:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        t = (doc.get("title") or "").strip()
        if canonical_article_title(t) == canon:
            out.append(p)
    return sorted(set(out))


def _max_date_from_date_strings(*parts: str) -> date | None:
    best: date | None = None
    for raw in parts:
        dt = _parse_date((raw or "").strip())
        if dt is None:
            continue
        d = dt.date()
        if best is None or d > best:
            best = d
    return best


def _consolidate_same_canonical_title_cluster(
    output_dir: str,
    site_name: str,
    url: str,
    canon: str,
    date_raw: str,
    payload: dict,
    existing_paths: list[str],
) -> str | None:
    """
    동일 정규화 제목의 기존 JSON들과 이번 수집을 한 파일로 합친다.
    본문은 이번 payload에 내용이 있으면 그것을(최신 크롤), 없으면 기존 중 가장 긴 본문.
    게재일 필드는 파싱 가능한 날짜 중 가장 늦은 날짜로 맞춘다.
    """
    loaded: list[tuple[str, dict]] = []
    for p in existing_paths:
        try:
            with open(p, encoding="utf-8") as f:
                loaded.append((p, json.load(f)))
        except (OSError, json.JSONDecodeError):
            continue
    if not loaded:
        return None

    docs_only = [d for _, d in loaded]
    date_parts = [date_raw] + [(d.get("date") or "").strip() for d in docs_only]
    max_d = _max_date_from_date_strings(*date_parts)
    if max_d is not None:
        merged_date_field = max_d.isoformat()
    else:
        merged_date_field = (date_raw or "").strip() or next(
            ((d.get("date") or "").strip() for d in docs_only if (d.get("date") or "").strip()),
            "",
        )

    pin = (payload.get("content") or "").strip()
    if pin:
        body = payload["content"]
    else:
        best_c = ""
        for d in docs_only:
            c = (d.get("content") or "").strip()
            if len(c) > len(best_c):
                best_c = c
        body = best_c

    skw = (payload.get("search_keyword") or "").strip()
    if not skw:
        for d in docs_only:
            v = (d.get("search_keyword") or "").strip()
            if v:
                skw = v
                break

    merged_doc: dict[str, Any] = {
        "title": canon,
        "date": merged_date_field,
        "content": body,
        "sources": [],
    }
    if skw:
        merged_doc["search_keyword"] = skw
    for d in docs_only:
        _merge_matched_context_fields(merged_doc, d)
    _merge_matched_context_fields(merged_doc, payload)

    for d in docs_only:
        for s in _sources_list_from_doc(d):
            u = (s.get("url") or "").strip()
            if not u:
                continue
            _append_source_url(merged_doc, (s.get("source") or "").strip() or "?", u)
    _append_source_url(merged_doc, site_name, url)

    target_fname = article_json_filename(canon, merged_date_field, None)
    target_path = os.path.join(output_dir, target_fname)
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(merged_doc, f, ensure_ascii=False, indent=2)

    for p in existing_paths:
        rp = os.path.normpath(os.path.abspath(p))
        rt = os.path.normpath(os.path.abspath(target_path))
        if rp != rt:
            try:
                os.remove(p)
            except OSError:
                pass

    print(
        f"[{site_name}] 동일 정규화 제목 통합·본문 최신: "
        f"{canon[:52]!r} → {os.path.basename(target_path)}"
    )

    return target_path


def _merge_article_meta(doc: dict, payload: dict) -> None:
    doc.pop("search_keywords", None)
    if payload.get("search_keyword") and not doc.get("search_keyword"):
        doc["search_keyword"] = payload["search_keyword"]
    _merge_matched_context_fields(doc, payload)
    if (payload.get("content") or "").strip():
        doc["content"] = payload["content"]


def save_article_json(
    site_name: str,
    url: str,
    payload: dict,
    output_dir: str | None = None,
    *,
    skip_similar_merge: bool = False,
    unique_json_per_url: bool = False,
) -> str:
    out = output_dir if output_dir is not None else DATASET_DIR
    os.makedirs(out, exist_ok=True)
    title_raw = (payload.get("title") or "").strip() or "제목 없음"
    canon = canonical_article_title(title_raw) or title_raw
    date_raw = (payload.get("date") or "").strip()
    fname = article_json_filename(canon, date_raw, url if unique_json_per_url else None)
    path = os.path.join(out, fname)
    with _article_save_lock:
        if not unique_json_per_url:
            same_paths = _find_json_paths_same_canonical_title(out, canon)
            if same_paths:
                got = _consolidate_same_canonical_title_cluster(
                    out, site_name, url, canon, date_raw, payload, same_paths
                )
                if got:
                    return got
        if unique_json_per_url:
            doc = {
                "title": canon,
                "date": date_raw,
                "content": payload.get("content") or "",
                "sources": [{"source": site_name, "url": url}],
            }
            if payload.get("search_keyword"):
                doc["search_keyword"] = payload["search_keyword"]
            _merge_matched_context_fields(doc, payload)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            return path
        if not os.path.isfile(path):
            if not skip_similar_merge:
                merged = _attempt_similar_article_merge(
                    out,
                    site_name,
                    url,
                    canon_title=canon,
                    date_raw=date_raw,
                    content=payload.get("content") or "",
                    search_keyword=(payload.get("search_keyword") or None),
                    keyword_contexts={
                        ctx["matched_keywords"][0]: [ctx]
                        for ctx in payload.get("matched_jw_search_contexts", [])
                        if ctx.get("matched_keywords")
                    },
                )
                if merged:
                    return merged
            doc = {
                "title": canon,
                "date": date_raw,
                "content": payload.get("content") or "",
                "sources": [{"source": site_name, "url": url}],
            }
            if payload.get("search_keyword"):
                doc["search_keyword"] = payload["search_keyword"]
            _merge_matched_context_fields(doc, payload)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            return path
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        _append_source_url(doc, site_name, url)
        _merge_article_meta(doc, payload)
        doc.pop("search_keywords", None)
        doc["title"] = canon
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        return path


def try_merge_article_without_llm(
    output_dir: str,
    site_name: str,
    url: str,
    extracted: dict,
    expanded_kw_list: list[str],
    search_kw: str | None,
    *,
    skip_similar_merge: bool = False,
    unique_json_per_url: bool = False,
    keyword_contexts: dict[str, list[dict]] | None = None,
) -> bool:
    if unique_json_per_url:
        return False
    title_raw = (extracted.get("title") or "").strip() or "제목 없음"
    canon = canonical_article_title(title_raw) or title_raw
    date_raw = (extracted.get("date") or "").strip()
    with _article_save_lock:
        same_paths = _find_json_paths_same_canonical_title(output_dir, canon)
        if same_paths:
            pl = _apply_keyword_context(
                _payload_for_keyword(
                    extracted.get("title") or title_raw,
                    date_raw,
                    extracted.get("content") or "",
                    search_kw,
                ),
                search_kw,
                keyword_contexts,
            )
            got = _consolidate_same_canonical_title_cluster(
                output_dir, site_name, url, canon, date_raw, pl, same_paths
            )
            if got:
                return True
            return False
        path = os.path.join(output_dir, article_json_filename(canon, date_raw))
        if not os.path.isfile(path):
            if not skip_similar_merge and _attempt_similar_article_merge(
                output_dir,
                site_name,
                url,
                canon_title=canon,
                date_raw=date_raw,
                content=extracted.get("content") or "",
                search_keyword=search_kw,
                keyword_contexts=keyword_contexts,
            ):
                return True
            return False
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if canonical_article_title(doc.get("title", "")) != canon:
            return False
        stub = {
            "search_keyword": search_kw,
            "content": extracted.get("content") or "",
        }
        _apply_keyword_context(stub, search_kw, keyword_contexts)
        merged = _append_source_url(doc, site_name, url)
        _merge_article_meta(doc, stub)
        doc["title"] = canon
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        if merged:
            print(f"[{site_name}] 동일 제목·날짜 → 출처 URL 병합(중요도 재호출 생략): {canon[:56]!r}")
        return True


def merge_keyword_context_for_existing_url(
    output_dir: str,
    site_name: str,
    url: str,
    search_kw: str | None,
    *,
    keyword_contexts: dict[str, list[dict]] | None = None,
) -> bool:
    """Merge search provenance when URL history skips an already-saved article."""
    if not search_kw:
        return False
    try:
        names = os.listdir(output_dir)
    except OSError:
        return False
    stub = _apply_keyword_context({"search_keyword": search_kw}, search_kw, keyword_contexts)
    for name in names:
        if not name.endswith(".json") or name.startswith("_title_mismatch__"):
            continue
        path = os.path.join(output_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        source_urls = {(source.get("url") or "").strip() for source in _sources_list_from_doc(doc)}
        if url not in source_urls and (doc.get("url") or "").strip() != url:
            continue
        _append_source_url(doc, site_name, url)
        _merge_article_meta(doc, stub)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        return True
    return False


def crawl_once(
    months: int | None,
    days: int | None,
    output_dir: str,
    max_pages_per_site: int,
    max_links_per_page: int,
    delay_sec: float,
    sites: list[str] | None = None,
    keywords: list[str] | None = None,
    history_file: str | None = None,
    continue_listing_after_old_page: bool = False,
    skip_similar_merge: bool = False,
    unique_json_per_url: bool = False,
    keyword_contexts: dict[str, list[dict]] | None = None,
    max_articles: int | None = None,
) -> int:
    kw_list = [k.strip() for k in (keywords or []) if k and str(k).strip()]
    if not kw_list:
        kw_list = ["리바로"]

    if days is not None:
        cutoff = datetime.now() - timedelta(days=int(days))
    else:
        cutoff = _cutoff_dt(months) if months is not None else None
    os.makedirs(output_dir, exist_ok=True)

    hf = history_file if history_file is not None else HISTORY_FILE
    scraped_urls = load_scraped_urls(history_file=hf)
    saved_urls: set[str] = set(scraped_urls)
    total_saved = 0

    sites_set = set(sites) if sites else None

    expanded_kw_list = list(kw_list)
    drug_profile: dict | None = None
    print(f"[키워드] 전 사이트 검색·매칭 키워드 {len(expanded_kw_list)}개")

    if unique_json_per_url:
        skip_similar_merge = True
        print('[저장] URL당 고유 JSON — 제목·날짜가 같아도 동일 제목 병합·유사 병합 없음')
    elif skip_similar_merge:
        print('[유사 병합] 비활성화 — 제목·본문 유사도 기반 기사 병합 안 함')

    for site_name, config in SITE_CONFIGS.items():
        if max_articles is not None and total_saved >= max_articles:
            break
        if sites_set is not None and site_name not in sites_set:
            continue
        listing_kw_list = listing_keywords_for_search(site_name, kw_list, drug_profile)
        cutoff_msg = cutoff.date().isoformat() if cutoff else "없음(전체)"
        print(f"\n[{site_name}] 시작 (cutoff={cutoff_msg}, listing={listing_kw_list})")

        per_site_saved = 0
        seen_on_site: set[str] = set()
        site_kw_list = listing_kw_list

        for kw in site_kw_list:
            if max_articles is not None and total_saved >= max_articles:
                break
            consecutive_empty_pages = 0
            consecutive_no_new_list_pages = 0
            prev_page_urls: frozenset[str] | None = None
            print(f"[{site_name}] 검색어: {kw!r}")

            for config_page, page_no in iter_site_paginated_configs(
                site_name, kw, max_pages=max_pages_per_site
            ):
                if max_articles is not None and total_saved >= max_articles:
                    break
                links = get_article_links(site_name, config_page, max_links=max_links_per_page)
                if not links:
                    consecutive_no_new_list_pages = 0
                    consecutive_empty_pages += 1
                    print(
                        f"[{site_name}] kw={kw!r} page={page_no}, 링크 없음 → "
                        f"{'다음 페이지 시도' if consecutive_empty_pages < 3 else '연속 빈 페이지로 중단'} "
                        f"({consecutive_empty_pages}/3)"
                    )
                    if consecutive_empty_pages >= 3:
                        break
                    continue
                consecutive_empty_pages = 0

                page_urls = frozenset(row[0] for row in links)
                fresh_links = [row for row in links if row[0] not in saved_urls and row[0] not in seen_on_site]
                if not fresh_links:
                    same_list_as_prev = prev_page_urls is not None and page_urls == prev_page_urls
                    if same_list_as_prev:
                        consecutive_no_new_list_pages += 1
                        print(
                            f"[{site_name}] kw={kw!r} page={page_no}, 링크={len(links)}건 "
                            f"신규 없음·이전 페이지와 동일 목록(페이지네이션 중복 추정) "
                            f"({consecutive_no_new_list_pages}/2) → "
                            f"{'다음' if consecutive_no_new_list_pages < 2 else '중단'}"
                        )
                        if consecutive_no_new_list_pages >= 2:
                            prev_page_urls = page_urls
                            break
                    else:
                        consecutive_no_new_list_pages = 0
                        print(
                            f"[{site_name}] kw={kw!r} page={page_no}, 링크={len(links)}건 "
                            f"신규 없음(이미 수집) — 목록 구성이 이전과 달라 다음 페이지 진행"
                        )
                    prev_page_urls = page_urls
                    continue
                consecutive_no_new_list_pages = 0

                print(
                    f"[{site_name}] kw={kw!r} page={page_no}, 링크={len(links)}건 "
                    f"(신규 {len(fresh_links)}건)"
                )

                parsed_dates: list[datetime] = []
                page_extracted_count = 0

                for link_row in links:
                    if max_articles is not None and total_saved >= max_articles:
                        break
                    url = link_row[0]
                    row_list_date = ""
                    if len(link_row) > 2 and (
                        site_name == "히트뉴스" or site_name in FIERCE_SITE_NAMES
                    ):
                        row_list_date = (link_row[2] or "").strip()
                    if url in saved_urls or url in seen_on_site:
                        merge_keyword_context_for_existing_url(
                            output_dir,
                            _normalized_source_name(site_name),
                            url,
                            kw,
                            keyword_contexts=keyword_contexts,
                        )
                        continue
                    seen_on_site.add(url)

                    html = fetch_html_requests(url)
                    if not html:
                        continue
                    extracted = extract_news_content(url, html, site_name=site_name)
                    if not extracted:
                        continue

                    saved_urls.add(url)
                    page_extracted_count += 1

                    effective_date = row_list_date or (extracted.get("date") or "").strip()

                    dt = _parse_date(effective_date)
                    if dt is not None:
                        parsed_dates.append(dt)

                    if cutoff is None or _is_within_cutoff(effective_date, cutoff):
                        source_name = _normalized_source_name(site_name)
                        if try_merge_article_without_llm(
                            output_dir,
                            source_name,
                            url,
                            extracted,
                            expanded_kw_list,
                            kw,
                            skip_similar_merge=skip_similar_merge,
                            unique_json_per_url=unique_json_per_url,
                            keyword_contexts=keyword_contexts,
                        ):
                            append_scraped_url(url, history_file=hf)
                            time.sleep(delay_sec)
                            continue
                        payload = _apply_keyword_context(
                            _payload_for_keyword(
                                extracted["title"],
                                effective_date,
                                extracted["content"],
                                kw,
                            ),
                            kw,
                            keyword_contexts,
                        )
                        save_article_json(
                            source_name,
                            url,
                            payload,
                            output_dir=output_dir,
                            skip_similar_merge=skip_similar_merge,
                            unique_json_per_url=unique_json_per_url,
                        )
                        append_scraped_url(url, history_file=hf)
                        per_site_saved += 1
                        total_saved += 1

                    time.sleep(delay_sec)

                prev_page_urls = frozenset(row[0] for row in links)

                if (
                    not continue_listing_after_old_page
                    and cutoff is not None
                    and parsed_dates
                    and len(parsed_dates) == page_extracted_count
                    and max(d.date() for d in parsed_dates) < cutoff.date()
                ):
                    print(f"[{site_name}] kw={kw!r} page={page_no}에서 cutoff 이전 기사만 확인 → 이 검색어 페이지 순회 중단")
                    break

        print(f"[{site_name}] 완료: 저장 {per_site_saved}건")

    return total_saved




def _dedupe_terms_casefold(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        s = (x or "").strip()
        if not s:
            continue
        low = s.casefold()
        if low in seen:
            continue
        seen.add(low)
        out.append(s)
    return out


def keywords_from_profile_excluding_ingredients(profile: dict) -> list[str]:
    out: list[str] = []

    def add(val: object) -> None:
        if val is None:
            return
        if isinstance(val, (list, tuple)):
            for x in val:
                add(x)
            return
        s = str(val).strip()
        if s:
            out.append(s)

    # 성분명은 제외
    add(profile.get("약 한글명"))
    add(profile.get("약 영문명"))
    add(profile.get("질환명"))
    add(profile.get("경쟁사 약 한글명"))
    add(profile.get("경쟁사 약 영문명"))
    return _dedupe_terms_casefold(out)


def _build_keyword_contexts(profiles: list[tuple[str, dict]], *, ingredient_only: bool = False) -> dict[str, list[dict]]:
    contexts: dict[str, list[dict]] = {}
    for drug_name, profile in profiles:
        if ingredient_only:
            kws = ingredient_search_terms_from_profile(profile)
        else:
            kws = keywords_from_profile_excluding_ingredients(profile)
        for kw in kws:
            s = str(kw or "").strip()
            if not s:
                continue
            contexts.setdefault(s, []).append({"jw_brand": drug_name, "matched_keywords": [s]})
    return contexts


def _resolve_output_dir(name_or_path: str) -> str:
    if os.path.isabs(name_or_path) or os.sep in name_or_path:
        return os.path.abspath(name_or_path)
    return os.path.join(BASE_DIR, name_or_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", type=str, default="", help="쉼표로 구분. 비우면 전체")
    ap.add_argument(
        "--stage",
        choices=("all", "news", "ingredient"),
        default="all",
        help="실행할 5년 수집 단계: all=1/2+2/2, news=성분 제외, ingredient=성분만",
    )
    ap.add_argument("--max-pages-per-site", type=int, default=9999)
    ap.add_argument("--max-links-per-page", type=int, default=2000)
    ap.add_argument("--max-articles", type=int, default=0, help="pilot용 최대 저장 기사 수. 0이면 제한 없음")
    ap.add_argument("--delay-sec", type=float, default=5.0)
    ap.add_argument(
        "--months",
        type=int,
        default=60,
        help="크롤링 기간 (개월). 기본 60(5년). 1년치면 12 입력.",
    )
    ap.add_argument(
        "--news-dir-name",
        type=str,
        default="news_5years",
        help="news 단계 출력 폴더명 (BASE_DIR 하위). 기본 news_5years.",
    )
    ap.add_argument(
        "--ingredient-dir-name",
        type=str,
        default="ingredient_5years",
        help="ingredient 단계 출력 폴더명 (BASE_DIR 하위). 기본 ingredient_5years.",
    )
    ap.add_argument(
        "--dailypharm-years-back",
        type=int,
        default=DAILYPHARM_YEARS_BACK_DEFAULT,
        help="데일리팜 목록 URL의 searchStartDate 계산용(기본 5년 전부터, ~오늘).",
    )
    ap.add_argument("--continue-after-old-list-page", action="store_true")
    ap.add_argument("--no-similar-merge", action="store_true")
    ap.add_argument("--unique-json-per-url", action="store_true")
    ap.add_argument(
        "--reverse-time-order",
        action="store_true",
        help="full crawl orchestration marker. Site adapters already request latest-first listings where supported.",
    )
    ap.add_argument(
        "--batch-by-month",
        action="store_true",
        help="accepted for orchestrator compatibility; monthly batch copies are produced by crawl_news_full_orchestrator.py.",
    )
    ap.add_argument(
        "--output-base-dir",
        type=str,
        default="",
        help="accepted for orchestrator compatibility. Use --news-dir-name/--ingredient-dir-name for actual output paths.",
    )
    ap.add_argument("--target-jw-brand", type=str, default="", help="pilot용 특정 JW brand 만 실행")
    ap.add_argument(
        "--drug-profile-dir",
        type=str,
        default=DEFAULT_DRUG_PROFILE_DIR,
        help="약 프로필 JSON 캐시 디렉터리(기본: 프로젝트 drug_profiles)",
    )
    args = ap.parse_args()

    global DAILYPHARM_YEARS_BACK
    DAILYPHARM_YEARS_BACK = int(args.dailypharm_years_back)

    sites = [s.strip() for s in args.sites.split(",") if s.strip()] if args.sites else None
    drug_dir = os.path.abspath(args.drug_profile_dir)

    profiles = _list_cached_drug_profiles(drug_dir)
    if not profiles:
        raise RuntimeError(f"drug_profiles에서 프로필을 찾지 못했습니다: {drug_dir!r}")
    if args.target_jw_brand:
        targets = {x.strip() for x in args.target_jw_brand.split(",") if x.strip()}
        profiles = [(name, prof) for name, prof in profiles if name in targets]
        if not profiles:
            raise RuntimeError(f"--target-jw-brand 에 해당하는 프로필을 찾지 못했습니다: {args.target_jw_brand!r}")

    months_5y = int(args.months)
    skip_similar_merge = bool(args.no_similar_merge)
    unique_json_per_url = bool(args.unique_json_per_url)
    if unique_json_per_url:
        skip_similar_merge = True

    if args.stage in ("all", "news"):
        search_terms: list[str] = []
        for _drug_name, prof in profiles:
            search_terms.extend(keywords_from_profile_excluding_ingredients(prof))
        search_terms = _dedupe_terms_casefold(search_terms)

        news_dir = _resolve_output_dir(args.news_dir_name)
        os.makedirs(news_dir, exist_ok=True)
        news_history = os.path.join(news_dir, "scraped_urls.txt")
        news_keyword_contexts = _build_keyword_contexts(profiles, ingredient_only=False)

        print(f"[1/2] {args.news_dir_name} (성분 제외) 키워드 {len(search_terms)}개 "
              f"| {months_5y}개월 → {news_dir}")
        t0 = time.time()
        saved_news = crawl_once(
            months=months_5y,
            days=None,
            output_dir=news_dir,
            max_pages_per_site=args.max_pages_per_site,
            max_links_per_page=args.max_links_per_page,
            delay_sec=args.delay_sec,
            sites=sites,
            keywords=search_terms,
            history_file=news_history,
            continue_listing_after_old_page=bool(args.continue_after_old_list_page),
            skip_similar_merge=skip_similar_merge,
            unique_json_per_url=unique_json_per_url,
            keyword_contexts=news_keyword_contexts,
            max_articles=(int(args.max_articles) or None),
        )
        news_elapsed = time.time() - t0
        news_report = {
            "mode": "news_5years_profile_keywords_excluding_ingredients",
            "profile_count": len(profiles),
            "keyword_count": len(search_terms),
            "total_saved_articles": int(saved_news),
            "elapsed_seconds": float(news_elapsed),
            "elapsed_hms": _format_elapsed(news_elapsed),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(os.path.join(news_dir, "crawl_report.json"), "w", encoding="utf-8") as f:
            json.dump(news_report, f, ensure_ascii=False, indent=2)
        print(f"=== 완료(1/2): 저장 {saved_news}건, 소요 {news_report['elapsed_hms']} ===")

    if args.stage in ("all", "ingredient"):
        ing_terms: list[str] = []
        for _drug_name, prof in profiles:
            ing_terms.extend(ingredient_search_terms_from_profile(prof))
        ing_terms = _dedupe_terms_casefold(ing_terms)

        ing_dir = _resolve_output_dir(args.ingredient_dir_name)
        os.makedirs(ing_dir, exist_ok=True)
        ing_history = os.path.join(ing_dir, "scraped_urls.txt")
        ingredient_keyword_contexts = _build_keyword_contexts(profiles, ingredient_only=True)

        print(f"[2/2] {args.ingredient_dir_name} (성분만) 키워드 {len(ing_terms)}개 "
              f"| {months_5y}개월 → {ing_dir}")
        t1 = time.time()
        saved_ing = crawl_once(
            months=months_5y,
            days=None,
            output_dir=ing_dir,
            max_pages_per_site=args.max_pages_per_site,
            max_links_per_page=args.max_links_per_page,
            delay_sec=args.delay_sec,
            sites=sites,
            keywords=ing_terms,
            history_file=ing_history,
            continue_listing_after_old_page=bool(args.continue_after_old_list_page),
            skip_similar_merge=skip_similar_merge,
            unique_json_per_url=unique_json_per_url,
            keyword_contexts=ingredient_keyword_contexts,
            max_articles=(int(args.max_articles) or None),
        )
        ing_elapsed = time.time() - t1
        ing_report = {
            "mode": "ingredient_5years_ingredient_terms_only",
            "profile_count": len(profiles),
            "keyword_count": len(ing_terms),
            "total_saved_articles": int(saved_ing),
            "elapsed_seconds": float(ing_elapsed),
            "elapsed_hms": _format_elapsed(ing_elapsed),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(os.path.join(ing_dir, "crawl_report.json"), "w", encoding="utf-8") as f:
            json.dump(ing_report, f, ensure_ascii=False, indent=2)
        print(f"=== 완료(2/2): 저장 {saved_ing}건, 소요 {ing_report['elapsed_hms']} ===")


if __name__ == "__main__":
    main()
