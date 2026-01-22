from __future__ import annotations

import argparse
import html
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


PORTALS_URL = "https://geometry-dash.fandom.com/wiki/Portals"
TRANSPORTERS_URL = "https://geometry-dash.fandom.com/wiki/Transporters"
OBJECTS_URL = "https://geometry-dash.fandom.com/wiki/Objects"
WIKIA_IMAGE_PREFIX = "https://static.wikia.nocookie.net/geometry-dash/images/"

DEFAULT_USER_AGENT = "Mozilla/5.0"


def _urls_from_srcset(value: str) -> list[str]:
    urls: list[str] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        urls.append(part.split()[0])
    return urls


_STYLE_URL_RE = re.compile(r"url\((?P<q>['\"]?)(?P<url>.*?)(?P=q)\)", re.IGNORECASE)


class _ImageUrlParser(HTMLParser):
    def __init__(self, required_substring: str) -> None:
        super().__init__(convert_charrefs=True)
        self.required_substring = required_substring
        self.urls: set[str] = set()

    def _maybe_add(self, value: str | None) -> None:
        if not value:
            return
        value = html.unescape(value).strip()
        if not value:
            return
        if value.startswith("//"):
            value = "https:" + value
        if self.required_substring in value:
            self.urls.add(normalize_image_url(value))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)

        for key in ("src", "data-src"):
            self._maybe_add(attrs_dict.get(key))

        for key in ("srcset", "data-srcset"):
            value = attrs_dict.get(key)
            if not value:
                continue
            for url in _urls_from_srcset(value):
                self._maybe_add(url)

        if tag == "a":
            self._maybe_add(attrs_dict.get("href"))

        style_value = attrs_dict.get("style")
        if style_value:
            for match in _STYLE_URL_RE.finditer(style_value):
                self._maybe_add(match.group("url"))


def extract_image_urls(html_text: str, required_substring: str) -> list[str]:
    parser = _ImageUrlParser(required_substring=required_substring)
    parser.feed(html_text)
    urls = set(parser.urls)

    raw_re = re.compile(re.escape(required_substring) + r"[^\s\"'<>)]*", re.IGNORECASE)
    for match in raw_re.finditer(html_text):
        urls.add(normalize_image_url(match.group(0)))

    return sorted(urls)


def _sanitize_folder_name(name: str) -> str:
    name = name.replace("_", " ").strip()
    name = re.sub(r"[^\w ._-]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Misc"


class _SectionedImageUrlParser(HTMLParser):
    def __init__(self, required_substring: str) -> None:
        super().__init__(convert_charrefs=True)
        self.required_substring = required_substring
        self.section_to_urls: dict[str, set[str]] = {}
        self.current_section = "Misc"
        self.in_output = False
        self.output_div_depth = 0

    def _add(self, url: str | None) -> None:
        if not self.in_output:
            return
        if not url:
            return
        url = html.unescape(url).strip()
        if not url:
            return
        if url.startswith("//"):
            url = "https:" + url
        if self.required_substring in url:
            section = self.current_section or "Misc"
            self.section_to_urls.setdefault(section, set()).add(normalize_image_url(url))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)

        if tag == "div":
            class_value = attrs_dict.get("class") or ""
            classes = set(class_value.split())
            if self.in_output:
                self.output_div_depth += 1
            elif "mw-parser-output" in classes:
                self.in_output = True
                self.output_div_depth = 1

        if self.in_output and tag == "span":
            class_value = attrs_dict.get("class") or ""
            headline_id = attrs_dict.get("id")
            if headline_id and "mw-headline" in class_value.split():
                self.current_section = _sanitize_folder_name(headline_id)

        for key in ("src", "data-src"):
            self._add(attrs_dict.get(key))

        for key in ("srcset", "data-srcset"):
            value = attrs_dict.get(key)
            if value:
                for url in _urls_from_srcset(value):
                    self._add(url)

        if tag == "a":
            self._add(attrs_dict.get("href"))

        style_value = attrs_dict.get("style")
        if style_value:
            for match in _STYLE_URL_RE.finditer(style_value):
                self._add(match.group("url"))

    def handle_endtag(self, tag: str) -> None:
        if not self.in_output:
            return
        if tag != "div":
            return
        self.output_div_depth -= 1
        if self.output_div_depth <= 0:
            self.in_output = False
            self.output_div_depth = 0


def extract_sectioned_image_urls(html_text: str, required_substring: str) -> dict[str, list[str]]:
    parser = _SectionedImageUrlParser(required_substring=required_substring)
    parser.feed(html_text)
    return {section: sorted(urls) for section, urls in parser.section_to_urls.items() if urls}


