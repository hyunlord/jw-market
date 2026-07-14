import base64
from pathlib import Path

from pipeline.scripts.news_cutover.build_url_dedup_staging_sql import build_components, read_news


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def write_news(path: Path) -> None:
    path.write_text(
        "\t".join(
            [
                "news_id",
                "article_url_b64",
                "title_b64",
                "published_date",
                "search_keyword_b64",
                "source_name_b64",
                "matched_search_keywords_b64",
                "matched_jw_search_contexts_b64",
                "tier",
                "text_len",
                "ingested_at",
                "collected_at",
            ]
        )
        + "\n"
        + "\n".join(
            [
                "\t".join(["a", b64("https://news.test/a?utm_source=x"), b64("same\ttitle\nsafe"), "2026-07-01", b64("kw"), b64("src"), b64('["kw"]'), b64("[]"), "2", "10", "2026-07-01 00:00:00", "2026-07-01 00:00:00"]),
                "\t".join(["b", b64("https://news.test/a"), b64("different"), "2026-07-01", b64("kw"), b64("src"), b64("[]"), b64("[]"), "2", "20", "2026-07-01 00:00:00", "2026-07-01 00:00:00"]),
                "\t".join(["c", b64("https://news.test/c"), b64("same title safe"), "2026-07-01", b64("kw"), b64("src"), b64("[]"), b64("[]"), "2", "30", "2026-07-01 00:00:00", "2026-07-01 00:00:00"]),
            ]
        ),
        encoding="utf-8",
    )


def test_build_components_when_base64_fields_contain_tab_and_newline(tmp_path: Path) -> None:
    # Given: a base64-safe export where decoded title contains tab/newline.
    source = tmp_path / "news.tsv"
    write_news(source)

    # When: components are calculated from URL and exact title/date only.
    rows = read_news(source)
    components = [set(ids) for ids in build_components(rows).values()]

    # Then: URL-normalized duplicates merge, but unsafe decoded text does not shift fields.
    assert {"a", "b"} in components
    assert {"c"} in components
