"""Behaviour of the n8n Code nodes that shorten long papers and report failed briefings.

The code is taken from the workflow JSON and executed with Node.js against a minimal mock of n8n's `$input`, `$` and
`$env`. Skipped when `node` is not installed. Deterministic: all texts are built from fixed strings.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const fs = require('fs');
const [, , codeFile, ctxFile] = process.argv;
const code = fs.readFileSync(codeFile, 'utf8');
const ctx = JSON.parse(fs.readFileSync(ctxFile, 'utf8'));
const wrap = (arr) => arr.map((json) => ({ json }));
const $input = { all: () => wrap(ctx.input), first: () => wrap(ctx.input)[0] };
const $ = (name) => ({ first: () => wrap(ctx.nodes[name])[0], all: () => wrap(ctx.nodes[name]) });
const $env = ctx.env || {};
let out;
try {
  if (ctx.expression) {
    out = { value: new Function('$', '$env', 'return (' + code + ');')($, $env) };
  } else {
    out = { value: new Function('$input', '$', '$env', code)($input, $, $env) };
  }
} catch (e) {
  out = { error: e.message };
}
console.log(JSON.stringify(out));
"""


def nodes_of(name):
    wf = json.loads((ROOT / "n8n" / "workflows" / f"{name}.json").read_text(encoding="utf-8"))
    return {n["name"]: n for n in wf["nodes"]}


