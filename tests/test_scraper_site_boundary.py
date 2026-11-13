"""站点边界单元测试：逐跳核对目标站点、站外跳转零请求。

运行：python3 -m unittest discover -s tests -v
"""
import pathlib
import sys
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "sample_site"))

import server as sample_server  # noqa: E402
from scraper import (  # noqa: E402
    OffSiteRedirectError,
    _get_soup,
    crawl_catalog,
)

import requests  # noqa: E402
from urllib.parse import urlparse  # noqa: E402


class CountingSession:
    """包装 requests.Session，记录每次真正发出的请求 URL。"""

    def __init__(self):
        self.session = requests.Session()
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.session.get(url, **kwargs)


class TestSiteBoundary(unittest.TestCase):
    """用本地两个站点（目录站 + 站外站）验证跳转拦截。"""

    @classmethod
    def setUpClass(cls):
        cls.catalog_log: list[tuple[str, str]] = []
        cls.offsite_log: list[tuple[str, str]] = []
        cls.offsite_srv, cls.offsite_base = sample_server.build_offsite_server(
            request_log=cls.offsite_log
        )
        cls.catalog_srv, cls.catalog_base = sample_server.build_catalog_server(
            request_log=cls.catalog_log, offsite_origin=cls.offsite_base
        )
        cls.offsite_srv.RequestHandlerClass.keywords[
            "redirects"
        ] = sample_server.offsite_redirects(cls.catalog_base)
        cls.catalog_netloc = urlparse(cls.catalog_base).netloc

    @classmethod
    def tearDownClass(cls):
        cls.catalog_srv.shutdown()
        cls.offsite_srv.shutdown()

    def setUp(self):
        self.offsite_log.clear()

    def _offsite_requests(self):
        return [path for host, path in self.offsite_log]

    def test_offsite_redirect_raises_without_requesting_target(self):
        session = CountingSession()
        with self.assertRaises(OffSiteRedirectError) as ctx:
            _get_soup(session, f"{self.catalog_base}/offsite/ghost-7.html", self.catalog_netloc)
        # 只请求了目录站上的原链接；站外目标一次都没请求
        self.assertEqual(len(session.requested), 1)
        self.assertTrue(session.requested[0].endswith("/offsite/ghost-7.html"))
        self.assertEqual(self._offsite_requests(), [])
        self.assertEqual(
            ctx.exception.redirect.target_url, f"{self.offsite_base}/details/ghost-7.html"
        )
        self.assertEqual(
            ctx.exception.redirect.source_url, f"{self.catalog_base}/offsite/ghost-7.html"
        )

    def test_offsite_then_back_to_site_still_blocked(self):
        """站外中转页会跳回本站，但仍必须在第一跳拦截。"""
        session = CountingSession()
        with self.assertRaises(OffSiteRedirectError) as ctx:
            _get_soup(
                session,
                f"{self.catalog_base}/offsite/ghost-7-return.html",
                self.catalog_netloc,
            )
        self.assertEqual(len(session.requested), 1)
        self.assertEqual(self._offsite_requests(), [])
        self.assertEqual(
            ctx.exception.redirect.target_url,
            f"{self.offsite_base}/details/ghost-7-return.html",
        )

    def test_catalog_bounce_offsite_is_unreachable(self):
        session = CountingSession()
        with self.assertRaises(OffSiteRedirectError):
            _get_soup(session, f"{self.catalog_base}/bounce.html", self.catalog_netloc)
        self.assertEqual(len(session.requested), 1)
        self.assertEqual(self._offsite_requests(), [])

    def test_same_site_redirect_is_followed(self):
        session = CountingSession()
        soup = _get_soup(session, f"{self.catalog_base}/go/redir-1.html", self.catalog_netloc)
        # 同站跳转被正常跟随：先请求入口，再请求真实详情页
        self.assertEqual(len(session.requested), 2)
        self.assertEqual(
            session.requested[1], f"{self.catalog_base}/details/redir-1.html"
        )
        self.assertIn("REDIR-1", soup.get_text())

    def test_offsite_host_mismatch_is_blocked_before_request(self):
        session = CountingSession()
        with self.assertRaises(OffSiteRedirectError):
            _get_soup(session, "http://elsewhere.example/x", self.catalog_netloc)
        self.assertEqual(session.requested, [])

    def test_crawl_catalog_offsite_detail_not_priced(self):
        log_start = len(self.offsite_log)
        result = crawl_catalog(f"{self.catalog_base}/ghost-catalog.html", "GHOST-7")
        self.assertEqual(result.matched, [])
        self.assertEqual(len(result.offsite_redirects), 1)
        self.assertTrue(result.failed_details[0].endswith("/offsite/ghost-7.html"))
        # 站外站点在这轮抓取中零请求
        self.assertEqual(self.offsite_log[log_start:], [])

    def test_crawl_catalog_offsite_page_raises(self):
        with self.assertRaises(OffSiteRedirectError):
            crawl_catalog(f"{self.catalog_base}/offsite-catalog.html", "MF-50")


if __name__ == "__main__":
    unittest.main()
