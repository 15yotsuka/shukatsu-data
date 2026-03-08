#!/usr/bin/env python3
"""
shukatsu-data/collector.py
就活締切収集パイプライン Phase 1
"""

import csv
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Windows コンソールで日本語を出力できるよう UTF-8 に強制変更
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

# ──────────────────────────────────
# 定数
# ──────────────────────────────────

JST = timezone(timedelta(hours=9))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

CSV_PATH = Path("companies.csv")
DATA_DIR = Path("data")
GEMINI_MODEL = "gemini-2.5-flash"
DRY_RUN = "--dry-run" in sys.argv

# ──────────────────────────────────
# ロギング設定
# ──────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────
# ユーティリティ
# ──────────────────────────────────

def now_jst() -> str:
    return datetime.now(JST).isoformat()


# ──────────────────────────────────
# HTTP fetch（指数バックオフ付きリトライ）
# ──────────────────────────────────

def fetch_html(url: str, max_retries: int = 3) -> str | None:
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
            return resp.text
        except requests.RequestException as exc:
            wait_sec = 2 ** attempt
            logger.warning(
                "Fetch attempt %d/%d failed for %s: %s",
                attempt, max_retries, url, exc,
            )
            if attempt < max_retries:
                logger.info("Waiting %ds before retry…", wait_sec)
                time.sleep(wait_sec)
    logger.error("All fetch attempts failed for %s", url)
    return None


def extract_text(html: str, char_limit: int = 8000) -> str:
    """HTML から不要タグを除去してプレーンテキストを取得する"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [ln.strip() for ln in soup.get_text(separator="\n").splitlines() if ln.strip()]
    return "\n".join(lines)[:char_limit]


# ──────────────────────────────────
# Gemini API 呼び出し
# ──────────────────────────────────

PROMPT_TEMPLATE = """\
以下は企業の採用ページから抽出したテキストです。
採用に関する締切日情報を抽出し、以下のJSON形式のみで返してください（説明文・コードブロック不要）。

{{"deadlines": [{{"type": "本選考|サマーインターン|オータムインターン|ウィンターインターン|早期選考|通年採用", "deadline": "YYYY-MM-DD or null", "label": "任意のラベル"}}]}}

ルール:
- type は上記の選択肢から最も適切なものを選ぶこと
- deadline が不明・記載なしの場合は null にすること
- 採用情報がない場合は {{"deadlines": []}} を返すこと