def run(tmp_path, code, ctx):
    (tmp_path / "h.js").write_text(HARNESS, encoding="utf-8")
    (tmp_path / "code.js").write_text(code, encoding="utf-8")
    (tmp_path / "ctx.json").write_text(json.dumps(ctx), encoding="utf-8")
    res = subprocess.run([NODE, str(tmp_path / "h.js"), str(tmp_path / "code.js"), str(tmp_path / "ctx.json")],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def paper_text(chars):
    sentence = "The quantum sampler converges under thermal loss. "
    return (sentence * (chars // len(sentence) + 1))[:chars]


def paper(tmp_path, text, podcast_type="Briefing", env=None, main_text=None):
    code = nodes_of("podcast-generation")["Paper"]["parameters"]["jsCode"]
    item = {"title": "T", "fullText": text}
    if main_text is not None:
        item["mainText"] = main_text
    ctx = {"input": [item], "nodes": {"INPUT": [{"arxivID": "2611.13428", "podcast_type": podcast_type}]},
           "env": env or {}}
    res = run(tmp_path, code, ctx)
    assert "error" not in res, res
    return res["value"][0]["json"]


# ------------------------------------------------------------------------------------------------ Extract Text
def extract(tmp_path, html):
    code = nodes_of("podcast-generation")["Extract Text"]["parameters"]["jsCode"]
    res = run(tmp_path, code, {"input": [{"data": html}], "nodes": {}})
    assert "error" not in res, res
    return res["value"][0]["json"]


PAGE = ("<html><head><title>A paper</title></head><body><nav>menu</nav>"
        '<section class="ltx_section"><h2>1 Introduction</h2><p>Main &amp; results.</p></section>'
        '<section class="ltx_bibliography"><h2>References</h2><p>Smith 1999.</p></section>'
        '<section class="ltx_appendix"><h2>Appendix A Proofs</h2><p>Long proof.</p></section></body></html>')


def test_extract_text_main_part_stops_before_bibliography_and_appendix(tmp_path):
    j = extract(tmp_path, PAGE)
    assert "Main & results." in j["mainText"]
    assert "Smith" not in j["mainText"] and "proof" not in j["mainText"] and "Appendix" not in j["mainText"]
    assert "Smith 1999." in j["fullText"] and "Long proof." in j["fullText"] and "menu" not in j["fullText"]


def test_extract_text_appendix_before_bibliography(tmp_path):
    html = PAGE.replace('<section class="ltx_bibliography"><h2>References</h2><p>Smith 1999.</p></section>', "")
    html = html.replace("</body>", '<section class="ltx_bibliography"><p>Smith 1999.</p></section></body>')
    j = extract(tmp_path, html)
    assert "Long proof" not in j["mainText"] and "Smith" not in j["mainText"] and "Main & results." in j["mainText"]


def test_extract_text_without_markers_has_no_main_text(tmp_path):
    j = extract(tmp_path, "<html><title>X</title><body><p>Only text.</p></body></html>")
    assert j["mainText"] is None and "Only text." in j["fullText"]


# ------------------------------------------------------------------------------------------------------ Paper
def test_short_paper_is_unchanged_even_if_a_main_part_exists(tmp_path):
    j = paper(tmp_path, paper_text(120_000), main_text=paper_text(60_000))
    assert len(j["fullText"]) == 120_000 and j["longPaper"] is False and j["supplementDropped"] is False
    assert j["tldrSource"] == j["fullText"]
    assert j["arxivID"] == "(arXiv: 2611.13428)" and j["link"] == "https://arxiv.org/pdf/2611.13428"


def test_long_paper_keeps_only_the_main_part(tmp_path):
    j = paper(tmp_path, paper_text(369_000), main_text=paper_text(40_000))
    assert len(j["fullText"]) == 40_000 and j["supplementDropped"] is True
    assert j["longPaper"] is False and j["textTruncated"] is False and j["textCharsOriginal"] == 369_000
    assert j["tldrSource"] == j["fullText"]


def test_too_short_main_part_is_ignored(tmp_path):
    j = paper(tmp_path, paper_text(369_000), main_text=paper_text(10_000))
    assert len(j["fullText"]) == 369_000 and j["supplementDropped"] is False and j["longPaper"] is True
    assert 50_000 < len(j["tldrSource"]) <= 60_000 and j["tldrSource"].endswith(".")


def test_long_paper_without_any_marker_is_flagged_for_the_long_model(tmp_path):
    j = paper(tmp_path, paper_text(369_000))
    assert j["longPaper"] is True and j["supplementDropped"] is False and len(j["fullText"]) == 369_000


def test_converted_pdf_is_cut_at_an_appendix_heading_in_the_second_half(tmp_path):
    body = paper_text(400_000)
    cut = 280_000
    text = body[:cut] + "\n\n## **Appendix A: Lattice generator update** \n\n" + body[cut:]
    j = paper(tmp_path, text)
    assert j["supplementDropped"] is True and j["longPaper"] is False
    assert cut <= len(j["fullText"]) <= cut + 2 and "Appendix" not in j["fullText"]  # keeps the blank lines before it


@pytest.mark.parametrize("heading,position", [
    ("## **Appendix A: Proofs** ", 20_000),     # early heading (e.g. a table of contents): ignored
    ("## **References** ", 300_000),            # references alone never end the text
    ("Appendix A in the text is only mentioned", 300_000),
])
def test_pdf_cut_ignores_other_headings(tmp_path, heading, position):
    body = paper_text(400_000)
    text = body[:position] + "\n\n" + heading + "\n\n" + body[position:]
    j = paper(tmp_path, text)
    assert j["supplementDropped"] is False and len(j["fullText"]) == len(text)


def test_huge_text_without_markers_is_cut_at_a_sentence_end(tmp_path):
    j = paper(tmp_path, paper_text(737_581))
    assert j["textTruncated"] is True and j["textCharsOriginal"] == 737_581
    assert 450_000 < len(j["fullText"]) <= 500_000 and j["fullText"].endswith(".") and j["longPaper"] is True


def test_main_part_above_the_limit_is_still_capped(tmp_path):
    j = paper(tmp_path, paper_text(900_000), main_text=paper_text(600_000))
    assert j["supplementDropped"] is True and j["textTruncated"] is True and len(j["fullText"]) <= 500_000


def test_lecture_series_is_never_shortened(tmp_path):
    j = paper(tmp_path, paper_text(737_581), podcast_type="Lecture Series", main_text=paper_text(40_000))
    assert len(j["fullText"]) == 737_581 and j["longPaper"] is False
    assert j["supplementDropped"] is False and j["textTruncated"] is False


def test_limits_come_from_the_environment(tmp_path):
    j = paper(tmp_path, paper_text(5_000), env={"PAPER_MAX_CHARS": "2000", "LONG_PAPER_CHARS": "1000"})
    assert j["textTruncated"] is True and len(j["fullText"]) <= 2_000 and j["longPaper"] is True


def test_cut_never_leaves_a_lone_surrogate(tmp_path):
    j = paper(tmp_path, "\U0001F600" * 3_000, env={"PAPER_MAX_CHARS": "2001"})
    j["fullText"].encode("utf-8")  # raises UnicodeEncodeError on a lone surrogate
    assert 0 < len(j["fullText"]) <= 2_001


# ----------------------------------------------------------------------------------------------- model choice
@pytest.mark.parametrize("long_paper,expected", [(True, "long-model"), (False, "brief-model")])
def test_briefing_model_follows_the_long_flag(tmp_path, long_paper, expected):
    expr = nodes_of("podcast-generation")["Briefing Backup Model"]["parameters"]["model"]
    assert expr.startswith("={{") and expr.endswith("}}")
    ctx = {"expression": True, "input": [], "nodes": {"Paper": [{"longPaper": long_paper}]},
           "env": {"LLM_MODEL_BRIEFING": "brief-model", "LLM_MODEL_LONG": "long-model"}}
    assert run(tmp_path, expr[3:-2].strip(), ctx)["value"] == expected


def test_long_model_falls_back_to_briefing_model_when_unset(tmp_path):
    expr = nodes_of("podcast-generation")["Briefing Backup Model"]["parameters"]["model"]
    ctx = {"expression": True, "input": [], "nodes": {"Paper": [{"longPaper": True}]},
           "env": {"LLM_MODEL_BRIEFING": "brief-model", "LLM_MODEL_LONG": ""}}
    assert run(tmp_path, expr[3:-2].strip(), ctx)["value"] == "brief-model"


# ------------------------------------------------------------------------------------- failed briefing report
@pytest.mark.parametrize("name", ["arxiv-scout", "arxiv-overflow-flush"])
def test_check_node_passes_clean_batches_and_names_failures(tmp_path, name):
    code = nodes_of(name)["Check Briefing Failures"]["parameters"]["jsCode"]
    ok = [{"pageContent": "x"}, {"pageContent": "y"}]
    res = run(tmp_path, code, {"input": ok, "nodes": {}})
    assert [i["json"] for i in res["value"]] == ok
    mixed = ok + [{"arxivId": "2611.13428", "error": "502 from gateway"},
                  {"arxivID": "2611.99999", "error": {"message": "boom"}}]
    res = run(tmp_path, code, {"input": mixed, "nodes": {}})
    assert "error" in res and "2 of 4" in res["error"]
    assert "2611.13428" in res["error"] and "2611.99999" in res["error"] and "502 from gateway" in res["error"]
    assert not re.search(r"\[object Object\]", res["error"])
