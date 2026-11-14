"""Unit tests for scripts/sync.py (no network access, fixed inputs)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sync", ROOT / "scripts" / "sync.py")
assert _spec is not None and _spec.loader is not None
sync = importlib.util.module_from_spec(_spec)
sys.modules["sync"] = sync
_spec.loader.exec_module(sync)

SHA = "0123456789abcdef0123456789abcdef01234567"
SNAPSHOT = "2026-10-09 00:00 UTC"


def item(owner_repo: str, description: str = "描述句子。", url: str | None = None, name: str | None = None) -> str:
    return f"- [{name or owner_repo}]({url or f'https://github.com/{owner_repo}'}) — {description}"


def make_readme(
    categories: list[tuple[str, list[str]]],
    *,
    header: list[str] | None = None,
    footer: list[str] | None = None,
) -> str:
    lines = list(header) if header is not None else ["# Fake 标题", "", "[English](README.md)", ""]
    lines.append("<!-- BEGIN PLUGINS -->")
    for title, entries in categories:
        lines.append(f"### {title}")
        lines.append("")
        lines.extend(entries)
    lines.append("<!-- END PLUGINS -->")
    lines.extend(list(footer) if footer is not None else ["", "[贡献指南](contributing.md#anchor)"])
    return "\n".join(lines) + "\n"


def statuses_for(document: sync.ParsedDocument, value: str = "no") -> dict[str, str]:
    return {plugin.key: value for plugin in document.plugins}


class ParseTests(unittest.TestCase):
    def test_parses_categories_and_entries(self) -> None:
        document = sync.parse_readme(make_readme([("A", [item("o/a"), item("o/b")]), ("B", [item("o/c")])]))
        self.assertEqual([category.title for category in document.categories], ["A", "B"])
        self.assertEqual([plugin.slug for plugin in document.plugins], ["o/a", "o/b", "o/c"])

    def test_missing_markers_fail(self) -> None:
        with self.assertRaises(sync.SyncError):
            sync.parse_readme("# nothing here\n")
        with self.assertRaises(sync.SyncError):
            sync.parse_readme("<!-- BEGIN PLUGINS -->\n### A\n" + item("o/a") + "\n")

    def test_markers_out_of_order_fail(self) -> None:
        with self.assertRaises(sync.SyncError):
            sync.parse_readme("<!-- END PLUGINS -->\n<!-- BEGIN PLUGINS -->\n")

    def test_duplicate_marker_fails(self) -> None:
        text = "<!-- BEGIN PLUGINS -->\n<!-- BEGIN PLUGINS -->\n<!-- END PLUGINS -->\n"
        with self.assertRaises(sync.SyncError):
            sync.parse_readme(text)

    def test_unexpected_line_between_markers_fails(self) -> None:
        text = "<!-- BEGIN PLUGINS -->\n### A\nstray text\n" + item("o/a") + "\n<!-- END PLUGINS -->\n"
        with self.assertRaisesRegex(sync.SyncError, "unexpected content"):
            sync.parse_readme(text)

    def test_entry_before_category_fails(self) -> None:
        text = "<!-- BEGIN PLUGINS -->\n" + item("o/a") + "\n<!-- END PLUGINS -->\n"
        with self.assertRaisesRegex(sync.SyncError, "before the first category"):
            sync.parse_readme(text)

    def test_empty_region_fails(self) -> None:
        with self.assertRaisesRegex(sync.SyncError, "no category headings"):
            sync.parse_readme("<!-- BEGIN PLUGINS -->\n<!-- END PLUGINS -->\n")

    def test_empty_category_fails(self) -> None:
        with self.assertRaisesRegex(sync.SyncError, "no entries"):
            sync.parse_readme("<!-- BEGIN PLUGINS -->\n### A\n<!-- END PLUGINS -->\n")

    def test_non_github_link_fails(self) -> None:
        text = "<!-- BEGIN PLUGINS -->\n### A\n- [o/a](https://gitlab.com/o/a) — d\n<!-- END PLUGINS -->\n"
        with self.assertRaisesRegex(sync.SyncError, "unsupported plugin entry syntax"):
            sync.parse_readme(text)

    def test_short_github_url_fails(self) -> None:
        text = "<!-- BEGIN PLUGINS -->\n### A\n- [o](https://github.com/o) — d\n<!-- END PLUGINS -->\n"
        with self.assertRaisesRegex(sync.SyncError, "no owner/repo path"):
            sync.parse_readme(text)

    def test_marker_line_reported_with_line_number(self) -> None:
        text = "<!-- BEGIN PLUGINS -->\n### A\n- broken\n<!-- END PLUGINS -->\n"
        with self.assertRaisesRegex(sync.SyncError, "line 3"):
            sync.parse_readme(text)

    def test_subdirectory_entries_map_to_repo_root(self) -> None:
        text = make_readme(
            [
                (
                    "A",
                    [
                        item("O/R"),
                        item("O/R#pkg", url="https://github.com/O/R/tree/main/packages/x"),
                        item("o/r", url="https://github.com/O/R.git"),
                    ],
                )
            ]
        )
        document = sync.parse_readme(text)
        self.assertEqual([plugin.slug for plugin in document.plugins], ["O/R", "O/R", "O/R"])
        self.assertEqual(sync.unique_repo_slugs(document), ["O/R"])
        self.assertEqual(document.plugins[1].raw, item("O/R#pkg", url="https://github.com/O/R/tree/main/packages/x"))

    def test_already_badged_entry_is_rejected(self) -> None:
        badged = item("o/a") + f" {sync.badge_markdown('o/a', 'no')}"
        with self.assertRaisesRegex(sync.SyncError, "already carries status badges"):
            sync.parse_readme(make_readme([("A", [badged])]))

    def test_blank_lines_are_preserved(self) -> None:
        text = make_readme([("A", [item("o/a")])])
        document = sync.parse_readme(text)
        self.assertEqual("\n".join(document.lines), text)


class ArchivedPayloadTests(unittest.TestCase):
    def test_partial_not_found_marks_unknown_and_keeps_valid(self) -> None:
        aliases = {"r0": "o/a", "r1": "o/b"}
        payload = {
            "data": {"r0": {"isArchived": True}, "r1": None},
            "errors": [{"type": "NOT_FOUND", "path": ["r1"], "message": "Could not resolve to a Repository"}],
        }
        self.assertEqual(sync.interpret_archived_payload(payload, aliases), {"o/a": "yes", "o/b": "unknown"})

    def test_false_maps_to_no(self) -> None:
        payload = {"data": {"r0": {"isArchived": False}}}
        self.assertEqual(sync.interpret_archived_payload(payload, {"r0": "o/a"}), {"o/a": "no"})

    def test_query_aliases_and_json_escaped_names(self) -> None:
        query, aliases = sync.build_archived_query(['o"x/a', "b/c"])
        self.assertEqual(aliases, {"r0": 'o"x/a', "r1": "b/c"})
        self.assertIn('owner:"o\\"x"', query)
        self.assertIn('name:"a"', query)

    def test_rate_limit_error_aborts(self) -> None:
        payload = {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
        with self.assertRaisesRegex(sync.SyncError, "RATE_LIMITED"):
            sync.interpret_archived_payload(payload, {"r0": "o/a"})

    def test_unexplained_null_aborts(self) -> None:
        with self.assertRaisesRegex(sync.SyncError, "null without a NOT_FOUND"):
            sync.interpret_archived_payload({"data": {"r0": None}}, {"r0": "o/a"})

    def test_missing_alias_aborts(self) -> None:
        with self.assertRaisesRegex(sync.SyncError, "missing alias"):
            sync.interpret_archived_payload({"data": {}}, {"r0": "o/a"})

    def test_not_found_for_unknown_alias_aborts(self) -> None:
        payload = {"data": {"r0": {"isArchived": False}}, "errors": [{"type": "NOT_FOUND", "path": ["r9"]}]}
        with self.assertRaisesRegex(sync.SyncError, "NOT_FOUND"):
            sync.interpret_archived_payload(payload, {"r0": "o/a"})


class FetchStatusesTests(unittest.TestCase):
    @staticmethod
    def _fake_runner(calls: list[dict[str, object]], archived: bool = False):
        def runner(args: list[str], *, input_data: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
            assert input_data is not None
            calls.append(json.loads(input_data))
            data = {
                match.group(1): {"isArchived": archived}
                for match in re.finditer(r"(r\d+): repository\(", input_data)
            }
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": data}), "")

        return runner

    def test_batches_of_fifty_with_one_second_delay(self) -> None:
        calls: list[dict[str, object]] = []
        sleeps: list[float] = []
        slugs = [f"o{i}/r{i}" for i in range(120)]
        statuses = sync.fetch_archived_statuses(
            slugs, sleep=sleeps.append, run_gh_fn=self._fake_runner(calls)
        )
        self.assertEqual([len(re.findall(r": repository\(", json.dumps(call))) for call in calls], [50, 50, 20])
        self.assertEqual(sleeps, [1.0, 1.0])
        self.assertEqual(len(statuses), 120)
        self.assertEqual(statuses["o7/r7"], "no")

    def test_non_json_output_aborts(self) -> None:
        def runner(args: list[str], *, input_data: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 401: Bad credentials")

        with self.assertRaisesRegex(sync.SyncError, "stdout is not JSON"):
            sync.fetch_archived_statuses(["o/a"], run_gh_fn=runner)


class PaginationTests(unittest.TestCase):
    def test_101_entries_split_100_plus_1_and_keep_order(self) -> None:
        lines = [f"entry {index}" for index in range(101)]
        pages = sync.paginate(lines, "header")
        self.assertEqual([len(page) for page in pages], [100, 1])
        self.assertEqual(pages[0][0], "entry 0")
        self.assertEqual(pages[0][-1], "entry 99")
        self.assertEqual(pages[1], ["entry 100"])

    def test_byte_budget_splits_early(self) -> None:
        lines = ["x" * 10 for _ in range(5)]
        max_bytes = sync.NAV_RESERVE_BYTES + 1 + 22
        pages = sync.paginate(lines, "h", max_bytes=max_bytes)
        self.assertEqual([len(page) for page in pages], [2, 2, 1])

    def test_single_entry_over_byte_budget_fails(self) -> None:
        with self.assertRaisesRegex(sync.SyncError, "single entry exceeds"):
            sync.paginate(["x" * 1000], "h", max_bytes=sync.NAV_RESERVE_BYTES + 10)

    def test_item_limit_respected_within_byte_budget(self) -> None:
        pages = sync.paginate([f"e{index}" for index in range(250)], "h")
        self.assertEqual([len(page) for page in pages], [100, 100, 50])

    def test_preamble_counted_in_page_budget(self) -> None:
        lines = ["x" * 10 for _ in range(5)]
        preamble, postamble = sync.TABLE_OPEN + "\n", sync.TABLE_CLOSE + "\n"
        max_bytes = (
            sync.NAV_RESERVE_BYTES
            + 1
            + len(preamble.encode("utf-8"))
            + len(postamble.encode("utf-8"))
            + 22
        )
        pages = sync.paginate(lines, "h", preamble=preamble, postamble=postamble, max_bytes=max_bytes)
        self.assertEqual([len(page) for page in pages], [2, 2, 1])


class BadgeTests(unittest.TestCase):
    def test_four_badges_with_cache_seconds(self) -> None:
        document = sync.parse_readme(make_readme([("A", [item("O/R")])]))
        line = sync.render_item_line(document.plugins[0], "no")
        urls = sync.OUR_BADGE_URL_RE.findall(line)
        self.assertTrue(line.startswith(document.plugins[0].raw + " "))
        self.assertEqual(len(urls), 4)
        for url in urls:
            self.assertIn("cacheSeconds=86400", url)
        self.assertIn("github/last-commit/O/R?display_timestamp=committer", urls[0])
        self.assertIn("github/created-at/O/R?cacheSeconds=86400", urls[1])
        self.assertIn("github/release-date-pre/O/R?display_date=published_at", urls[2])
        self.assertIn("badge/archived-no-brightgreen", urls[3])

    def test_archived_labels(self) -> None:
        self.assertIn("badge/archived-yes-red", sync.badge_markdown("o/a", "yes"))
        self.assertIn("badge/archived-no-brightgreen", sync.badge_markdown("o/a", "no"))
        self.assertIn("badge/archived-unknown-lightgrey", sync.badge_markdown("o/a", "unknown"))
        with self.assertRaises(sync.SyncError):
            sync.badge_markdown("o/a", "maybe")

    def test_description_links_do_not_add_badges(self) -> None:
        raw = item("o/a", description="参考 https://github.com/o/b 与 [x](https://github.com/o/c)。")
        document = sync.parse_readme(make_readme([("A", [raw])]))
        line = sync.render_item_line(document.plugins[0], "no")
        self.assertEqual(len(sync.OUR_BADGE_URL_RE.findall(line)), 4)
        self.assertEqual(len(re.findall(r"github\.com/o/[bc]", line)), 2)
        self.assertEqual(document.plugins[0].slug, "o/a")


class LinkRewriteTests(unittest.TestCase):
    def test_readme_and_contributing_targets(self) -> None:
        base = f"https://github.com/{sync.UPSTREAM_REPO}/blob/{SHA}"
        self.assertEqual(sync.rewrite_relative_links("[English](README.md)", SHA), f"[English]({base}/README.md)")
        self.assertEqual(
            sync.rewrite_relative_links("[评审](contributing.md#how--收录)", SHA),
            f"[评审]({base}/contributing.md#how--收录)",
        )

    def test_other_targets_untouched(self) -> None:
        for line in ("[x](dsh-session:…)", "[x](#锚点)", "[x](https://example.com/README.md)", "普通文本"):
            self.assertEqual(sync.rewrite_relative_links(line, SHA), line)

    def test_fenced_code_is_not_rewritten(self) -> None:
        text = make_readme(
            [("A", [item("o/a")])],
            header=[
                "# Fake 标题",
                "",
                "[English](README.md)",
                "",
                "```markdown",
                "[English](README.md)",
                "```",
                "",
            ],
        )
        document = sync.parse_readme(text)
        enhanced = sync.render_readme_zh(document, SHA, SNAPSHOT, statuses_for(document))
        lines = enhanced.split("\n")
        fence_start = lines.index("```markdown")
        self.assertEqual(lines[fence_start : fence_start + 3], ["```markdown", "[English](README.md)", "```"])
        self.assertEqual(
            enhanced.count(f"](https://github.com/{sync.UPSTREAM_REPO}/blob/{SHA}/README.md)"),
            1,
        )


class EnhancedReadmeTests(unittest.TestCase):
    def test_additions_only(self) -> None:
        text = make_readme([("A", [item("o/a"), item("o/b")])])
        document = sync.parse_readme(text)
        enhanced = sync.render_readme_zh(document, SHA, SNAPSHOT, statuses_for(document))
        lines = enhanced.split("\n")
        inserted = lines[1:5]
        self.assertEqual(inserted[0], "")
        self.assertTrue(all(line.startswith("> ") for line in inserted[1:]))
        self.assertEqual(inserted[1], f"> **非官方镜像。** 本文件由 [awesome-dsh-plugin-status]({sync.MIRROR_REPO_URL}) 自动生成，正文来自上游"
                         f" [`{sync.UPSTREAM_REPO}`]({sync.UPSTREAM_REPO_URL}) 的 `{sync.UPSTREAM_BRANCH}` 分支 commit"
                         f" [`{SHA[:7]}`](https://github.com/{sync.UPSTREAM_REPO}/commit/{SHA})，内容、顺序与空白保持原样。")
        stripped = [line.split(" ![last commit]")[0] for line in lines[5:]]
        rewritten = [sync.rewrite_relative_links(line, SHA) for line in document.lines[1:]]
        self.assertEqual(stripped, rewritten)

    def test_generation_is_idempotent_and_not_stacked(self) -> None:
        document = sync.parse_readme(make_readme([("A", [item("o/a")])]))
        first = sync.render_readme_zh(document, SHA, SNAPSHOT, statuses_for(document))
        second = sync.render_readme_zh(document, SHA, SNAPSHOT, statuses_for(document))
        self.assertEqual(first, second)
        self.assertEqual(first.count("![last commit]"), 1)
        self.assertEqual(first.count("![created at]("), 1)
        self.assertEqual(first.count("![release date]"), 1)
        self.assertEqual(first.count("![release]("), 0)
        self.assertEqual(first.count("![archived]"), 1)

    def test_unknown_script_target_preserved(self) -> None:
        raw = item("lacemou/dsh-session-ref", description="粘贴 @[label](dsh-session:…) 引用。")
        document = sync.parse_readme(make_readme([("A", [raw])]))
        enhanced = sync.render_readme_zh(document, SHA, SNAPSHOT, statuses_for(document))
        self.assertIn("(dsh-session:…)", enhanced)


class PackageTests(unittest.TestCase):
    def _document(self, categories: list[tuple[str, list[str]]]) -> sync.ParsedDocument:
        return sync.parse_readme(make_readme(categories))

    def test_catalog_covers_entries_once_and_respects_limits(self) -> None:
        document = self._document([("A", [item(f"o{i}/a") for i in range(101)]), ("B", [item("o/b")])])
        statuses = statuses_for(document)
        files, index = sync.generate_package(document, b"RAW\n", b"LICENSE\n", SHA, SNAPSHOT, statuses)
        self.assertEqual(files["upstream/README.zh.md"], b"RAW\n")
        self.assertEqual(files["LICENSE"], b"LICENSE\n")
        self.assertEqual([entry.pages for entry in index], [("catalog/c01-p001.md", "catalog/c01-p002.md"),
                                                            ("catalog/c02-p001.md",)])
        self.assertEqual([entry.count for entry in index], [101, 1])
        counts = []
        for path in [path for entry in index for path in entry.pages]:
            blob = files[path]
            self.assertLessEqual(len(blob), sync.PAGE_MAX_BYTES)
            entries = [
                line for line in blob.decode("utf-8").split("\n") if line.startswith(sync.TABLE_ROW_PREFIX)
            ]
            self.assertLessEqual(len(entries), sync.PAGE_MAX_ITEMS)
            counts.append(len(entries))
        self.assertEqual(counts, [100, 1, 1])
        self.assertEqual(sum(counts), len(document.plugins))

    def test_same_root_queried_once_but_badged_twice(self) -> None:
        document = self._document(
            [
                (
                    "A",
                    [
                        item("O/R"),
                        item("O/R#pkg", url="https://github.com/O/R/tree/main/packages/x"),
                    ],
                )
            ]
        )
        self.assertEqual(sync.unique_repo_slugs(document), ["O/R"])
        statuses = statuses_for(document, "yes")
        files, index = sync.generate_package(document, b"RAW\n", b"LICENSE\n", SHA, SNAPSHOT, statuses)
        page = files["catalog/c01-p001.md"].decode("utf-8")
        self.assertEqual(len(sync.OUR_BADGE_URL_RE.findall(page)), 8)
        self.assertEqual(page.count("badge/archived-yes-red"), 2)
        self.assertEqual(page.count("github/last-commit/O/R?"), 2)
        self.assertEqual(page.count("/O/R?"), 6)
        enhanced = files["README.zh.md"].decode("utf-8")
        self.assertEqual(len(sync.OUR_BADGE_URL_RE.findall(enhanced)), 8)

    def test_missing_status_fails(self) -> None:
        document = self._document([("A", [item("o/a")])])
        with self.assertRaisesRegex(sync.SyncError, "missing archived status"):
            sync.generate_package(document, b"RAW\n", b"LICENSE\n", SHA, SNAPSHOT, {})

    def test_entry_readme_links_every_page_and_source(self) -> None:
        document = self._document([("A", [item("o/a")]), ("B", [item("o/b")])])
        statuses = statuses_for(document)
        files, index = sync.generate_package(document, b"RAW\n", b"LICENSE\n", SHA, SNAPSHOT, statuses)
        entry = files["README.md"].decode("utf-8")
        for entry_pages in index:
            for path in entry_pages.pages:
                self.assertIn(f"]({path})", entry)
        self.assertIn(f"https://github.com/{sync.UPSTREAM_REPO}/commit/{SHA}", entry)
        self.assertIn("catalog/", entry)

    def test_write_outputs_removes_only_stale_catalog_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp)
            sync.write_outputs(
                output,
                {"README.md": b"a", "upstream/README.zh.md": b"raw", "catalog/c01-p001.md": b"p1",
                 "catalog/c01-p002.md": b"p2"},
            )
            (output / "catalog" / "notes.txt").write_text("keep", encoding="utf-8")
            sync.write_outputs(
                output,
                {"README.md": b"b", "upstream/README.zh.md": b"raw2", "catalog/c01-p001.md": b"p1b"},
            )
            self.assertFalse((output / "catalog" / "c01-p002.md").exists())
            self.assertTrue((output / "catalog" / "notes.txt").exists())
            self.assertEqual((output / "catalog" / "c01-p001.md").read_bytes(), b"p1b")
            self.assertEqual((output / "README.md").read_bytes(), b"b")
            self.assertEqual((output / "upstream" / "README.zh.md").read_bytes(), b"raw2")


class CatalogTableTests(unittest.TestCase):
    def _package(self, raw_item: str, archived: str = "no") -> tuple[sync.Plugin, str, dict[str, bytes]]:
        document = sync.parse_readme(make_readme([("A", [raw_item])]))
        plugin = document.plugins[0]
        files, _ = sync.generate_package(document, b"RAW\n", b"LICENSE\n", SHA, SNAPSHOT, {plugin.key: archived})
        return plugin, files["catalog/c01-p001.md"].decode("utf-8"), files

    def _block(self, page: str) -> tuple[str, str]:
        body = page.split(sync.TABLE_OPEN, 1)[1].split(sync.TABLE_CLOSE, 1)[0].strip("\n").split("\n")
        self.assertEqual(len(body), 2)
        return body[0], body[1]

    def test_block_layout_name_and_badges_then_full_width_description(self) -> None:
        _, page, _ = self._package(item("o/a", description="一句话简介。"))
        head, description = self._block(page)
        self.assertEqual(
            head,
            sync.TABLE_ROW_PREFIX
            + '<a href="https://github.com/o/a">o/a</a></td><td>'
            + sync.badge_cell("o/a", "no")
            + "</td></tr>",
        )
        self.assertEqual(description, f"<tr>{sync.DESCRIPTION_CELL}一句话简介。</td></tr>")
        self.assertTrue(page.startswith("## A\n"))
        self.assertIn(sync.TABLE_OPEN, page)
        self.assertTrue(page.endswith(f"\n{sync.TABLE_CLOSE}\n"))

    def test_badges_are_four_unscaled_images_one_per_line(self) -> None:
        _, page, _ = self._package(item("o/a"))
        head, _ = self._block(page)
        self.assertEqual(head.count("<img"), 4)
        self.assertEqual(head.count("<br>"), 3)  # one break between each pair of badges
        self.assertEqual(head.count("</td>"), 2)
        for alt, url in zip(sync.BADGE_ALTS, sync.badge_urls("o/a", "no")):
            self.assertIn(f'<img src="{url}" alt="{alt}">', head)
            self.assertIn("cacheSeconds=86400", url)
        self.assertEqual([alt for alt in sync.BADGE_ALTS], [re.search(r'alt="([^"]+)"', part).group(1)
                                                           for part in head.split("<br>")])

    def test_description_inline_markdown_converted_and_html_escaped(self) -> None:
        description = "命令 `dsh-progress run -- <cmd>` 与 [文档](https://example.com/a?x=1&y=2) 及 **粗体**，还有 <br> 与 H&P。"
        _, page, _ = self._package(item("o/a", description=description))
        _, body = self._block(page)
        self.assertIn("<code>dsh-progress run -- &lt;cmd&gt;</code>", body)
        self.assertIn('<a href="https://example.com/a?x=1&amp;y=2">文档</a>', body)
        self.assertIn("<strong>粗体</strong>", body)
        self.assertIn("&lt;br&gt;", body)
        self.assertIn("H&amp;P", body)
        self.assertNotIn("<cmd>", body)

    def test_bare_url_autolinked(self) -> None:
        _, page, _ = self._package(item("o/a", description="见 https://example.com/docs 说明。"))
        _, body = self._block(page)
        self.assertIn('<a href="https://example.com/docs">https://example.com/docs</a>', body)

    def test_intraword_underscores_and_wildcards_stay_literal(self) -> None:
        description = "注册为 mcp__siyuan__<tool>，路由 /api/media-preview/* 与 mcp__cue_<domain>__* 保持原样。"
        _, page, _ = self._package(item("o/a", description=description))
        _, body = self._block(page)
        self.assertIn("mcp__siyuan__&lt;tool&gt;", body)
        self.assertIn("/api/media-preview/*", body)
        self.assertIn("mcp__cue_&lt;domain&gt;__*", body)
        self.assertNotIn("<tool>", body)

    def test_display_name_and_url_kept_verbatim(self) -> None:
        raw = item(
            "Jonah-Wu23/dsh-gungnir#dsh-plugin",
            url="https://github.com/Jonah-Wu23/dsh-gungnir/tree/main/packages/dsh-plugin",
        )
        _, page, _ = self._package(raw)
        head, _ = self._block(page)
        self.assertTrue(
            head.startswith(
                sync.TABLE_ROW_PREFIX
                + '<a href="https://github.com/Jonah-Wu23/dsh-gungnir/tree/main/packages/dsh-plugin">'
                + "Jonah-Wu23/dsh-gungnir#dsh-plugin</a>"
            )
        )

    def test_archived_label_reflected_in_row(self) -> None:
        for archived, label in (
            ("yes", "archived-yes-red"),
            ("no", "archived-no-brightgreen"),
            ("unknown", "archived-unknown-lightgrey"),
        ):
            _, page, _ = self._package(item("o/a"), archived)
            self.assertIn(label, self._block(page)[0])

    def test_full_text_mirror_keeps_markdown_list_and_single_line_badges(self) -> None:
        raw = item("o/a")
        _, _, files = self._package(raw)
        enhanced = files["README.zh.md"].decode("utf-8")
        self.assertIn(f"{raw} {sync.badge_markdown('o/a', 'no')}", enhanced)
        self.assertNotIn("<img", enhanced)
        self.assertNotIn("<table", enhanced)

    def test_every_page_wraps_items_in_exactly_one_table(self) -> None:
        document = sync.parse_readme(make_readme([("A", [item(f"o{i}/a") for i in range(101)])]))
        statuses = statuses_for(document)
        files, index = sync.generate_package(document, b"RAW\n", b"LICENSE\n", SHA, SNAPSHOT, statuses)
        self.assertEqual(index[0].pages, ("catalog/c01-p001.md", "catalog/c01-p002.md"))
        for path in index[0].pages:
            page = files[path].decode("utf-8")
            self.assertEqual(page.count(sync.TABLE_OPEN), 1)
            self.assertEqual(page.count(sync.TABLE_CLOSE), 1)
            self.assertEqual(page.count(sync.DESCRIPTION_CELL), page.count(sync.TABLE_ROW_PREFIX))


class SyncPipelineTests(unittest.TestCase):
    def test_sync_with_injected_network(self) -> None:
        text = make_readme(
            [("A", [item("O/R"), item("O/R#pkg", url="https://github.com/O/R/tree/main/packages/x")])]
        )
        seen: list[list[str]] = []

        def fetch() -> sync.FetchedSnapshot:
            return sync.FetchedSnapshot(sha=SHA, readme_bytes=text.encode("utf-8"), license_bytes=b"LICENSE\n")

        def statuses(slugs: list[str]) -> dict[str, str]:
            seen.append(list(slugs))
            return {slug.lower(): "unknown" for slug in slugs}

        with tempfile.TemporaryDirectory() as tmp:
            report = sync.sync(tmp, snapshot=SNAPSHOT, fetch_snapshot_fn=fetch, fetch_statuses_fn=statuses)
            self.assertEqual(seen, [["O/R"]])
            self.assertEqual((report.items, report.repos, report.categories, report.pages), (2, 1, 1, 1))
            self.assertEqual(report.archived_counts, {"yes": 0, "no": 0, "unknown": 2})
            self.assertEqual(pathlib.Path(tmp, "upstream", "README.zh.md").read_bytes(), text.encode("utf-8"))
            entry = pathlib.Path(tmp, "README.md").read_text(encoding="utf-8")
            self.assertIn(f"`{SHA[:7]}`", entry)
            self.assertIn("badge/archived-unknown-lightgrey", pathlib.Path(tmp, "README.zh.md").read_text(encoding="utf-8"))

    def test_bad_utf8_fails(self) -> None:
        def fetch() -> sync.FetchedSnapshot:
            return sync.FetchedSnapshot(sha=SHA, readme_bytes=b"\xff\xfe", license_bytes=b"")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(sync.SyncError, "not valid UTF-8"):
                sync.sync(tmp, snapshot=SNAPSHOT, fetch_snapshot_fn=fetch, fetch_statuses_fn=lambda slugs: {})


if __name__ == "__main__":
    unittest.main()
