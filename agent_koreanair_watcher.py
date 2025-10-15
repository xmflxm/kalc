#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Korean Air Agent bulletin watcher (빠른 클릭 수집 + 상세 제목/등록일 추출)
- 시작 URL: 최신순 정렬 페이지
- 목록 카드를 실제 클릭해 상세 URL/제목/등록일 수집(SPA에서 <a href> 없음 대응)
- 최초 1회 스냅샷(10건), 이후 새 글만 텔레그램 알림
- 실행시간 단축: 후보 범위 축소, 블랙리스트, 짧은 타임아웃, URL-change 폴링
"""

import os
import re
import json
import time
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Optional

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── 상태 파일 ────────────────────────────────────────────────────────────────
STATE_FILE = Path(__file__).with_name("seen_posts.json")
BASELINE_FLAG = Path(__file__).with_name(".kal_baseline_done")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 최신순 페이지(검증된 링크)
DEFAULT_START = "https://agent.koreanair.com/service/usage/bulletin?currentPage=1&sortByNewest=true"
START_URL = os.getenv("START_URL", DEFAULT_START)

# 실행 속도/범위 설정
MAX_LIST = int(os.getenv("MAX_LIST", "24"))         # 클릭 후보 최대 개수(권장 24~32)
SNAPSHOT_TOP_N = int(os.getenv("SNAPSHOT_TOP_N", "10"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 상세 URL 패턴
DETAIL_OK = re.compile(r"/service/usage/bulletin/[^/?#]+", re.I)

# ── 유틸 ─────────────────────────────────────────────────────────────────────
class Post:
    def __init__(self, title: str, url: str, date: Optional[str] = None):
        self.title = (title or "").strip()
        self.url = (url or "").strip()
        self.id = self.url if self.url else hashlib.sha1(f"{self.title}|{self.url}".encode()).hexdigest()
        self.date = (date or "").strip() or None

def notify_telegram(text: str):
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not (token and chat_id):
        logging.warning("텔레그램 토큰/챗ID 미설정 → 알림 생략")
        return
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(api, data={"chat_id": chat_id, "text": text})
    if r.status_code != 200:
        logging.warning("텔레그램 전송 실패: %s %s", r.status_code, r.text)

def load_seen() -> Dict[str, Dict]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logging.warning("seen_posts.json 읽기 실패 → 새로 시작")
    return {}

def save_seen(seen: Dict[str, Dict]):
    STATE_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")

def format_posts(posts: List[Post]) -> str:
    lines = []
    for p in posts:
        date = f" ({p.date})" if p.date else ""
        lines.append(f"- {p.title}{date}\n  {p.url}")
    return "\n".join(lines)

def dismiss_banners(page):
    labels = ["동의", "확인", "닫기", "Accept", "Agree", "OK", "확인하기"]
    for txt in labels:
        try:
            btn = page.locator(f'button:has-text("{txt}")')
            if btn.count() > 0:
                btn.first.click(timeout=600)
        except Exception:
            pass

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _to_ymd(s: str) -> Optional[str]:
    if not s:
        return None
    # 2025-10-10 / 2025.10.10 / 2025년 10월 10일 → YYYY-MM-DD
    m = re.search(r"(20\d{2})[.\-년\s]*(\d{1,2})[.\-월\s]*(\d{1,2})", s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # 유효 범위 검증
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"

# ── 상세 페이지: 제목/등록일 정밀 추출 ───────────────────────────────────────
def get_detail_title(page) -> Optional[str]:
    """상세 페이지의 '제목' 필드 우선 추출(사이트 전용 휴리스틱)."""
    bad = re.compile(r"(공지사항\s*상세|상세보기|KEMATE\[Agent\])", re.I)

    def ok(v: str) -> Optional[str]:
        v = _clean(v)
        if len(v) > 3 and not bad.search(v):
            return v
        return None

    # 1) 라벨 기반: '제목' 인접 dd/td
    for sel in [
        "dt:has-text('제목') + dd",
        "th:has-text('제목') + td",
        "dt:has-text('제 목') + dd",
        "th:has-text('제 목') + td",
    ]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                v = ok(loc.inner_text())
                if v:
                    return v
        except Exception:
            pass

    # 2) 카드/본문 대표 헤더
    for sel in [
        "article h3", "article h2", "section h3", "section h2",
        ".view-header h3", ".board-view h3", ".board-view .title",
        ".subject", ".tit", ".title"
    ]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                v = ok(loc.inner_text())
                if v:
                    return v
        except Exception:
            pass

    # 3) 본문 첫 굵은 텍스트 후보
    for sel in ["article strong", "article b", "main strong"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                v = ok(loc.inner_text())
                if v:
                    return v
        except Exception:
            pass

    # 4) og:title / h1,h2 / <title>
    try:
        v = page.locator('meta[property="og:title"]').first.get_attribute("content")
        v = ok(v or "")
        if v:
            return v
    except Exception:
        pass

    for sel in ["h1", "h2", "[role='heading']"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                v = ok(loc.inner_text())
                if v:
                    return v
        except Exception:
            pass

    try:
        v = _clean(page.title())
        v = re.sub(r"\s*[-|–]\s*KAL.*$", "", v)
        v = ok(v)
        if v:
            return v
    except Exception:
        pass

    return None

def get_detail_date(page) -> Optional[str]:
    """상세 페이지의 '등록일/작성일/게시일'을 YYYY-MM-DD로 추출."""
    label_selectors = [
        "dt:has-text('등록일') + dd",
        "th:has-text('등록일') + td",
        "dt:has-text('작성일') + dd",
        "th:has-text('작성일') + td",
        "dt:has-text('게시일') + dd",
        "th:has-text('게시일') + td",
        "dt:has-text('등록 일자') + dd",
        "th:has-text('등록 일자') + td",
    ]
    for sel in label_selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                v = _to_ymd(loc.inner_text())
                if v:
                    return v
        except Exception:
            pass

    # 흔한 클래스
    for sel in [".date", ".reg-date", ".write-date", ".post-date"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                v = _to_ymd(loc.inner_text())
                if v:
                    return v
        except Exception:
            pass

    # <time> 태그
    try:
        loc = page.locator("time").first
        if loc.count() > 0:
            v = _to_ymd(loc.inner_text() or loc.get_attribute("datetime") or "")
            if v:
                return v
    except Exception:
        pass

    # 최후: 본문 전체 스캔
    try:
        return _to_ymd(page.inner_text("body"))
    except Exception:
        return None

# ── 목록 클릭 수집(빠른 버전) ────────────────────────────────────────────────
def collect_posts_by_click(page) -> List[Post]:
    posts: List[Post] = []

    # 네트워크 안정화 + 스크롤로 항목 더 로딩
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    try:
        for _ in range(2):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(250)
    except Exception:
        pass

    # 본문(main) 스코프 우선
    scope_locator = page.locator("main, [role='main'], #__next main, #app main")
    if isinstance(scope_locator, tuple):  # 안전 처리(쉼표 실수 방지)
        scope_locator = scope_locator[0]
    scope = scope_locator if scope_locator.count() > 0 else page.locator("body")

    # 클릭 후보: 카드/리스트 항목 위주 (헤더/푸터/가이드 제외)
    CAND_SELECTOR = (
        "article, li, [role='listitem'], "
        "div[class*='list'], div[class*='item'], "
        "a[role='button'], button"
    )
    elems = scope.locator(CAND_SELECTOR)

    def is_black(text: str, href: str) -> bool:
        t = (text or "").lower()
        h = (href or "").lower()
        bad_words = [
            "약관", "개인정보", "privacy", "terms", "이용약관", "개인정보처리방침",
            "footer", "사이트맵", "도움말", "고객센터", "서비스 이용"
        ]
        bad_href = ["footer_privacy", "footer_terms", "/service/guide/"]
        return any(w in t for w in bad_words) or any(b in h for b in bad_href)

    total = elems.count()
    n = min(total, MAX_LIST)
    logging.info("후보(스코프 제한) %d개 중 %d개 시도", total, n)

    def is_detail(url: str) -> bool:
        return bool(DETAIL_OK.search(url))

    def quick_nav_change(old_url: str, wait_ms: int = 2000) -> bool:
        deadline = time.time() + wait_ms / 1000
        while time.time() < deadline:
            if page.url != old_url:
                return True
            page.wait_for_timeout(100)
        return False

    tried = 0
    for i in range(n):
        el = elems.nth(i)
        try:
            txt = _clean(el.inner_text() or "")
            if len(txt) < 4:
                continue

            href = ""
            try:
                href = el.get_attribute("href") or ""
            except Exception:
                pass

            if is_black(txt, href):
                continue

            # 카드 힌트(키워드)가 전혀 없으면 스킵 → 속도 ↑
            if not any(k in txt for k in ["공지", "안내", "스케줄", "W", "국내선", "국제선"]):
                continue

            tried += 1
            if tried > MAX_LIST:
                break

            # 가시화 & 클릭
            try:
                el.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass

            old = page.url
            try:
                el.click(timeout=1200, no_wait_after=True)
            except Exception:
                try:
                    el.dispatch_event("click")
                except Exception:
                    continue

            if not quick_nav_change(old, wait_ms=2000):
                # 이동 안 했으면 패스
                continue

            cur = page.url
            if is_detail(cur):
                # 상세에서 제목/등록일 정밀 추출
                title = get_detail_title(page) or txt
                date_txt = get_detail_date(page) or ""
                posts.append(Post(title=title, url=cur, date=date_txt))

            # 목록으로 빠르게 복귀
            page.go_back(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

        except Exception as e:
            logging.info("클릭 실패(%d): %s", i, e)
            # 시작 URL로 짧게 복구
            try:
                page.goto(START_URL, wait_until="domcontentloaded", timeout=5000)
            except Exception:
                pass
            continue

    # 중복 제거
    uniq, out = set(), []
    for p in posts:
        if p.id in uniq:
            continue
        uniq.add(p.id)
        out.append(p)
    return out

# ── 메인 ────────────────────────────────────────────────────────────────────
def main():
    seen = load_seen()
    want_snapshot = not BASELINE_FLAG.exists()
    FORCE_SNAPSHOT = os.getenv("FORCE_SNAPSHOT", "0").lower() in {"1", "true", "yes"}
    if FORCE_SNAPSHOT:
        want_snapshot = True

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=UA,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            java_script_enabled=True,
            viewport={"width": 1366, "height": 950},
        )
        # ★ 기본 타임아웃 단축
        context.set_default_navigation_timeout(5000)  # 5s
        context.set_default_timeout(4000)             # 4s

        page = context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded", timeout=20000)
        try:
            dismiss_banners(page)
            page.wait_for_load_state("networkidle", timeout=5000)
        except PWTimeout:
            pass

        posts = collect_posts_by_click(page)
        browser.close()

    if not posts:
        logging.info("게시글을 찾지 못했습니다.")
        return

    # 날짜 기준 정렬(있으면)
    def key_func(p: Post):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", p.date or "")
        if m:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (0, 0, 0)

    posts.sort(key=key_func, reverse=True)

    if want_snapshot:
        topn = posts[:SNAPSHOT_TOP_N]
        text = "KAL Agent 스냅샷 (최신 10건)\n\n" + format_posts(topn)
        notify_telegram(text)
        now = int(time.time())
        for p in posts:
            seen[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": now}
        save_seen(seen)
        BASELINE_FLAG.write_text("done", encoding="utf-8")
        logging.info("스냅샷 전송 및 상태 저장 완료")
        return

    # 이후: 새 글만
    new_posts = [p for p in posts if p.id not in seen]
    if new_posts:
        msg = f"KAL Agent 새 글 알림 ({len(new_posts)}건)\n\n" + format_posts(new_posts)
        notify_telegram(msg)
        now = int(time.time())
        for p in new_posts:
            seen[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": now}
        save_seen(seen)
        logging.info("새 글 %d건 전송/저장 완료", len(new_posts))
    else:
        logging.info("새 글 없음")

if __name__ == "__main__":
    main()
