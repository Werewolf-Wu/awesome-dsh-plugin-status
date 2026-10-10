#!/usr/bin/env python3
"""Sync the awesome-dsh-plugin snapshot and regenerate the mirror files.

Downloads the upstream Chinese README and LICENSE at a pinned commit, parses
the plugin list, queries the GitHub GraphQL API for the archived flag of every
repository, and regenerates the managed outputs:

    upstream/README.zh.md   byte-identical copy of the upstream README
    LICENSE                 byte-identical copy of the upstream license
    README.zh.md            full upstream text + four status badges per entry
    catalog/cNN-pNNN.md     per-category paginated browsing pages
    README.md               Chinese entry page

This script only writes data files; publishing (commit/push) is done by
scripts/publish.sh. Credentials are read by the gh CLI itself and are never
printed or passed as command arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

UPSTREAM_REPO = "awesome-dsh-plugin/awesome-dsh-plugin"
UPSTREAM_BRANCH = "main"
MIRROR_REPO = "Werewolf-Wu/awesome-dsh-plugin-status"
UPSTREAM_REPO_URL = f"https://github.com/{UPSTREAM_REPO}"
MIRROR_REPO_URL = f"https://github.com/{MIRROR_REPO}"
RAW_URL = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
BLOB_URL = "https://github.com/{repo}/blob/{ref}/{path}"
COMMIT_URL = "https://github.com/{repo}/commit/{ref}"

START_MARKER = "<!-- BEGIN PLUGINS -->"
END_MARKER = "<!-- END PLUGINS -->"

ITEM_RE = re.compile(r"^- \[(.+?)\]\((https://github\.com/[^)]+)\) ([—-]) (.+)$")
REL_LINK_RE = re.compile(r"\[([^\]]*)\]\(((?:\./)?(?:README|contributing)\.md(?:#[^)\s]*)?)\)")
GH_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]+")
CATALOG_NAME_RE = re.compile(r"c[0-9]+-p[0-9]+\.md")
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
BADGE_ALTS = ("last commit", "created at", "release date", "archived")
BADGE_MARKER = "![last commit](https://img.shields.io/github/last-commit/"
OUR_BADGE_URL_RE = re.compile(
    r"https://img\.shields\.io/(?:github/(?:last-commit|created-at|release-date-pre)/|badge/archived-)[^)\s\"]+"
)
INLINE_RE = re.compile(
    r"`([^`]+)`"                                # code span
    r"|\[([^\]]+)\]\(([^)\s]+)\)"               # markdown link
    r"|\*\*([^*]+)\*\*"                         # bold
    r"|(https?://[^\s<>\"）】，。；、]+)"        # bare url
)

PAGE_MAX_ITEMS = 100
PAGE_MAX_BYTES = 200_000
NAV_RESERVE_BYTES = 512
TABLE_OPEN = '<table width="100%">'
TABLE_CLOSE = "</table>"
TABLE_ROW_PREFIX = '<tr><td width="300">'
DESCRIPTION_CELL = '<td colspan="2">'
BATCH_SIZE = 50
BATCH_DELAY_SECONDS = 1.0
TIMEOUT_SECONDS = 30
ARCHIVED_LABELS = {
    "yes": "archived-yes-red",
    "no": "archived-no-brightgreen",
    "unknown": "archived-unknown-lightgrey",
}


class SyncError(RuntimeError):
    """Unrecoverable sync failure: nothing is published."""


@dataclass(frozen=True)
class Plugin:
    raw: str
    name: str
    url: str
    description: str
    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def key(self) -> str:
        return self.slug.lower()


@dataclass(frozen=True)
class Category:
    title: str
    plugins: tuple[Plugin, ...]


@dataclass(frozen=True)
class ParsedDocument:
    lines: tuple[str, ...]
    categories: tuple[Category, ...]
    items_by_line: dict[int, Plugin]

    @property
    def plugins(self) -> tuple[Plugin, ...]:
        return tuple(plugin for category in self.categories for plugin in category.plugins)


@dataclass(frozen=True)
class FetchedSnapshot:
    sha: str
    readme_bytes: bytes
    license_bytes: bytes


@dataclass(frozen=True)
class CategoryPages:
    title: str
    count: int
    pages: tuple[str, ...]


@dataclass(frozen=True)
class SyncReport:
    sha: str
    items: int
    repos: int
    categories: int
    pages: int
    archived_counts: dict[str, int]
    upstream_sha256: str


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- parsing ---


def parse_readme(text: str) -> ParsedDocument:
    lines = text.split("\n")
    if lines.count(START_MARKER) != 1 or lines.count(END_MARKER) != 1:
        raise SyncError("upstream README must contain exactly one BEGIN/END plugins marker pair")
    start = lines.index(START_MARKER)
    end = lines.index(END_MARKER)
    if start >= end:
        raise SyncError("upstream README plugin markers are out of order")

    categories: list[Category] = []
    items_by_line: dict[int, Plugin] = {}
    for index in range(start + 1, end):
        line = lines[index]
        lineno = index + 1
        if not line.strip():
            continue
        if line.startswith("### "):
            title = line[4:].strip()
            if not title:
                raise SyncError(f"line {lineno}: category heading has no title")
            categories.append(Category(title=title, plugins=()))
            continue
        if line.startswith("- "):
            if not categories:
                raise SyncError(f"line {lineno}: plugin entry before the first category heading")
            match = ITEM_RE.match(line)
            if match is None:
                raise SyncError(f"line {lineno}: unsupported plugin entry syntax: {line[:120]!r}")
            if BADGE_MARKER in line:
                raise SyncError(f"line {lineno}: entry already carries status badges, refusing to nest another set")
            owner, repo = _github_root(match.group(2), lineno)
            plugin = Plugin(
                raw=line,
                name=match.group(1),
                url=match.group(2),
                description=match.group(4),
                owner=owner,
                repo=repo,
            )
            items_by_line[index] = plugin
            categories[-1] = Category(title=categories[-1].title, plugins=categories[-1].plugins + (plugin,))
            continue
        raise SyncError(f"line {lineno}: unexpected content between plugin markers: {line[:120]!r}")

    if not categories:
        raise SyncError("no category headings found between plugin markers")
    for category in categories:
        if not category.plugins:
            raise SyncError(f"category has no entries: {category.title!r}")
    if not items_by_line:
        raise SyncError("no plugin entries found between plugin markers")
    return ParsedDocument(lines=tuple(lines), categories=tuple(categories), items_by_line=items_by_line)


def _github_root(url: str, lineno: int) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "github.com":
        raise SyncError(f"line {lineno}: plugin link is not on github.com: {url}")
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2:
        raise SyncError(f"line {lineno}: plugin link has no owner/repo path: {url}")
    owner, repo = segments[0], segments[1]
    if repo.lower().endswith(".git"):
        repo = repo[: -len(".git")]
    if not owner or not repo or GH_NAME_RE.fullmatch(owner) is None or GH_NAME_RE.fullmatch(repo) is None:
        raise SyncError(f"line {lineno}: invalid GitHub owner/repo in {url}")
    return owner, repo


def unique_repo_slugs(document: ParsedDocument) -> list[str]:
    seen: set[str] = set()
    slugs: list[str] = []
    for plugin in document.plugins:
        if plugin.key not in seen:
            seen.add(plugin.key)
            slugs.append(plugin.slug)
    return slugs


# ---------------------------------------------------------------- badges ----


def _encode_slug(slug: str) -> str:
    return "/".join(urllib.parse.quote(segment, safe="") for segment in slug.split("/"))


def badge_urls(slug: str, archived: str) -> tuple[str, str, str, str]:
    try:
        label = ARCHIVED_LABELS[archived]
    except KeyError as exc:
        raise SyncError(f"unknown archived status: {archived!r}") from exc
    encoded = _encode_slug(slug)
    return (
        f"https://img.shields.io/github/last-commit/{encoded}"
        "?display_timestamp=committer&cacheSeconds=86400",
        f"https://img.shields.io/github/created-at/{encoded}?cacheSeconds=86400",
        f"https://img.shields.io/github/release-date-pre/{encoded}"
        "?display_date=published_at&cacheSeconds=86400",
        f"https://img.shields.io/badge/{label}?cacheSeconds=86400",
    )


def badge_images(slug: str, archived: str) -> tuple[str, str, str, str]:
    return tuple(
        f"![{alt}]({url})" for alt, url in zip(BADGE_ALTS, badge_urls(slug, archived))
    )


def badge_markdown(slug: str, archived: str) -> str:
    """Four badges on one line, used by the verbatim full-text mirror."""
    return " ".join(badge_images(slug, archived))


def badge_cell(slug: str, archived: str) -> str:
    """Four raw HTML badges, one per line; the wide cell keeps them unscaled."""
    return "<br>".join(
        f'<img src="{url}" alt="{alt}">' for alt, url in zip(BADGE_ALTS, badge_urls(slug, archived))
    )


def escape_html(text: str, *, quote: bool = False) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote:
        text = text.replace('"', "&quot;")
    return text


def render_inline_markdown(text: str) -> str:
    """Convert the inline markdown that actually occurs in descriptions to HTML.

    Raw HTML in the source is escaped so it shows up as literal text, matching
    the upstream wording instead of being dropped or mis-parsed by sanitizers.
    """
    out: list[str] = []
    cursor = 0
    for match in INLINE_RE.finditer(text):
        out.append(escape_html(text[cursor : match.start()]))
        code, link, href, bold, url = match.groups()
        if code is not None:
            out.append(f"<code>{escape_html(code)}</code>")
        elif link is not None:
            out.append(f'<a href="{escape_html(href, quote=True)}">{escape_html(link)}</a>')
        elif bold is not None:
            out.append(f"<strong>{escape_html(bold)}</strong>")
        else:
            out.append(f'<a href="{escape_html(url, quote=True)}">{escape_html(url)}</a>')
        cursor = match.end()
    out.append(escape_html(text[cursor:]))
    return "".join(out)


def render_item_line(plugin: Plugin, archived: str) -> str:
    return f"{plugin.raw} {badge_markdown(plugin.slug, archived)}"


def render_table_block(plugin: Plugin, archived: str) -> str:
    """Two table rows per plugin: name + badges, then the description."""
    name = escape_html(plugin.name)
    href = escape_html(plugin.url, quote=True)
    return (
        f'{TABLE_ROW_PREFIX}<a href="{href}">{name}</a></td>'
        f"<td>{badge_cell(plugin.slug, archived)}</td></tr>\n"
        f"<tr>{DESCRIPTION_CELL}{render_inline_markdown(plugin.description)}</td></tr>"
    )


# --------------------------------------------------------------- rendering --


def rewrite_relative_links(line: str, sha: str) -> str:
    def replace(match: re.Match[str]) -> str:
        target = match.group(2)
        if target.startswith("./"):
            target = target[2:]
        return f"[{match.group(1)}]({BLOB_URL.format(repo=UPSTREAM_REPO, ref=sha, path=target)})"

    return REL_LINK_RE.sub(replace, line)


def render_readme_zh(document: ParsedDocument, sha: str, snapshot: str, statuses: dict[str, str]) -> str:
    short = sha[:7]
    note = [
        f"> **非官方镜像。** 本文件由 [awesome-dsh-plugin-status]({MIRROR_REPO_URL}) 自动生成，正文来自上游"
        f" [`{UPSTREAM_REPO}`]({UPSTREAM_REPO_URL}) 的 `{UPSTREAM_BRANCH}` 分支 commit"
        f" [`{short}`]({COMMIT_URL.format(repo=UPSTREAM_REPO, ref=sha)})，内容、顺序与空白保持原样。",
        f"> 快照时间：{snapshot}。每个条目末尾附加四个 [Shields.io](https://shields.io/) 状态徽章"
        "（最后提交 / 仓库创建日期 / 最近 Release 日期 / 是否归档）；徽章是动态图片，其数据不保存在本文件中。",
        "> 原文字节镜像见 [`upstream/README.zh.md`](upstream/README.zh.md)，分页浏览见 [`README.md`](README.md)"
        "（本文件较大，GitHub 可能无法完整渲染）。投稿与历史请到上游仓库。",
    ]

    out: list[str] = []
    fenced = False
    for index, line in enumerate(document.lines):
        if index == 0:
            out.append(line)
            out.append("")
            out.extend(note)
            continue
        plugin = document.items_by_line.get(index)
        if plugin is not None:
            out.append(render_item_line(plugin, statuses[plugin.key]))
            continue
        if FENCE_RE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        out.append(line if fenced else rewrite_relative_links(line, sha))
    return "\n".join(out)


def page_name(category_index: int, page_index: int) -> str:
    return f"c{category_index:02d}-p{page_index:03d}.md"


def paginate(
    item_lines: list[str],
    header: str,
    *,
    preamble: str = "",
    postamble: str = "",
    max_items: int = PAGE_MAX_ITEMS,
    max_bytes: int = PAGE_MAX_BYTES,
) -> list[list[str]]:
    base = (
        len(header.encode("utf-8"))
        + len(preamble.encode("utf-8"))
        + len(postamble.encode("utf-8"))
        + NAV_RESERVE_BYTES
    )
    pages: list[list[str]] = []
    current: list[str] = []
    size = base
    for line in item_lines:
        line_bytes = len(line.encode("utf-8")) + 1
        if current and (len(current) >= max_items or size + line_bytes > max_bytes):
            pages.append(current)
            current = []
            size = base
        if size + line_bytes > max_bytes:
            raise SyncError(f"single entry exceeds the {max_bytes} byte page budget")
        current.append(line)
        size += line_bytes
    if current:
        pages.append(current)
    if not pages:
        raise SyncError("nothing to paginate")
    return pages


def render_catalog(
    document: ParsedDocument,
    sha: str,
    snapshot: str,
    statuses: dict[str, str],
) -> tuple[list[CategoryPages], dict[str, str]]:
    index: list[CategoryPages] = []
    files: dict[str, str] = {}
    for category_index, category in enumerate(document.categories, 1):
        header = (
            f"## {category.title}\n\n"
            f"来源：[{UPSTREAM_REPO}]({UPSTREAM_REPO_URL}) · commit "
            f"[`{sha[:7]}`]({COMMIT_URL.format(repo=UPSTREAM_REPO, ref=sha)})\n\n"
            f"快照：{snapshot} · 分类共 {len(category.plugins)} 条\n\n"
        )
        blocks = [render_table_block(plugin, statuses[plugin.key]) for plugin in category.plugins]
        pages = paginate(blocks, header, preamble=TABLE_OPEN + "\n", postamble=TABLE_CLOSE + "\n")
        total = len(pages)
        paths: list[str] = []
        for page_index, page in enumerate(pages, 1):
            relative = f"catalog/{page_name(category_index, page_index)}"
            paths.append(relative)
            nav = ["[首页](../README.md)", f"第 {page_index}/{total} 页"]
            if page_index > 1:
                nav.append(f"[上一页]({page_name(category_index, page_index - 1)})")
            if page_index < total:
                nav.append(f"[下一页]({page_name(category_index, page_index + 1)})")
            content = (
                header
                + " · ".join(nav)
                + "\n\n"
                + TABLE_OPEN
                + "\n"
                + "\n".join(page)
                + "\n"
                + TABLE_CLOSE
                + "\n"
            )
            if len(content.encode("utf-8")) > PAGE_MAX_BYTES:
                raise SyncError(f"{relative}: rendered page exceeds {PAGE_MAX_BYTES} bytes")
            if len(page) > PAGE_MAX_ITEMS:
                raise SyncError(f"{relative}: rendered page has more than {PAGE_MAX_ITEMS} entries")
            files[relative] = content
        index.append(CategoryPages(title=category.title, count=len(category.plugins), pages=tuple(paths)))
    return index, files


def render_entry_readme(
    document: ParsedDocument,
    sha: str,
    snapshot: str,
    statuses: dict[str, str],
    catalog_index: list[CategoryPages],
    enhanced_bytes: int,
) -> str:
    items = document.plugins
    repos = len(unique_repo_slugs(document))
    pages = sum(len(entry.pages) for entry in catalog_index)
    counts = {"yes": 0, "no": 0, "unknown": 0}
    for plugin in items:
        counts[statuses[plugin.key]] += 1
    short = sha[:7]
    commit_url = COMMIT_URL.format(repo=UPSTREAM_REPO, ref=sha)
    blob_readme = BLOB_URL.format(repo=UPSTREAM_REPO, ref=sha, path="README.zh.md")
    blob_license = BLOB_URL.format(repo=UPSTREAM_REPO, ref=sha, path="LICENSE")
    blob_contributing = BLOB_URL.format(repo=UPSTREAM_REPO, ref=sha, path="contributing.md")
    raw_enhanced = RAW_URL.format(repo=MIRROR_REPO, ref="main", path="README.zh.md")
    raw_upstream = RAW_URL.format(repo=MIRROR_REPO, ref="main", path="upstream/README.zh.md")

    table = ["| 分类 | 条目 | 页面 |", "| --- | --- | --- |"]
    for entry in catalog_index:
        links = " ".join(f"[{number}]({path})" for number, path in enumerate(entry.pages, 1))
        table.append(f"| [{entry.title}]({entry.pages[0]}) | {entry.count} | {links} |")

    return "\n".join(
        [
            "# awesome-dsh-plugin 状态镜像",
            "",
            f"每日从上游 [`{UPSTREAM_REPO}`]({UPSTREAM_REPO_URL}) 的 `{UPSTREAM_BRANCH}` 分支同步 DSH 插件列表，"
            "为每个条目附加四项仓库状态徽章，并按分类分页浏览。本仓库是**非官方镜像**：内容全部来自上游，"
            "不修改内容，也不接受插件投稿。",
            "",
            "## 当前快照",
            "",
            f"- 上游 commit：[`{short}`]({commit_url})"
            f"（[该版本的 README.zh.md]({blob_readme}) · [LICENSE]({blob_license})）",
            f"- 最近成功同步：{snapshot}",
            f"- 快照规模：{len(items)} 个条目 / {repos} 个仓库 / {len(document.categories)} 个分类 / {pages} 个浏览页",
            f"- 归档状态：`yes` {counts['yes']} 个 · `no` {counts['no']} 个 · `unknown` {counts['unknown']} 个",
            "",
            "## 文件",
            "",
            f"- [完整增强版 README.zh.md](README.zh.md)：上游全文（含目录、收录标准、贡献说明、警告与免责声明）"
            f"＋ 每条四徽章；约 {enhanced_bytes / 1048576:.1f} MB，超出 GitHub 的 Markdown 渲染上限，"
            "文件页不显示内容，请用 [Raw](" + raw_enhanced + ") 下载查看。",
            "- [原文字节镜像 upstream/README.zh.md](upstream/README.zh.md)：与上游下载内容逐字节一致的原文，用于核对。",
            f"- Raw 下载：[增强版]({raw_enhanced}) · [原文]({raw_upstream})",
            "",
            "## 分类浏览",
            "",
            "分类页用表格呈现：每个插件占两行——第一行左侧是插件名称（链接指向插件仓库）、右侧是四个状态徽章"
            "（每个徽章一行、按原始尺寸显示）；第二行是跨两列的简介。",
            "",
            *table,
            "",
            "## 徽章说明",
            "",
            "每个条目末尾有四个徽章，全部由 [Shields.io](https://shields.io/) 动态生成，"
            "并带 `cacheSeconds=86400`（建议缓存 24 小时）：",
            "",
            "| 徽章 | 含义 |",
            "| --- | --- |",
            "| `last commit` | 仓库默认分支最后一次提交的时间（committer 时间，rebase 后仍显示新时间） |",
            "| `created at` | 仓库创建时间（Shields 按相对月份显示） |",
            "| `release date` | 最近一次 GitHub Release 的发布时间（含预发布，取 Releases 列表首项） |",
            "| `archived` | 本次同步时刻的归档状态快照：`yes` / `no` / `unknown` |",
            "",
            "- 仓库没有 Release 或不可访问时，Shields 会显示它自己的错误徽章（如 `no releases`、`repo not found`、`invalid`），"
            "本镜像不伪造版本或日期。",
            "- `archived` 为 `unknown` 表示这次同步时该仓库不存在（可能已删除或改名）。",
            "- 徽章是动态图片：`cacheSeconds=86400` 只是缓存提示，实际刷新还受 GitHub 图片代理与 CDN 影响，"
            "可能与仓库最新状态有延迟。",
            "",
            "## 说明与限制",
            "",
            "- `main` 分支始终保持只有一个可达根提交：每天同步成功后生成新的单提交快照并强推，不保留历史；"
            "快照新旧以本页“最近成功同步”时间为准。",
            f"- 上游保留完整历史、投稿与评审流程：[上游仓库]({UPSTREAM_REPO_URL}) · "
            f"[贡献指南]({blob_contributing})。插件本身的 bug 请到插件自己的仓库提交。",
            f"- 上游的警告与免责声明原样保留在完整增强版中（Raw 下载见上文“文件”一节），也可阅读"
            f"[上游原文的对应章节]({blob_readme}#免责声明)：安装插件等于运行第三方代码，请自行审阅源码、风险自担。",
            "- 本仓库代码与内容采用 CC0-1.0（见 [LICENSE](LICENSE)）。",
            "",
        ]
    )


# -------------------------------------------------------------- assembly ----


def _catalog_item_lines(content: str, path: str) -> list[str]:
    """Return the table body lines of a catalog page (two lines per plugin)."""
    if TABLE_OPEN not in content or TABLE_CLOSE not in content:
        raise SyncError(f"{path}: catalog page is missing the item table")
    body = content.split(TABLE_OPEN, 1)[1].split(TABLE_CLOSE, 1)[0].strip("\n")
    lines = body.split("\n")
    if len(lines) % 2 or not all(lines[index].startswith(TABLE_ROW_PREFIX) for index in range(0, len(lines), 2)):
        raise SyncError(f"{path}: catalog table rows are malformed")
    return lines


def verify_package(
    document: ParsedDocument,
    statuses: dict[str, str],
    files: dict[str, bytes],
    catalog_index: list[CategoryPages],
) -> None:
    expected = [render_item_line(plugin, statuses[plugin.key]) for plugin in document.plugins]
    expected_blocks = [render_table_block(plugin, statuses[plugin.key]) for plugin in document.plugins]
    enhanced = files["README.zh.md"].decode("utf-8")
    found = [line for line in enhanced.split("\n") if line.startswith("- ") and BADGE_MARKER in line]
    if found != expected:
        raise SyncError("enhanced README.zh.md integrity check failed (entries duplicated, reordered or lost)")

    catalog_found: list[str] = []
    for path in [path for entry in catalog_index for path in entry.pages]:
        lines = _catalog_item_lines(files[path].decode("utf-8"), path)
        catalog_found.extend("\n".join(lines[index : index + 2]) for index in range(0, len(lines), 2))
    if catalog_found != expected_blocks:
        raise SyncError("catalog pages integrity check failed (entries duplicated, reordered or lost)")

    catalog_items = {path: len(_catalog_item_lines(files[path].decode("utf-8"), path)) // 2
                     for path in [path for entry in catalog_index for path in entry.pages]}
    for name, blob in files.items():
        if name == "README.zh.md":
            file_items = sum(1 for line in blob.decode("utf-8").split("\n") if line.startswith("- ") and BADGE_MARKER in line)
        elif name.startswith("catalog/"):
            file_items = catalog_items[name]
        else:
            continue
        text = blob.decode("utf-8")
        badge_urls = OUR_BADGE_URL_RE.findall(text)
        if len(badge_urls) != 4 * file_items:
            raise SyncError(
                f"{name}: expected {4 * len(file_items)} badge URLs for {len(file_items)} entries, "
                f"found {len(badge_urls)}"
            )
        for url in badge_urls:
            if "cacheSeconds=86400" not in url:
                raise SyncError(f"{name}: badge URL is missing cacheSeconds=86400: {url}")


def generate_package(
    document: ParsedDocument,
    readme_bytes: bytes,
    license_bytes: bytes,
    sha: str,
    snapshot: str,
    statuses: dict[str, str],
) -> tuple[dict[str, bytes], list[CategoryPages]]:
    for plugin in document.plugins:
        if plugin.key not in statuses:
            raise SyncError(f"missing archived status for {plugin.slug}")
    catalog_index, catalog_files = render_catalog(document, sha, snapshot, statuses)
    enhanced = render_readme_zh(document, sha, snapshot, statuses).encode("utf-8")
    files: dict[str, bytes] = {
        "upstream/README.zh.md": readme_bytes,
        "LICENSE": license_bytes,
        "README.zh.md": enhanced,
    }
    for relative, content in catalog_files.items():
        files[relative] = content.encode("utf-8")
    files["README.md"] = render_entry_readme(
        document, sha, snapshot, statuses, catalog_index, len(enhanced)
    ).encode("utf-8")
    verify_package(document, statuses, files, catalog_index)
    return files, catalog_index


def write_outputs(output_dir: Path, files: dict[str, bytes]) -> list[str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="awesome-dsh-plugin-status-"))
    try:
        for relative, blob in files.items():
            staged = staging / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(blob)
        written: list[str] = []
        for relative in sorted(files):
            target = output_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging / relative), str(target))
            written.append(relative)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    catalog_dir = output_dir / "catalog"
    keep = {Path(relative).name for relative in files if relative.startswith("catalog/")}
    if catalog_dir.is_dir():
        for existing in sorted(catalog_dir.iterdir()):
            if existing.is_file() and CATALOG_NAME_RE.fullmatch(existing.name) and existing.name not in keep:
                existing.unlink()
    return written


# ---------------------------------------------------------------- network ---


def run_gh(
    args: list[str],
    *,
    input_data: str | None = None,
    timeout: int = TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["gh", *args], input=input_data, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise SyncError("gh CLI is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"gh {' '.join(args)} exceeded the {timeout}s timeout") from exc


def fetch_upstream_sha() -> str:
    proc = run_gh(["api", f"repos/{UPSTREAM_REPO}/commits/{UPSTREAM_BRANCH}", "--jq", ".sha"])
    sha = proc.stdout.strip()
    if proc.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise SyncError(
            f"failed to resolve {UPSTREAM_REPO}@{UPSTREAM_BRANCH}: exit={proc.returncode} "
            f"stdout={sha[:80]!r} stderr={proc.stderr.strip()[:300]!r}"
        )
    return sha


def http_get(url: str, *, timeout: int = TIMEOUT_SECONDS) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"{MIRROR_REPO} sync"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            data = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise SyncError(f"GET {url} failed: {exc}") from exc
    if status != 200:
        raise SyncError(f"GET {url} returned HTTP {status}")
    return data


def fetch_snapshot() -> FetchedSnapshot:
    sha = fetch_upstream_sha()
    readme_bytes = http_get(RAW_URL.format(repo=UPSTREAM_REPO, ref=sha, path="README.zh.md"))
    license_bytes = http_get(RAW_URL.format(repo=UPSTREAM_REPO, ref=sha, path="LICENSE"))
    return FetchedSnapshot(sha=sha, readme_bytes=readme_bytes, license_bytes=license_bytes)


def build_archived_query(batch: list[str]) -> tuple[str, dict[str, str]]:
    alias_keys: dict[str, str] = {}
    parts: list[str] = []
    for index, slug in enumerate(batch):
        owner, _, repo = slug.partition("/")
        alias = f"r{index}"
        alias_keys[alias] = slug.lower()
        parts.append(f"{alias}: repository(owner:{json.dumps(owner)}, name:{json.dumps(repo)}) {{ isArchived }}")
    return "query { " + " ".join(parts) + " }", alias_keys


def interpret_archived_payload(payload: object, alias_keys: dict[str, str]) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise SyncError("GraphQL response is not a JSON object")
    errors = payload.get("errors") or []
    if not isinstance(errors, list):
        raise SyncError("GraphQL response 'errors' is not a list")
    data = payload.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SyncError("GraphQL response 'data' is not an object")

    not_found: set[str] = set()
    for error in errors:
        path = error.get("path") if isinstance(error, dict) else None
        if (
            isinstance(error, dict)
            and error.get("type") == "NOT_FOUND"
            and isinstance(path, list)
            and len(path) == 1
            and path[0] in alias_keys
        ):
            not_found.add(path[0])
            continue
        raise SyncError(f"GraphQL error: {json.dumps(error, ensure_ascii=False)[:400]}")

    resolved: dict[str, str] = {}
    for alias, key in alias_keys.items():
        if alias not in data:
            raise SyncError(f"GraphQL response is missing alias {alias}")
        value = data[alias]
        if value is None:
            if alias not in not_found:
                raise SyncError(f"GraphQL alias {alias} is null without a NOT_FOUND error")
            resolved[key] = "unknown"
        elif isinstance(value, dict) and isinstance(value.get("isArchived"), bool):
            resolved[key] = "yes" if value["isArchived"] else "no"
        else:
            raise SyncError(
                f"GraphQL alias {alias} returned unexpected data: {json.dumps(value, ensure_ascii=False)[:200]}"
            )
    return resolved


def fetch_archived_statuses(
    slugs: list[str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    run_gh_fn: Callable[..., subprocess.CompletedProcess[str]] = run_gh,
) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for offset in range(0, len(slugs), BATCH_SIZE):
        if offset:
            sleep(BATCH_DELAY_SECONDS)
        batch = slugs[offset : offset + BATCH_SIZE]
        query, alias_keys = build_archived_query(batch)
        proc = run_gh_fn(["api", "graphql", "--input", "-"], input_data=json.dumps({"query": query}))
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise SyncError(
                f"GraphQL batch failed (exit {proc.returncode}): stdout is not JSON; "
                f"stderr={proc.stderr.strip()[:400]!r}"
            ) from exc
        statuses.update(interpret_archived_payload(payload, alias_keys))
    return statuses


# -------------------------------------------------------------- pipeline ----


def sync(
    output_dir: Path,
    *,
    snapshot: str | None = None,
    fetch_snapshot_fn: Callable[[], FetchedSnapshot] = fetch_snapshot,
    fetch_statuses_fn: Callable[[list[str]], dict[str, str]] = fetch_archived_statuses,
) -> SyncReport:
    if snapshot is None:
        snapshot = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fetched = fetch_snapshot_fn()
    try:
        text = fetched.readme_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SyncError(f"upstream README.zh.md is not valid UTF-8: {exc}") from exc
    document = parse_readme(text)
    slugs = unique_repo_slugs(document)
    print(f"upstream {UPSTREAM_REPO}@{UPSTREAM_BRANCH} commit {fetched.sha}")
    print(f"parsed {len(document.plugins)} entries / {len(slugs)} repositories / {len(document.categories)} categories")
    statuses = fetch_statuses_fn(slugs)
    counts = {"yes": 0, "no": 0, "unknown": 0}
    for plugin in document.plugins:
        counts[statuses[plugin.key]] += 1
    files, catalog_index = generate_package(
        document, fetched.readme_bytes, fetched.license_bytes, fetched.sha, snapshot, statuses
    )
    write_outputs(Path(output_dir), files)
    written = (Path(output_dir) / "upstream" / "README.zh.md").read_bytes()
    digest = sha256(written)
    if digest != sha256(fetched.readme_bytes):
        raise SyncError("written upstream/README.zh.md does not match the downloaded bytes")
    pages = sum(len(entry.pages) for entry in catalog_index)
    print(f"pages {pages} (max {PAGE_MAX_ITEMS} entries / {PAGE_MAX_BYTES} bytes each)")
    print(f"archived yes={counts['yes']} no={counts['no']} unknown={counts['unknown']}")
    return SyncReport(
        sha=fetched.sha,
        items=len(document.plugins),
        repos=len(slugs),
        categories=len(document.categories),
        pages=pages,
        archived_counts=counts,
        upstream_sha256=digest,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync the awesome-dsh-plugin status mirror")
    parser.add_argument("--output-dir", default=".", help="directory that receives the generated files (default: .)")
    args = parser.parse_args(argv)
    try:
        report = sync(Path(args.output_dir))
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"source commit {report.sha} sha256 {report.upstream_sha256}")
    print(f"items {report.items} repos {report.repos} categories {report.categories} pages {report.pages}")
    print(f"archived yes={report.archived_counts['yes']} no={report.archived_counts['no']} "
          f"unknown={report.archived_counts['unknown']}")
    print("sync ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
