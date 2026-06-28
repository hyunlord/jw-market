import json
import logging
import os
import sys
import requests
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

WORKFLOW_ID = "196"
GENOS_URL = "https://jwai-dev.jwhealthcare.com"

CRAWLING_BASE = Path(__file__).parent.parent / "GCP" / "crawling"
CATALOG_PATH = str(CRAWLING_BASE / "drug_catalog/_catalog.json")
MAX_WORKERS = 4

logger = logging.getLogger(__name__)


def load_catalog(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return json.dumps(json.load(f), ensure_ascii=False)


def process_all(session: requests.Session, url: str, catalog: str, news_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    all_files = list(news_dir.glob("*.json"))
    if not all_files:
        logger.warning("파일이 없습니다.")
        return
    total = len(all_files)
    logger.info("총 %d개 파일 처리 시작", total)
    tag_counter = Counter()
    done = 0
    skipped = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_file, session, url, catalog, src, output_dir): src for src in all_files}
        bar = tqdm(as_completed(futures), total=total, desc="파일 처리", unit="파일")
        for future in bar:
            try:
                status, tag = future.result()
            except Exception as e:
                logger.error("예상치 못한 오류: %s", e)
                continue
            if status == "done":
                done += 1
                tag_counter[tag] += 1
            elif status == "skipped":
                skipped += 1
                tag_counter[tag] += 1
            bar.set_postfix(done=done, skipped=skipped, refresh=False)
    logger.info("완료 — 신규 %d건 처리 / %d건 스킵 / 총 %d건", done, skipped, total)
    logger.info("태그 분포 (총 %d건):", done + skipped)
    for tag, count in tag_counter.most_common():
        logger.info("  %-15s %4d개", tag, count)


def process_file(session: requests.Session, url: str, catalog: str, src: Path, output_dir: Path) -> tuple[str, str]:
    dest = output_dir / src.name
    if dest.exists():
        with open(dest, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if "matches" in existing and "summary" in existing:
            return "skipped", existing.get("tag", "unknown")
    with open(src, "r", encoding="utf-8") as f:
        data = json.load(f)
    try:
        body = {
            "question": f"카탈로그:\n{catalog}\n\n제목: {data['title']}\n\n내용: {data['content']}\n\nsearch_keyword: {data['search_keyword']}"
        }
        response = session.post(f"{url}/run/v2", json=body)
        response.raise_for_status()
        result = parse_result(response.json())
    except (requests.RequestException, ValueError) as e:
        logger.error("%s 요청 실패: %s", src.name, e)
        return "error", "기타"
    data["matches"] = result["matches"]
    data["summary"] = result["summary"]
    data["tag"] = result["tag"]
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return "done", data["tag"]


def parse_result(response: dict) -> dict:
    fallback = {"matches": [], "summary": None, "tag": "기타"}
    try:
        if "text" not in response.get("data", {}):
            logger.warning("예상치 못한 응답 구조: %s", response)
            return fallback
        text = response["data"]["text"].strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(text)
        if isinstance(result, dict) and "matches" in result:
            return result
        return {"matches": result, "summary": None, "tag": "관련 없음"}
    except json.JSONDecodeError as e:
        logger.error("파싱 실패: %s", e)
        return fallback
    except Exception as e:
        logger.error("parse_result 오류: %s", e)
        return fallback


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: uv run python score.py <사이트명>")
        print("예시:   uv run python score.py 데일리팜")
        sys.exit(1)
    site_name = sys.argv[1]
    news_dir = CRAWLING_BASE / f"news_5years_{site_name}"
    output_dir = CRAWLING_BASE / f"news_5years_{site_name}_processed"
    if not news_dir.exists():
        print(f"오류: 폴더가 존재하지 않습니다 — {news_dir}")
        sys.exit(1)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger.info("처리 대상: %s", news_dir)
    direct_url = os.environ.get("GENOS_WORKFLOW_URL")
    if direct_url:
        url = direct_url.rstrip("/")
        headers = {}
    else:
        url = f"{GENOS_URL}/api/gateway/workflow/{WORKFLOW_ID}"
        genos_token = os.environ.get("GENOS_TOKEN")
        if not genos_token:
            raise RuntimeError("GENOS_TOKEN is required when GENOS_WORKFLOW_URL is not set")
        headers = {"Authorization": f"Bearer {genos_token}"}
    catalog = load_catalog(CATALOG_PATH)
    with requests.Session() as session:
        session.headers.update(headers)
        res = session.get(f"{url}/healthcheck")
        logger.info("healthcheck: %s", res.json())
        process_all(session, url, catalog, news_dir, output_dir)
