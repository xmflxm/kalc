#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Korean Air Agent bulletin watcher (클릭 네비게이션 방식)
- 최신순 페이지에서 각 항목을 실제 클릭하여 상세 URL을 확보
- 최초 1회: 스냅샷 10건 전송
- 이후: 새 글만 전송
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

# ---- 상태 파일 ----
STATE_FILE = Path(__file__).with_name("seen_posts.json")
BASELINE_FLAG = Path(__file__).with_name(".kal_baseline_done")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 최신순 페이지(사용자가 확인한 URL)
DEFAULT_START = "https://agent.koreanair.com/service/usage/bulletin?currentPage=1&sortByNewest=true"
START_URL = os.getenv("START_URL", DEFAULT_START)

MAX_LIST = int(os.getenv("MAX_LIST", "40"))       # 한 번에 시도할 목록 수(안정성 위해 과하지 않게)
SNAPSHOT_TOP_N = int(os.getenv("SNAPSHOT_TOP_N", "10"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DETAIL_OK = re.compile(r"/service/usage/bulletin/[^/?#]+", re.I)

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
                btn.first.click(timeout=800)
        except Exception:
            pass

def collect_posts_by_click(page) -> List[Post]:
    """
    목록 셀렉터에 의존하지 않고, 화면의 클릭 가능한 요소들을 실제 클릭해
    상세 URL을 확보한다. (SPA에서 <a href>가 없는 경우 대응)
    """
    posts: List[Post] = []

    # 화면이 그려질 시간을 조금 주고, 아래로 스크롤
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    try:
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(400)
    except Exception:
        pass

    # 후보: a, role=link, button, onclick 보유, 제목스팬을 감싼 div 등
    CAND_SELECTOR = (
        "a, [role='link'], button, [onclick], "
        "div:has(> span), li, div[class*='list'], div[class*='item'], article"
    )
    elems = page.locator(CAND_SELECTOR)
    n = min(elems.count(), MAX_LIST)
    logging.info("클릭 후보 %d개 중 %d개 시도", elems.count(), n)

    for i in range(n):
        el = elems.nth(i)
        try:
            txt = (el.inner_text() or "").strip()
            # 너무 짧은 텍스트/아이콘 전용은 건너뛰기
            if not txt or len(txt) < 4:
                continue
            # 제목스러운 키워드가 있으면 가산점 (없어도 시도)
            if any(k in txt for k in ["공지", "안내", "W", "국내선", "스케줄", "REACCM"]):
                pass

            before = page.url
            # 라우팅 예상 → 페이지 전환을 기다림
            with page.expect_navigation(wait_until="domcontentloaded", timeout=8000):
                el.click()

            cur = page.url
            if DETAIL_OK.search(cur):
                # 상세 페이지 제목을 다시 한 번 읽어서 보정(가능할 때)
                try:
                    h = page.locator("h1, h2").first
                    if h.count() > 0:
                        htxt = (h.inner_text() or "").strip()
                        if len(htxt) >= 4:
                            txt = htxt
                except Exception:
                    pass
                posts.append(Post(title=txt, url=cur))
            else:
                logging.info("상세 패턴 불일치: %s", cur)

            # 목록으로 복귀
            page.go_back(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(200)

        except Exception as e:
            logging.info("클릭 실패(%d): %s", i, e)
            # 목록이 사라졌으면 시작 URL로 복구
            try:
                page.goto(START_URL, wait_until="domcontentloaded", timeout=15000)
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

def main():
    seen = load_seen()
    want_snapshot = not BASELINE_FLAG.exists()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent=UA,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            java_script_enabled=True,
            viewport={"width": 1366, "height": 950},
        )
        page = context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)
        try:
            dismiss_banners(page)
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass

        posts = collect_posts_by_click(page)
        browser.close()

    if not posts:
        logging.info("게시글을 찾지 못했습니다.")
        return

    # 최신순 페이지이므로, 날짜 텍스트가 있으면 최신순 정렬 비슷하게 보정
    def key_func(p: Post):
        # YYYY-MM-DD → 정렬핵
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
