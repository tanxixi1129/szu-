import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app
import crawler


PUBLIC_RESOLVER = lambda *args, **kwargs: [
    (2, 1, 6, "", ("93.184.216.34", 0))
]


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None, json_data=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self._json_data = json_data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def iter_bytes(self):
        yield self.content

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class RobotsClient:
    def stream(self, method, url, headers=None):
        if url.endswith("/robots.txt"):
            return FakeResponse(200, b"User-agent: *\nDisallow: /private\n", {"content-type": "text/plain"})
        return FakeResponse(200, b"public page", {"content-type": "text/plain"})

    def close(self):
        pass


class BraveHttpClient:
    def __init__(self):
        self.kwargs = None

    def get(self, url, **kwargs):
        self.kwargs = kwargs
        return FakeResponse(
            200,
            headers={"content-type": "application/json"},
            json_data={"web": {"results": [{"url": "https://example.com/a", "title": "A", "description": "B"}]}},
        )

    def close(self):
        pass


class FakeSearch:
    def search(self, query, country, search_lang):
        return [
            {"url": "https://example.com/article", "title": "可信资料", "snippet": "可信摘要"},
            {"url": "https://outside.example/news", "title": "外部候选", "snippet": "等待审核"},
        ]

    def close(self):
        pass


class FakeFetcher:
    def __init__(self, version=1):
        self.version = version

    def fetch(self, url, allowed_domains, **kwargs):
        if url.endswith("/sitemap.xml"):
            raise crawler.FetchError("测试站点没有 sitemap")
        text = (
            f"<html><head><title>可信研究报告</title></head><body><article>"
            f"版本 {self.version}。北辰项目预算为一百二十万元，计划十一月交付。"
            + "这是用于验证互联网资料自动采集、正文清洗、内容去重和知识检索的公开研究资料。" * 8
            + "</article></body></html>"
        )
        return crawler.FetchResult(
            url=url,
            status_code=200,
            content_type="text/html",
            content=text.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8", "etag": f'"v{self.version}"'},
        )

    def close(self):
        pass


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.original_connect = app.connect_db
        self.original_data_dir = app.DATA_DIR
        self.original_remote_enabled = app.REMOTE_ENABLED
        self.original_delay = crawler.HOST_DELAY
        app.connect_db = lambda: self.connection
        app.DATA_DIR = Path(".")
        app.REMOTE_ENABLED = False
        crawler.HOST_DELAY = 0
        crawler.init_crawler_db()

    def tearDown(self):
        app.connect_db = self.original_connect
        app.DATA_DIR = self.original_data_dir
        app.REMOTE_ENABLED = self.original_remote_enabled
        crawler.HOST_DELAY = self.original_delay
        self.connection.close()

    def create_source(self):
        return crawler.create_source(
            {
                "name": "行业研究",
                "keywords": ["北辰项目"],
                "allowed_domains": ["example.com"],
                "feed_urls": [],
                "country": "CN",
                "search_lang": "zh-hans",
                "max_pages": 20,
                "enabled": True,
            }
        )

    def test_url_normalization_and_domain_allowlist(self):
        normalized = crawler.normalize_url("HTTPS://Example.COM/a//b?utm_source=x&z=2&a=1#part")
        self.assertEqual(normalized, "https://example.com/a/b?a=1&z=2")
        self.assertTrue(crawler.domain_allowed("news.example.com", ["example.com"]))
        self.assertFalse(crawler.domain_allowed("example.com.evil.test", ["example.com"]))
        with self.assertRaisesRegex(ValueError, "80/443"):
            crawler.normalize_url("https://example.com:8443/private")

    def test_private_network_is_blocked(self):
        private_resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 0))]
        with self.assertRaisesRegex(ValueError, "非公网"):
            crawler.validate_public_url("http://internal.example/", private_resolver)
        self.assertEqual(
            crawler.validate_public_url("https://example.com/a", PUBLIC_RESOLVER),
            "https://example.com/a",
        )

    def test_robots_rules_are_enforced(self):
        fetcher = crawler.SafeFetcher(client=RobotsClient(), resolver=PUBLIC_RESOLVER)
        with self.assertRaisesRegex(crawler.FetchError, "robots.txt"):
            fetcher.fetch("https://example.com/private", ["example.com"])

    def test_brave_search_adapter(self):
        client = BraveHttpClient()
        search = crawler.BraveSearchClient("secret", client=client)
        results = search.search("RAG", "CN", "zh-hans")
        self.assertEqual(results[0]["url"], "https://example.com/a")
        self.assertEqual(client.kwargs["headers"]["X-Subscription-Token"], "secret")
        self.assertEqual(client.kwargs["params"]["freshness"], "pw")

    def test_source_crud_and_candidate_review(self):
        source = self.create_source()
        updated = crawler.update_source(source["id"], {"enabled": False, "max_pages": 10})
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["max_pages"], 10)

        crawler.upsert_candidate(source["id"], "https://other.example/report", "报告", "摘要")
        candidate = crawler.list_candidates()[0]
        decision = crawler.decide_candidate(candidate["id"], "approve_url")
        self.assertEqual(decision["action"], "approve_url")
        page = crawler.page_task(decision["page_id"])
        self.assertTrue(page["approved_once"])

    def test_incremental_ingestion_and_version_history(self):
        source = self.create_source()
        first = crawler.run_sync(source["id"], search_client=FakeSearch(), fetcher=FakeFetcher(1))
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["added"], 1)
        self.assertEqual(first["pending_review"], 1)

        documents = app.list_documents()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["source_type"], "web")
        document_id = documents[0]["id"]
        results = app.search_knowledge("北辰项目预算", 3)
        self.assertEqual(results[0]["source_url"], "https://example.com/article")

        second = crawler.run_sync(source["id"], search_client=FakeSearch(), fetcher=FakeFetcher(1))
        self.assertEqual(second["unchanged"], 1)
        with app.connect_db() as connection:
            version_count = connection.execute("SELECT COUNT(*) FROM web_versions").fetchone()[0]
        self.assertEqual(version_count, 1)

        third = crawler.run_sync(source["id"], search_client=FakeSearch(), fetcher=FakeFetcher(2))
        self.assertEqual(third["updated"], 1)
        self.assertEqual(app.list_documents()[0]["id"], document_id)
        with app.connect_db() as connection:
            versions = connection.execute("SELECT COUNT(*) FROM web_versions").fetchone()[0]
            current_chunks = connection.execute(
                "SELECT GROUP_CONCAT(content, ' ') FROM chunks WHERE document_id = ?", (document_id,)
            ).fetchone()[0]
        self.assertEqual(versions, 2)
        self.assertIn("版本 2", current_chunks)
        self.assertNotIn("版本 1", current_chunks)

        self.assertTrue(app.delete_document(document_id))
        rebuilt = crawler.run_sync(source["id"], search_client=FakeSearch(), fetcher=FakeFetcher(2))
        self.assertEqual(rebuilt["added"], 1)
        self.assertEqual(len(app.list_documents()), 1)

    def test_feed_parser(self):
        xml = b'''<urlset><url><loc>https://example.com/a</loc></url>
        <url><loc>https://example.com/b?utm_source=feed</loc></url></urlset>'''
        self.assertEqual(
            crawler.parse_feed_urls(xml, "https://example.com/sitemap.xml"),
            ["https://example.com/a", "https://example.com/b"],
        )

    def test_version_retention_keeps_latest_twenty(self):
        source = self.create_source()
        page_id, _ = crawler.ensure_page(source["id"], "https://example.com/archive")
        start = datetime.now(timezone.utc) - timedelta(days=21)
        with app.connect_db() as connection:
            for index in range(22):
                content_hash = f"hash-{index}"
                connection.execute(
                    """
                    INSERT INTO web_versions(page_id, content_hash, title, content, content_type, fetched_at)
                    VALUES (?, ?, 'title', 'content', 'text/html', ?)
                    """,
                    (page_id, content_hash, (start + timedelta(days=index)).isoformat()),
                )
            connection.execute("UPDATE web_pages SET content_hash = 'hash-21' WHERE id = ?", (page_id,))
        self.assertEqual(crawler.prune_versions(), 2)
        self.assertEqual(len(crawler.get_page_versions(page_id)["versions"]), 20)

    @patch("crawler.subprocess.run")
    def test_windows_task_definition(self, run_mock):
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = ""
        crawler.install_windows_task("18:00")
        command = run_mock.call_args.args[0][-1]
        self.assertIn("StartWhenAvailable", command)
        self.assertIn("MultipleInstances IgnoreNew", command)


if __name__ == "__main__":
    unittest.main()