def normalize_image_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path

    revision_latest = "/revision/latest"
    idx = path.find(revision_latest)
    if idx != -1:
        path = path[: idx + len(revision_latest)]

    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def fetch_html(url: str, *, user_agent: str = DEFAULT_USER_AGENT, timeout_s: int = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _url_to_filename(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    if "/revision/" in path:
        path = path.split("/revision/", 1)[0]
    filename = os.path.basename(path)
    filename = filename.strip() or "image"
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    return filename


@dataclass(frozen=True)
class DownloadResult:
    url: str
    path: Path | None
    error: str | None
    skipped: bool = False


def download(url: str, dest_dir: Path, *, user_agent: str = DEFAULT_USER_AGENT, timeout_s: int = 60) -> DownloadResult:
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = _url_to_filename(url)
    dest_path = dest_dir / filename
    if dest_path.exists():
        return DownloadResult(url=url, path=dest_path, error=None, skipped=True)
    temp_path = dest_path.with_suffix(dest_path.suffix + ".part")

    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response, open(temp_path, "wb") as out_file:
            while True:
                chunk = response.read(1024 * 64)
                if not chunk:
                    break
                out_file.write(chunk)
        temp_path.replace(dest_path)
        return DownloadResult(url=url, path=dest_path, error=None, skipped=False)
    except (urllib.error.URLError, OSError) as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return DownloadResult(url=url, path=None, error=str(exc), skipped=False)


def scrape_portals(out_dir: Path, *, required_substring: str = WIKIA_IMAGE_PREFIX, delay_s: float = 0.1) -> int:
    return scrape_page(PORTALS_URL, out_dir, required_substring=required_substring, delay_s=delay_s)


def scrape_transporters(
    out_dir: Path, *, required_substring: str = WIKIA_IMAGE_PREFIX, delay_s: float = 0.1
) -> int:
    return scrape_page(TRANSPORTERS_URL, out_dir, required_substring=required_substring, delay_s=delay_s)


def scrape_page(
    url: str,
    out_dir: Path,
    *,
    required_substring: str = WIKIA_IMAGE_PREFIX,
    delay_s: float = 0.1,
    user_agent: str = DEFAULT_USER_AGENT,
    max_images: int = 0,
) -> int:
    html_text = fetch_html(url, user_agent=user_agent)
    urls = extract_image_urls(html_text, required_substring=required_substring)
    if not urls:
        print(f"No matching images found on {url}")
        return 1

    if max_images and len(urls) > max_images:
        urls = urls[:max_images]

    print(f"Found {len(urls)} matching images.")
    failures = 0
    for idx, url in enumerate(urls, start=1):
        result = download(url, out_dir, user_agent=user_agent)
        if result.error:
            failures += 1
            print(f"[{idx}/{len(urls)}] FAIL {url} -> {result.error}")
        elif result.skipped:
            print(f"[{idx}/{len(urls)}] SKIP {url} -> {result.path}")
        else:
            print(f"[{idx}/{len(urls)}] OK   {url} -> {result.path}")
        if delay_s:
            time.sleep(delay_s)

    if failures:
        print(f"Done with {failures} failures.")
        return 2
    print("Done.")
    return 0


def scrape_objects(
    out_dir: Path,
    *,
    required_substring: str = WIKIA_IMAGE_PREFIX,
    delay_s: float = 0.1,
    user_agent: str = DEFAULT_USER_AGENT,
    max_images_per_section: int = 0,
) -> int:
    html_text = fetch_html(OBJECTS_URL, user_agent=user_agent)
    sectioned = extract_sectioned_image_urls(html_text, required_substring=required_substring)
    if not sectioned:
        print(f"No matching images found on {OBJECTS_URL}")
        return 1

    sections = sorted(sectioned.items(), key=lambda kv: kv[0].lower())
    total = sum(len(urls) for _, urls in sections)
    print(f"Found {total} matching images across {len(sections)} sections.")

    failures = 0
    for section, urls in sections:
        if max_images_per_section and len(urls) > max_images_per_section:
            urls = urls[:max_images_per_section]
        section_dir = out_dir / section
        print(f"Section: {section} ({len(urls)} images)")
        for idx, image_url in enumerate(urls, start=1):
            result = download(image_url, section_dir, user_agent=user_agent)
            if result.error:
                failures += 1
                print(f"  [{idx}/{len(urls)}] FAIL {image_url} -> {result.error}")
            elif result.skipped:
                print(f"  [{idx}/{len(urls)}] SKIP {image_url} -> {result.path}")
            else:
                print(f"  [{idx}/{len(urls)}] OK   {image_url} -> {result.path}")
            if delay_s:
                time.sleep(delay_s)

    if failures:
        print(f"Done with {failures} failures.")
        return 2
    print("Done.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Geometry Dash wiki images.")
    parser.add_argument(
        "--page",
        choices=["portals", "transporters", "objects"],
        default="portals",
        help="Which wiki page to scrape (default: portals).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory for downloaded images (default: Images/<Page>).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Delay between downloads in seconds (default: 0.1).",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent header to use for requests (default: Mozilla/5.0).",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="Max images per section (objects) or per page (others); 0 means unlimited.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out) if args.out else Path("Images") / args.page.capitalize()

    if args.page == "portals":
        return scrape_page(PORTALS_URL, out_dir, delay_s=args.delay, user_agent=args.user_agent, max_images=args.max)
    if args.page == "transporters":
        return scrape_page(
            TRANSPORTERS_URL, out_dir, delay_s=args.delay, user_agent=args.user_agent, max_images=args.max
        )
    if args.page == "objects":
        return scrape_objects(out_dir, delay_s=args.delay, user_agent=args.user_agent, max_images_per_section=args.max)
    raise RuntimeError(f"Unknown page: {args.page}")


if __name__ == "__main__":
    raise SystemExit(main())
