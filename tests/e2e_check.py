"""端到端检查：本地 HTTP 服务托管样本站，调用 FastAPI 完整链路。

运行：python3 tests/e2e_check.py
"""
import functools
import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "sample_site"))

import server as sample_server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402

SITE_DIR = ROOT / "sample_site"


def start_site_server() -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"

def check(name: str, cond: bool, extra: str = ""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        raise SystemExit(f"检查失败: {name}")


def main():
    # 目录站与站外样例站各起一个端点，用独立的请求日志验证站外零请求
    httpd, base = start_site_server()
    catalog_log: list[tuple[str, str]] = []
    offsite_log: list[tuple[str, str]] = []
    offsite_srv, offsite_base = sample_server.build_offsite_server(request_log=offsite_log)
    catalog_srv, redirect_base = sample_server.build_catalog_server(
        request_log=catalog_log, offsite_origin=offsite_base
    )
    # 目录站端口确定后，回填站外站"跳回本站"的跳转目标
    offsite_srv.RequestHandlerClass.keywords["redirects"] = sample_server.offsite_redirects(
        redirect_base
    )
    client = TestClient(app)
    catalog = f"{base}/index.html"
    print(f"样本站: {catalog}")
    print(f"跳转样例目录站: {redirect_base}（站外站: {offsite_base}）")

    # 1) 正常求解：MF-50 需求 180 → 2 包 100 片装，¥450
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 180})
    assert r.status_code == 200, r.text
    d = r.json()
    check("MF-50/180 状态 ok", d["status"] == "ok")
    check("总价 ¥450.00", d["solution"]["total_price"] == "450.00")
    check("实到 200 片", d["solution"]["total_pieces"] == 200)
    check("超量 20 片", d["solution"]["overage"] == 20)
    items = d["solution"]["items"]
    check("仅一种包装", len(items) == 1)
    check("100片装 ×2", items[0]["pack_size"] == 100 and items[0]["purchased_packs"] == 2)
    check("详情来源 URL", items[0]["detail_url"].endswith("/details/mf50-100.html"))

    # 2) 分页与跨页去重：3 个列表页都被抓取，mf50-50 只计一次
    check("抓取 3 个列表页", len(d["list_pages"]) == 3, str(d["list_pages"]))
    check("跨页重复跳过 1 次", d["duplicate_detail_links_skipped"] == 1)
    check("匹配详情 3 个", len(d["matched_detail_pages"]) == 3, str(d["matched_detail_pages"]))

    # 3) 混购 + 库存约束：需求 175 → 100+50+25 各 1 包，¥413
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 175})
    d = r.json()
    check("MF-50/175 状态 ok", d["status"] == "ok")
    check("混购 3 种包装", len(d["solution"]["items"]) == 3)
    check("总价 ¥413.00", d["solution"]["total_price"] == "413.00")
    check("实到 175 片（零超量）", d["solution"]["total_pieces"] == 175 and d["solution"]["overage"] == 0)

    # 4) 库存上限：需求 1000 → 恰好买光 10 包 100 片装
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 1000})
    d = r.json()
    check("MF-50/1000 状态 ok", d["status"] == "ok")
    check("总价 ¥2250.00", d["solution"]["total_price"] == "2250.00")
    check("实到 1000 片", d["solution"]["total_pieces"] == 1000)

    # 5) 库存不足：需求 4000 > 可购总量 3750 → 不给确定最低价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 4000})
    d = r.json()
    check("MF-50/4000 状态 insufficient_stock", d["status"] == "insufficient_stock")
    check("无方案、无总价", d["solution"] is None)
    check("报告可购总量 3750", d["total_available_pieces"] == 3750)

    # 6) 匹配详情抓取失败：BROKEN-1 的详情页 404 → 不给确定最低价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "BROKEN-1", "demand": 10})
    d = r.json()
    check("BROKEN-1 状态 scrape_failed", d["status"] == "scrape_failed")
    check("无方案", d["solution"] is None)
    check("报告失败详情页", d["failed_detail_pages"][0].endswith("/details/broken-404.html"))

    # 7) 无匹配型号（兜底扫描时 broken-404 无法核对，应在响应中如实标注）
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "NOPE-9", "demand": 10})
    d = r.json()
    check("NOPE-9 状态 no_matching_products", d["status"] == "no_matching_products")
    check("无方案", d["solution"] is None)
    check(
        "标注未能核对的页面",
        any(u.endswith("/details/broken-404.html") for u in d.get("unverified_failed_pages", [])),
    )

    # 8) 其他型号正常匹配：NY-66 需求 150 → 2 包 100 片装 ¥360
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "NY-66", "demand": 150})
    d = r.json()
    check("NY-66/150 状态 ok", d["status"] == "ok")
    check("总价 ¥360.00", d["solution"]["total_price"] == "360.00")

    # 9) 目录不可达
    r = client.post("/api/v1/quote", json={"catalog_url": f"{base}/no-such-page.html", "model": "MF-50", "demand": 10})
    d = r.json()
    check("目录不存在状态 catalog_unreachable", d["status"] == "catalog_unreachable")

    # ===== 站点边界：离线跳转样例 =====
    # 11) 匹配详情跳到站外：返回原链接 + 跳转目标地址，不给确定报价
    r = client.post(
        "/api/v1/quote",
        json={"catalog_url": f"{redirect_base}/ghost-catalog.html", "model": "GHOST-7", "demand": 10},
    )
    d = r.json()
    check("详情越界状态 scrape_failed", d["status"] == "scrape_failed")
    check("无方案（站外伪造价不参与最低价）", d["solution"] is None)
    check(
        "详情越界只报原链接",
        [u for u in d["failed_detail_pages"] if u.endswith("/offsite/ghost-7.html")],
        str(d.get("failed_detail_pages")),
    )
    redirects = {x["detail_url"]: x["redirect_target"] for x in d.get("offsite_redirects", [])}
    check(
        "返回跳转目标地址",
        redirects.get(f"{redirect_base}/offsite/ghost-7.html") == f"{offsite_base}/details/ghost-7.html",
        str(d.get("offsite_redirects")),
    )
    check("站外站点零请求", offsite_log == [], str(offsite_log))

    # 12) 先跳站外、再跳回本站：第一跳即拦截，站外仍零请求
    r = client.post(
        "/api/v1/quote",
        json={"catalog_url": f"{redirect_base}/offsite-catalog-return.html", "model": "RETURN-7", "demand": 10},
    )
    d = r.json()
    check("站外后跳回本站仍状态 scrape_failed", d["status"] == "scrape_failed")
    check("无方案", d["solution"] is None)
    redirects = {x["detail_url"]: x["redirect_target"] for x in d.get("offsite_redirects", [])}
    check(
        "中转越界返回原链接与站外目标",
        redirects.get(f"{redirect_base}/offsite/ghost-7-return.html")
        == f"{offsite_base}/details/ghost-7-return.html",
        str(d.get("offsite_redirects")),
    )
    check("站外站点零请求（未被诱导跳回）", offsite_log == [], str(offsite_log))

    # 13) 目录越界：目录的下一页跳站外 → 目录不可用，且站外零请求
    r = client.post(
        "/api/v1/quote",
        json={"catalog_url": f"{redirect_base}/offsite-catalog.html", "model": "MF-50", "demand": 10},
    )
    d = r.json()
    check("目录越界状态 catalog_unreachable", d["status"] == "catalog_unreachable")
    check("无方案", d["solution"] is None)
    check(
        "目录越界返回跳转目标地址",
        d.get("offsite_redirects")
        and d["offsite_redirects"][0]["redirect_target"] == f"{offsite_base}/bounce-back.html",
        str(d.get("offsite_redirects")),
    )
    check("站外站点仍零请求", offsite_log == [], str(offsite_log))

    # 14) 同站跳转仍可用：REDIR-1 详情 302 到同站另一路径，正常报价
    r = client.post(
        "/api/v1/quote",
        json={"catalog_url": f"{redirect_base}/page3.html", "model": "REDIR-1", "demand": 120},
    )
    d = r.json()
    check("同站跳转状态 ok", d["status"] == "ok")
    check("同站跳转后总价 ¥300.00", d["solution"]["total_price"] == "300.00")
    check("详情来源为原链接", d["solution"]["items"][0]["detail_url"].endswith("/go/redir-1.html"))
    check("站外站点零请求", offsite_log == [], str(offsite_log))

    # 15) 同站跳转的目录页（下一页跳回本站）仍可翻页
    r = client.post(
        "/api/v1/quote",
        json={"catalog_url": f"{redirect_base}/page2.html", "model": "MF-50", "demand": 180},
    )
    d = r.json()
    check("同站分页跳转正常", d["status"] == "ok" and len(d["list_pages"]) >= 2, str(d["list_pages"]))

    # 15) 正常报价仍可用（站点边界不影响原目录）
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 180})
    d = r.json()
    check("原目录仍正常报价", d["status"] == "ok" and d["solution"]["total_price"] == "450.00")

    # 10) 兜底扫描语义：列表文案不含型号时逐个核对详情页
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        (tmp_path / "details").mkdir()
        (tmp_path / "index.html").write_text(
            '<html><body><ul class="product-list">'
            '<li class="product-item"><a class="product-link" href="details/d1.html">特价滤膜 甲</a></li>'
            '<li class="product-item"><a class="product-link" href="details/d2.html">特价滤膜 乙</a></li>'
            "</ul></body></html>",
            encoding="utf-8",
        )
        (tmp_path / "details" / "d1.html").write_text(
            '<html><body><h1 class="product-title">特价滤膜 甲</h1>'
            '<table><tr><td class="product-model">FALL-1</td>'
            '<td class="pack-size">40</td>'
            '<td class="pack-price">¥90.00</td>'
            '<td class="stock-packs">8</td></tr></table></body></html>',
            encoding="utf-8",
        )
        # d2.html 故意不存在（404）
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp)
        httpd2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        cat2 = f"http://127.0.0.1:{httpd2.server_address[1]}/index.html"

        # 10a) 详情页型号匹配（FALL-1 只在详情页出现）→ 命中，但 d2 未能核对 → 不给确定最低价
        r = client.post("/api/v1/quote", json={"catalog_url": cat2, "model": "FALL-1", "demand": 40})
        d = r.json()
        check("兜底命中但存在未核对页 → scrape_failed", d["status"] == "scrape_failed")
        check("无方案", d["solution"] is None)
        check("失败页为 d2", d["failed_detail_pages"][0].endswith("/details/d2.html"))

        # 10b) 全部不匹配 → no_matching_products 并标注未核对页
        r = client.post("/api/v1/quote", json={"catalog_url": cat2, "model": "ABSENT-7", "demand": 40})
        d = r.json()
        check("兜底无匹配 → no_matching_products", d["status"] == "no_matching_products")
        check("标注未核对页 d2", any(u.endswith("/details/d2.html") for u in d.get("unverified_failed_pages", [])))
        httpd2.shutdown()

    httpd.shutdown()
    catalog_srv.shutdown()
    offsite_srv.shutdown()
    print("\n全部端到端检查通过 ✓")


if __name__ == "__main__":
    main()