テキスト:
{text}
"""


def call_gemini(model, text: str, max_retries: int = 2) -> list | None:
    prompt = PROMPT_TEMPLATE.format(text=text)
    for attempt in range(1, max_retries + 1):
        try:
            response = model.generate_content(prompt)
            raw = response.text.strip()
            # コードブロックが含まれている場合は除去
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw.strip())
            return parsed.get("deadlines", [])
        except Exception as exc:
            err_str = str(exc).lower()
            logger.warning("Gemini attempt %d/%d failed: %s", attempt, max_retries, exc)
            if any(kw in err_str for kw in ("quota", "rate", "429", "resource exhausted")):
                logger.info("Rate limit detected. Waiting 60s…")
                time.sleep(60)
            elif attempt < max_retries:
                time.sleep(5)
    logger.error("Gemini extraction failed after all retries")
    return None


# ──────────────────────────────────
# JSON 入出力
# ──────────────────────────────────

def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _normalize_for_diff(companies: list[dict]) -> list[dict]:
    """差分比較用: fetched_at / source_url を除外したリストを返す"""
    result = []
    for c in companies:
        entry = {k: v for k, v in c.items() if k != "deadlines"}
        entry["deadlines"] = [
            {k: v for k, v in d.items() if k not in ("fetched_at", "source_url")}
            for d in c.get("deadlines", [])
        ]
        result.append(entry)
    return result


def has_changed(existing: dict | None, new_companies: list[dict]) -> bool:
    if existing is None:
        return True
    return (
        _normalize_for_diff(existing.get("companies", []))
        != _normalize_for_diff(new_companies)
    )


# ──────────────────────────────────
# CSV 読み込み
# ──────────────────────────────────

def read_companies() -> list[dict]:
    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ──────────────────────────────────
# フォールバック企業データ
# ──────────────────────────────────

def make_empty_company(cid: str, cname: str, industry: str) -> dict:
    return {
        "company_id": cid,
        "company_name": cname,
        "industry": industry,
        "deadlines": [],
    }


# ──────────────────────────────────
# メイン処理
# ──────────────────────────────────

def main() -> None:
    if DRY_RUN:
        logger.info("=== DRY RUN MODE ===")

    # Gemini 初期化（ドライランはスキップ）
    model = None
    if not DRY_RUN:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logger.error("GEMINI_API_KEY environment variable is not set")
            sys.exit(1)
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(GEMINI_MODEL)
        logger.info("Gemini model: %s", GEMINI_MODEL)

    # CSV 読み込み
    companies = read_companies()
    logger.info("Loaded %d companies from %s", len(companies), CSV_PATH)

    # 卒業年度でグループ化
    by_year: dict[int, list[dict]] = {}
    for row in companies:
        year = int(row["grad_year"])
        by_year.setdefault(year, []).append(row)

    DATA_DIR.mkdir(exist_ok=True)

    for grad_year, year_companies in sorted(by_year.items()):
        output_path = DATA_DIR / f"deadlines-{grad_year}.json"
        existing = load_json(output_path)

        # 既存データを company_id でインデックス化（フォールバック用）
        existing_map: dict[str, dict] = {
            c["company_id"]: c
            for c in (existing or {}).get("companies", [])
        }

        company_results: list[dict] = []

        for company in year_companies:
            cid = company["company_id"]
            cname = company["company_name"]
            url = company["career_page_url"]
            industry = company["industry"]
            logger.info("Processing: %s (%s)", cname, url)

            # ── ドライラン ──
            if DRY_RUN:
                logger.info("[DRY RUN] Would fetch: %s", url)
                company_results.append(make_empty_company(cid, cname, industry))
                continue

            # ── フェッチ ──
            html = fetch_html(url)
            if html is None:
                logger.warning("Skipping %s (fetch failed)", cname)
                company_results.append(
                    existing_map.get(cid, make_empty_company(cid, cname, industry))
                )
                time.sleep(random.uniform(2, 5))
                continue

            text = extract_text(html)

            # ── Gemini 解析 ──
            deadlines_raw = call_gemini(model, text)
            fetched_at = now_jst()

            if deadlines_raw is None:
                logger.warning("Gemini failed for %s. Keeping previous data.", cname)
                company_results.append(
                    existing_map.get(cid, make_empty_company(cid, cname, industry))
                )
            else:
                deadlines = [
                    {
                        "type": d.get("type", "その他"),
                        "deadline": d.get("deadline"),
                        "label": d.get("label", ""),
                        "source_url": url,
                        "fetched_at": fetched_at,
                    }
                    for d in deadlines_raw
                ]
                company_results.append(
                    {
                        "company_id": cid,
                        "company_name": cname,
                        "industry": industry,
                        "deadlines": deadlines,
                    }
                )

            # リクエスト間隔
            sleep_sec = random.uniform(2, 5)
            logger.info("Sleeping %.1fs…", sleep_sec)
            time.sleep(sleep_sec)

        # ── 差分検知・保存 ──
        if has_changed(existing, company_results):
            output = {
                "version": "1.0",
                "updated_at": now_jst(),
                "grad_year": grad_year,
                "companies": company_results,
            }
            save_json(output_path, output)
            logger.info("Saved %s (%d companies)", output_path, len(company_results))
        else:
            logger.info("No changes for grad_year=%d, skip write", grad_year)

    print("Phase 1完了。data/deadlines-27.jsonの内容を確認してください。")


if __name__ == "__main__":
    main()
