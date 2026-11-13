"""离线样本站服务：静态目录 + 站内/站外跳转样例（仅标准库，无需额外依赖）。

两个可独立启动的静态站点：
  - 目录站：托管 sample_site/ 根目录（index.html、page2.html、page3.html 等）；
  - 站外站：托管 sample_site/offsite/ 下的模拟外部供应商页面。

跳转样例（302）：
  目录站  /offsite/ghost-7.html        -> <站外站>/details/ghost-7.html
  目录站  /offsite/ghost-7-return.html -> <站外站>/details/ghost-7-return.html
  目录站  /bounce.html                 -> <站外站>/bounce-back.html
  目录站  /go/redir-1.html             -> /details/redir-1.html（同站跳转，应正常抓取）
  站外站  /details/ghost-7-return.html -> <目录站>/details/mf50-100.html（先站外、后跳回本站）
  站外站  /bounce-back.html            -> <目录站>/page3.html（先站外、后跳回本站）

两个站点的 netloc 必然不同（端口不同），抓取方应在第一跳就核对出站外目标并
拦截，绝不向站外站发出任何请求。两个站点都会记录收到的请求 (host, path)，
离线测试据此断言"站外零请求"。

单独运行（仅目录站；站外跳转目标默认使用不可达的 offsite.invalid，
确保离线环境不可能真的请求到站外页面）：
  python sample_site/server.py --port 8080
"""
from __future__ import annotations

import argparse
import functools
import http.server
import pathlib
import posixpath
import threading
from urllib.parse import unquote, urlsplit

OFFSITE_PATH_ALIASES = {
    "/details/ghost-7.html": "ghost-7-real.html",
}


def catalog_redirects(offsite_origin: str) -> dict[str, str]:
    return {
        "/offsite/ghost-7.html": f"{offsite_origin}/details/ghost-7.html",
        "/offsite/ghost-7-return.html": f"{offsite_origin}/details/ghost-7-return.html",
        "/bounce.html": f"{offsite_origin}/bounce-back.html",
        # 同站跳转：应被正常跟随并抓到真实详情页
        "/go/redir-1.html": "/details/redir-1.html",
    }


def offsite_redirects(catalog_origin: str) -> dict[str, str]:
    return {
        # 先跳站外、再跳回本站：仍必须在第一跳拦截
        "/details/ghost-7-return.html": f"{catalog_origin}/details/mf50-100.html",
        "/bounce-back.html": f"{catalog_origin}/page3.html",
    }


class StaticSiteHandler(http.server.SimpleHTTPRequestHandler):
    """静态文件 + 可配置跳转表 + 请求日志。"""

    def __init__(
        self,
        *args,
        root: str,
        redirects: dict[str, str],
        request_log: list[tuple[str, str]],
        path_aliases: dict[str, str] | None = None,
        **kwargs,
    ):
        self.root = pathlib.Path(root)
        self.redirects = redirects
        self.request_log = request_log
        self.path_aliases = path_aliases or {}
        super().__init__(*args, directory=str(self.root), **kwargs)

    def do_GET(self):  # noqa: N802 - 标准库方法名
        path = urlsplit(self.path).path
        self.request_log.append((self.headers.get("Host", ""), path))
        location = self.redirects.get(path)
        if location:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

    def translate_path(self, path: str) -> str:
        parts = [
            part
            for part in posixpath.normpath(unquote(urlsplit(path).path)).split("/")
            if part not in ("", ".", "..")
        ]
        alias = self.path_aliases.get("/" + "/".join(parts))
        if alias:
            parts = alias.split("/")
        return str(self.root.joinpath(*parts))

    def log_message(self, fmt, *args):  # noqa: A003 - 标准库签名
        pass  # 保持测试输出干净


def _start(
    root: pathlib.Path,
    redirects: dict[str, str],
    request_log: list[tuple[str, str]],
    port: int,
    host: str,
    path_aliases: dict[str, str] | None = None,
) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(
        StaticSiteHandler,
        root=str(root),
        redirects=redirects,
        request_log=request_log,
        path_aliases=path_aliases,
    )
    httpd = http.server.ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://{host}:{httpd.server_address[1]}"


def build_catalog_server(
    port: int = 0,
    host: str = "127.0.0.1",
    site_root: str | None = None,
    offsite_origin: str = "http://offsite.invalid",
    request_log: list[tuple[str, str]] | None = None,
) -> tuple[http.server.ThreadingHTTPServer, str]:
    """启动目录站（含站内/站外跳转样例），返回 (httpd, base URL)。"""
    root = pathlib.Path(site_root) if site_root else pathlib.Path(__file__).resolve().parent
    log: list[tuple[str, str]] = request_log if request_log is not None else []
    return _start(root, catalog_redirects(offsite_origin), log, port, host)


def build_offsite_server(
    port: int = 0,
    host: str = "127.0.0.1",
    site_root: str | None = None,
    catalog_origin: str = "http://site.invalid",
    request_log: list[tuple[str, str]] | None = None,
) -> tuple[http.server.ThreadingHTTPServer, str]:
    """启动站外样例站（仅测试用；正常部署不对外提供）。"""
    root = pathlib.Path(site_root) if site_root else pathlib.Path(__file__).resolve().parent
    log: list[tuple[str, str]] = request_log if request_log is not None else []
    return _start(
        root / "offsite",
        offsite_redirects(catalog_origin),
        log,
        port,
        host,
        path_aliases=OFFSITE_PATH_ALIASES,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="离线样本站（含站内/站外跳转样例）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--site", default=None, help="样本站根目录（默认为本文件所在目录）")
    parser.add_argument(
        "--offsite-origin",
        default="http://offsite.invalid",
        help="站外跳转目标地址（离线默认不可达，确保不会真的请求到站外）",
    )
    args = parser.parse_args()

    _, origin = build_catalog_server(
        port=args.port,
        host=args.host,
        site_root=args.site,
        offsite_origin=args.offsite_origin,
    )
    print(f"样本站已启动: {origin}（站外跳转目标: {args.offsite_origin}）")
    try:
        threading.Event().wait()  # 前台常驻，等待 Ctrl+C
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
