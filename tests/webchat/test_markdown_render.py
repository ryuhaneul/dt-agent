"""Pinned standard GFM rendering, display policy and security regressions.

Run with cmarkgfm installed in the test environment (no runtime dependency).
Raw HTML is exercised by security tests; the differential corpus includes HTML
as escaped text and code, since cmark's safe renderer omits raw HTML whereas
micromark escapes it: https://github.com/micromark/micromark#security
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from cmarkgfm.cmark import Options, markdown_to_html_with_extensions

ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).with_name("markdown_render.mjs")
VENDOR = ROOT / "ductor_bot/webchat/web/static/js/vendor/micromark-gfm.js"
VENDOR_SHA256 = "1edc4ec68814249b9e988e0f9236a9827e2477e910fc361f629a6791034eb1b3"
SEEDS = (939187043, 482063701, 615349129)
CASES_PER_SEED = 7000
SAFE_HREF = re.compile(r"^(?:https?|ircs?|mailto|xmpp):|^#", re.IGNORECASE)
BLOCKS = {"root", "table", "thead", "tbody", "tr", "ul", "ol", "li", "blockquote", "div"}
VOID = {"img", "input", "hr", "br", "col"}


def render(inputs, *, raw=False):
    result = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps({"inputs": inputs, "raw": raw}),
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return json.loads(result.stdout)


def reference(text):
    # Avoid github_flavored_markdown_to_html's forced pre[lang] HTML convention.
    return markdown_to_html_with_extensions(
        text,
        options=Options.CMARK_OPT_FOOTNOTES,
        extensions=["table", "autolink", "tagfilter", "strikethrough", "tasklist"],
    )


class Tree(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = ["root", {}, []]
        self.stack = [self.root]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = [tag, dict(attrs), []]
        self.stack[-1][2].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        assert self.stack[-1][0] == tag, (tag, self.stack[-1][0])
        self.stack.pop()

    def handle_data(self, text):
        children = self.stack[-1][2]
        if children and isinstance(children[-1], str):
            children[-1] += text
        else:
            children.append(text)


def normalized(html, *, bare_urls=(), strong_contents=()):  # noqa: C901
    """Independent application of display policies 1-4 on either renderer.

    Ignore serialization whitespace between blocks, preserve code verbatim and
    compare all remaining elements, text and semantic attributes (including href).
    """

    def visit(node, parent="root", in_anchor=False, verbatim=False, in_link=False):  # noqa: C901, PLR0912
        if isinstance(node, str):
            if parent in BLOCKS and not node.strip():
                return []
            return [node if verbatim else re.sub(r"\s+", " ", node)]
        tag, attrs, children = node
        attrs = dict(attrs)
        if tag == "img":
            alt, href = attrs.get("alt", ""), attrs.get("src", "")
            if in_link or not re.match(r"^https?:", href, re.IGNORECASE):
                return [alt]
            return visit(
                [
                    "a",
                    {"href": href, **({"title": attrs["title"]} if "title" in attrs else {})},
                    [alt],
                ],
                parent,
            )
        # Only the exact bare URL emitted by the generator may lose a final ].
        # Explicit links and interior brackets are never rewritten.
        trailing = []
        if (
            tag == "a"
            and len(children) == 1
            and isinstance(children[0], str)
            and children[0] in bare_urls
            and children[0].endswith("]")
            and attrs.get("href", "").endswith("%5D")
        ):
            attrs["href"] = attrs["href"][:-3]
            children = [children[0][:-1]]
            trailing = ["]"]
        # Exactly two redundant strong tags around one approved four-star literal.
        if (
            tag == "strong"
            and not attrs
            and len(children) == 1
            and not isinstance(children[0], str)
            and children[0][0] == "strong"
            and not children[0][1]
            and children[0][2] in strong_contents
        ):
            children = children[0][2]
        inside = in_anchor or (tag == "a" and bool(SAFE_HREF.match(attrs.get("href", ""))))
        result = []
        for child in children:
            for item in visit(
                child, tag, inside, verbatim or tag in {"pre", "code"}, in_link or tag == "a"
            ):
                if isinstance(item, str) and result and isinstance(result[-1], str):
                    result[-1] += item
                else:
                    result.append(item)
        if tag == "a" and (in_anchor or not SAFE_HREF.match(attrs.get("href", ""))):
            if "id" in attrs:
                return [("span", (("id", attrs["id"]),), tuple(result))]
            return result
        if tag in {"root", "colgroup", "col"} or (
            tag == "div" and attrs.get("class") == "table-scroll"
        ):
            return result
        for key in ("target", "rel", "tabindex"):
            attrs.pop(key, None)
        if attrs.get("class", "").startswith("align-"):
            attrs["align"] = attrs.pop("class").removeprefix("align-")
        return [(tag, tuple(sorted(attrs.items())), tuple(result)), *trailing]

    return visit(Tree(html).root)


def generated_cases():
    labels = [
        "guide",
        "한글 링크",
        "emoji 🙂",
        "**bold**",
        "*em*",
        "~~strike~~",
        "`code`",
        "**a *b* c**",
        "&copy;",
        "&quot;",
        "a & b",
        r"a\*b",
    ]
    parts = [
        "part",
        "a(b)",
        "q[ready]",
        "%20",
        "%5Bq%5D",
        "🙂",
        "🚀",
        "東京",
        "한글",
        "&x=y",
        "&amp;foo",
        "&copy;",
        "a{b}",
        "foo~bar",
    ]
    for seed in SEEDS:
        rng = random.Random(seed)  # noqa: S311 - reproducible differential corpus
        for index in range(CASES_PER_SEED):
            label = rng.choice(labels)
            suffix = f"{seed}/{index}/{rng.randrange(1_000_000)}"
            url = rng.choice(["https://example.test/", "http://a.b.test/", "HTTPS://example.test/"])
            url += suffix + "/" + "".join(rng.choices(parts, k=rng.randint(1, 4)))
            destination = rng.choice(
                [
                    url,
                    "relative/" + suffix,
                    "#f-" + suffix,
                    "mailto:user@example.test",
                    "irc://example.test/room",
                    "",
                ]
            )
            atom = rng.choice([label, "`<span onclick=x>HTML</span>`", "&lt;b&gt;HTML&lt;/b&gt;"])
            kind = index % 24
            cases = [
                f"한글 🙂 {url}",
                f"({url}) 뒤",
                f"[{label}]({url})",
                f"[{label}](<{url}> 'title {suffix}')",
                f"[{label}]({destination})",
                f"![{label}]({url})",
                f"[![{label}]({url})]({destination})",
                f"# {atom}\n\n## {suffix}\n\n{url}",
                f"- {atom}\n- [{label}]({url})\n  - {suffix}",
                f"1. {atom}\n2. {suffix}\n\n   {url}",
                f"> {atom}\n>\n> [{label}]({url})\n\n{suffix}",
                f"```js\n{url}\n<script>attack('{suffix}')</script>\n```",
                f"~~~\n{atom}\n{url}\n~~~\n\n{suffix}",
                f"    {url}\n    <img src='{suffix}'>\n\n{atom}",
                f"| {atom} | {suffix} |\n| :--- | ---: |\n| [{label}]({url}) | `x` |",
                f"| a | b |\n| - | :-: |\n| {suffix} |\n| x\\|y | {atom} |",
                f"**{label}** *{suffix}* ~~{atom}~~\n\n{url}",
                f'[{label}][ref]\n\n[ref]: {url} "{suffix}"',
                f"[ref][]\n\n[ref]: <{url}>\n\n{suffix}",
                f"`{url}` **{atom}**\n\n{suffix}",
                f"- [x] {atom}\n- [ ] [{label}]({url})\n\n{suffix}",
                f"{suffix}  \n{atom}\n\n---\n\n{url}",
                f"<https://example.test/{suffix}> <user@example.test>\n\n{atom}",
                f"{atom}\n\n{url}\n\n| x |\n| - |\n| {suffix} |",
            ]
            source = cases[kind] + f"\n\ncase {suffix}"
            options = {
                "bare_urls": (url, re.sub(r"(?:&[a-zA-Z]+;)+$", "", url))
                if kind in {0, 1, 7, 9, 16, 21, 23}
                else (),
                "strong_contents": tuple(
                    content
                    for literal, content in (
                        ("****bold****", ["bold"]),
                        ("****a *b* c****", ["a ", ["em", {}, ["b"]], " c"]),
                    )
                    if literal in source
                ),
            }
            yield seed, kind, source, options


def test_seeded_cmark_differential():
    cases = list(generated_cases())
    assert len(cases) >= 20_000
    assert len({text for _, _, text, _ in cases}) == len(cases)
    actual = render([text for _, _, text, _ in cases], raw=True)["output"]
    failures = []
    bracket_count = strong_count = 0
    for (seed, kind, text, options), output in zip(cases, actual, strict=True):
        oracle = reference(text)
        expected = normalized(oracle, **options)
        result = normalized(output["html"], **options)
        policy = normalized(output["raw"], **options)
        bracket_count += normalized(oracle) != normalized(oracle, bare_urls=options["bare_urls"])
        strong_count += normalized(output["raw"]) != normalized(
            output["raw"], strong_contents=options["strong_contents"]
        )
        if result != expected or result != policy:
            failures.append((seed, kind, text, expected, result, policy))
    assert not failures, f"{len(failures)} differences; first 5: {failures[:5]!r}"
    assert bracket_count == 481
    assert strong_count == 219


# Known upstream differences are exact fixtures, never a blanket skip for inputs
# containing a bracket/footnote/escape. The grammar excludes bare ftp, and email
# autolinks recognize the address rather than including a preceding mailto:.
# https://github.com/micromark/micromark-extension-gfm-autolink-literal#syntax
# https://github.com/micromark/micromark-extension-gfm-autolink-literal#bugs
# https://github.com/micromark/micromark-extension-gfm-table#bugs
# https://github.com/micromark/micromark-extension-gfm-footnote#html
# Additional differences approved after the first 21,000-case run:
# - Bare final ]: 481 cases, e.g. https://example.test/q[ready]. micromark
#   leaves the last ] outside the anchor; cmark-gfm includes it.
#   Locked autolink dependency 2.1.0 source and differential fixtures:
#   https://github.com/micromark/micromark-extension-gfm-autolink-literal/blob/2.1.0/dev/lib/syntax.js#L706-L751
#   https://github.com/micromark/micromark-extension-gfm-autolink-literal/blob/2.1.0/test/index.js#L417-L442
# - Redundant four-star literals: 219 cases, e.g. ****bold**** and
#   ****a *b* c****. micromark emits
#   strong/strong; cmark-gfm coalesces the redundant strong. Only the exact
#   source literal and its unchanged child subtree are eligible; em/strong differences remain failures.
#   https://github.com/micromark/micromark/blob/4.0.3/test/io/text/emphasis.js
#   https://spec.commonmark.org/0.31.2/#emphasis-and-strong-emphasis
# Eight cases contain both differences (692 total mismatching cases).
# The bracket bug (#278) is documented upstream but remains in pinned 3.0.0;
# retain its observed behavior explicitly instead of claiming a parsing change.
KNOWN_DIFFERENCES = [
    (
        "WWW.example.com",
        '<p><a href="http://WWW.example.com">WWW.example.com</a></p>',
        "<p>WWW.example.com</p>",
    ),
    (
        "ftp://example.com",
        "<p>ftp://example.com</p>",
        '<p><a href="ftp://example.com">ftp://example.com</a></p>',
    ),
    (
        "mailto:hello@example.com",
        '<p>mailto:<a href="mailto:hello@example.com">hello@example.com</a></p>',
        '<p><a href="mailto:hello@example.com">mailto:hello@example.com</a></p>',
    ),
    ("[https://example.com", "<p>[https://example.com</p>", "<p>[https://example.com</p>"),
    (
        "https://example.test/q[ready]",
        '<p><a href="https://example.test/q%5Bready">https://example.test/q[ready</a>]</p>',
        '<p><a href="https://example.test/q%5Bready%5D">https://example.test/q[ready]</a></p>',
    ),
    (
        "****bold****",
        "<p><strong><strong>bold</strong></strong></p>",
        "<p><strong>bold</strong></p>",
    ),
]


@pytest.mark.parametrize(("source", "micromark_html", "cmark_html"), KNOWN_DIFFERENCES)
def test_known_autolink_differences(source, micromark_html, cmark_html):
    output = render([source], raw=True)["output"][0]
    assert output["raw"].strip() == micromark_html
    assert reference(source).strip() == cmark_html
    assert normalized(output["html"]) == normalized(micromark_html)


def test_known_normalization_does_not_hide_other_differences():
    url = "https://example.test/q[ready]"
    included = '<p><a href="https://example.test/q%5Bready%5D">' + url + "</a></p>"
    excluded = '<p><a href="https://example.test/q%5Bready">' + url[:-1] + "</a>]</p>"
    assert normalized(included, bare_urls=(url,)) == normalized(excluded, bare_urls=(url,))
    # An explicit link is not eligible, even if its label resembles a bare URL.
    assert normalized(included) != normalized(excluded)
    middle = '<p><a href="https://example.test/q%5Bready%5D/more">' + url + "/more</a></p>"
    broken = middle.replace("%5D/more", "/more").replace("]/more</a>", "/more</a>]")
    assert normalized(middle, bare_urls=(url + "/more",)) != normalized(
        broken, bare_urls=(url + "/more",)
    )
    # A changed destination or label cannot be excused by the trailing-bracket rule.
    assert normalized(included, bare_urls=(url,)) != normalized(
        excluded.replace('href="https:', 'href="http:'), bare_urls=(url,)
    )
    nested = "<p><strong><strong>bold</strong></strong></p>"
    single = "<p><strong>bold</strong></p>"
    assert normalized(nested, strong_contents=(["bold"],)) == normalized(single)
    assert normalized(nested) != normalized(single)
    assert normalized(
        nested.replace("<strong>bold", "<em>bold").replace("bold</strong>", "bold</em>"),
        strong_contents=(["bold"],),
    ) != normalized(single)
    assert normalized(nested.replace("bold", "other"), strong_contents=(["bold"],)) != normalized(
        single.replace("bold", "other")
    )


def test_footnote_fragments_and_known_output_difference():
    # Footnote HTML is specified by the extension's official #html section above.
    output = render(["A[^1]\n\n[^1]: note"], raw=True)["output"][0]
    assert 'href="#user-content-fn-1"' in output["html"]
    assert 'href="#user-content-fnref-1"' in output["html"]
    assert '<h2 id="footnote-label" class="sr-only">Footnotes</h2>' in output["html"]
    assert 'href="#fn-1"' in reference("A[^1]\n\n[^1]: note")
    assert normalized(output["html"]) == normalized(output["raw"])


def test_known_table_escape_difference():
    # https://github.com/micromark/micromark-extension-gfm-table#bugs
    # https://github.com/github/cmark-gfm/issues/277
    source = "| Head |\n| - |\n| C \\\\| Charlie |"
    output = render([source], raw=True)["output"][0]
    expected = (
        "<table><thead><tr><th>Head</th></tr></thead><tbody><tr><td>C \\</td></tr></tbody></table>"
    )
    cmark_expected = expected.replace("C \\", "C | Charlie")
    assert normalized(output["raw"]) == normalized(expected)
    assert normalized(reference(source)) == normalized(cmark_expected)
    assert normalized(output["html"]) == normalized(expected)


def test_pinned_vendor_and_self_contained_module():
    source = VENDOR.read_text()
    assert hashlib.sha256(VENDOR.read_bytes()).hexdigest() == VENDOR_SHA256
    assert not re.search(
        r"\b(?:import\s*\(|import\s+.+?from\s*['\"]|export\s*.+?from\s*['\"])", source
    )
    assert "document." not in source
    assert "micromark 4.0.3 + micromark-extension-gfm 3.0.0" in source
    assert "sh scripts/vendor_micromark.sh" in source
    assert "Permission is hereby granted" in source
    lock = json.loads((ROOT / "scripts/vendor/micromark/package-lock.json").read_text())
    assert lock["packages"]["node_modules/micromark"]["version"] == "4.0.3"
    assert lock["packages"]["node_modules/micromark-extension-gfm"]["version"] == "3.0.0"


def test_display_policy_and_escaping():
    inputs = [
        "[relative](docs/x) [empty]() [fragment](#part) [network](//example.test/x)",
        '![<img onerror="x">](https://image.test/a "title")',
        "[![inner](https://image.test/a)](https://outer.test/b)",
        "[![inner](https://image.test/a)](relative)",
        "![local](images/x.png) ![empty]()",
        "[A[^1]](https://outer.test/a)\n\n[^1]: note",
        "[A[^1]](relative)\n\n[^1]: note",
        "A[^1]\n\n[^1]: note",
        "| a | b |\n| :- | -: |\n| x | y |",
        "```js\nconst x = 1;\n```",
    ]
    result = render(inputs, raw=True)
    assert result["escaped"] == "&amp;&lt;&gt;&quot;&#39;"
    for output in result["output"]:
        assert normalized(output["html"]) == normalized(output["raw"])
        assert "<img" not in output["html"]
        tree = Tree(output["html"])

        def check(node, inside=False):
            if isinstance(node, str):
                return
            tag, attrs, children = node
            if tag == "a":
                assert not inside
                assert attrs["target"] == "_blank"
                assert attrs["rel"] == "noopener noreferrer"
                assert SAFE_HREF.match(attrs["href"])
            for child in children:
                check(child, inside or tag == "a")

        check(tree.root)
    html = [item["html"] for item in result["output"]]
    assert html[0].count("<a ") == 1
    assert html[2].count("<a ") == 1
    assert "<a " not in html[3]
    assert '<sup><span id="user-content-fnref-1">1</span></sup>' in html[5]
    assert html[5].count("<a ") == 2  # outer link plus footnote backreference
    assert 'href="#user-content-fn-1"' not in html[5]
    assert 'href="#user-content-fn-1"' in html[6]  # unwrapped relative outer link
    assert 'href="#user-content-fn-1"' in html[7]
    assert '<colgroup><col span="2"></colgroup>' in html[8]
    assert 'class="align-left"' in html[8]
    assert 'class="align-right"' in html[8]
    assert 'align="' not in html[8]
    assert '<pre tabindex="0"><code class="language-js">' in html[9]


def test_unwrapped_footnote_keeps_generated_backreference_target():
    source = '[<a id="attacker-id">A</a>[^1]](https://outer.test/a)\n\n[^1]: note'
    output = render([source], raw=True)["output"][0]
    targets = {}
    backrefs = []

    def inspect(node):
        if isinstance(node, str):
            return
        tag, attrs, children = node
        if "id" in attrs:
            targets[attrs["id"]] = tag
        if "data-footnote-backref" in attrs:
            backrefs.append(attrs["href"])
        for child in children:
            inspect(child)

    inspect(Tree(output["html"]).root)
    assert "attacker-id" not in targets
    assert targets["user-content-fnref-1"] == "span"
    assert backrefs == ["#user-content-fnref-1"]
    assert 'id="user-content-fnref-1"' in output["raw"]
    assert "&lt;a id=&quot;attacker-id&quot;&gt;" in output["html"]
    assert normalized(output["html"]) == normalized(output["raw"])


def test_hostile_html_protocols_and_images_are_inert_strings():
    payloads = [
        "<script>alert(1)</script>",
        "<img src=https://image.test/x onerror=alert(1)>",
        '<svg onload="alert(1)"><a href="javascript:x">x</a></svg>',
        '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
        "[x](javascript:alert(1))",
        "[x](JaVaScRiPt:alert(1))",
        "[x](jav&#x61;script:alert(1))",
        "[x](data:text/html,attack)",
        "[x](vbscript:msgbox(1))",
        "![x](javascript:alert(1))",
        "![<script>alert(1)</script>](https://image.test/x)",
        "[![x](data:image/svg+xml,attack)](javascript:alert(1))",
        '![x](https://image.test/x "\\" onerror=alert(1)")',
        "<style>body{background:url(https://image.test/x)}</style>",
    ]
    output = render(payloads + [f"| x |\n| - |\n| {p} |" for p in payloads])["output"]
    for item in output:
        tree = Tree(item["html"])

        def check(node):
            if isinstance(node, str):
                return
            tag, attrs, children = node
            assert tag not in {"script", "style", "svg", "img", "iframe", "object", "embed", "link"}
            assert not any(
                key.startswith("on") or key in {"src", "srcdoc", "style"} for key in attrs
            )
            if "href" in attrs:
                assert SAFE_HREF.match(attrs["href"])
            for child in children:
                check(child)

        check(tree.root)
