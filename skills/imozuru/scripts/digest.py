#!/usr/bin/env python3
"""Build a verified detailed report and complete search record.

Usage:
    digest.py add <topic_dir> [--round N] [--by NAME]
    digest.py <topic_dir> [--check]

Every explanatory sentence must include one or more exact source quotations.
"""

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402  照合と引用の描画は抜き書きと同じものを使う
import skin  # noqa: E402  見た目（コルク板と象牙の紙）は抜き書き・ダッシュボードと共通

QUERIES = "queries.jsonl"
FETCH_LOG = "fetch-log.jsonl"
# 水準ごとに、読み手が知りたいことが違う。Lv1 は何が起きているか、Lv2 は仕組み、
# Lv3 は仕組みを知っている人が次に知りたいこと（通説の修正・未決着・手法と数値・出典の食い違い）
LEVELS = (("lv1", "Lv1", "No prior knowledge"),
          ("lv2", "Lv2", "Related-field knowledge"),
          ("lv3", "Lv3", "Field specialist"))


def append_records(topic: Path, records: list[dict]) -> int:
    """検索の記録を queries.jsonl の末尾に足す。足した件数を返す。"""
    topic.mkdir(parents=True, exist_ok=True)
    with (topic / QUERIES).open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)


def normalize_record(r: dict, round_: int | None, by: str | None) -> dict:
    """形を確かめてそろえる。query と hits が無い記録は受け付けない（空振りは hits を空の配列で書く）。"""
    if not isinstance(r, dict) or not isinstance(r.get("query"), str) or not r["query"].strip():
        raise ValueError(f"record has no query: {json.dumps(r, ensure_ascii=False)[:80]}")
    hits = r.get("hits")
    if not isinstance(hits, list):
        raise ValueError(f"hits is not an array (use [] for no hits): {r['query']}")
    out_hits = []
    for h in hits:
        if not isinstance(h, dict) or not isinstance(h.get("url"), str):
            raise ValueError(f"hit has no url: {r['query']}")
        hit = {"title": str(h.get("title") or ""), "url": h["url"]}
        if h.get("also"):
            hit["also"] = [str(u) for u in h["also"]]
        out_hits.append(hit)
    rnd = r.get("round", round_)
    if type(rnd) is not int:
        raise ValueError(f"round is not an integer: {r['query']}")
    return {"round": rnd, "by": str(r.get("by") or by or "unknown"),
            "method": str(r.get("method") or "WebSearch"), "query": r["query"], "hits": out_hits}


def url_key(url: str) -> str:
    """照合用に URL をそろえる。http と https、www、末尾の /、#以降の違いは同じものとして扱う。"""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (p.hostname or "").lower().removeprefix("www.")
    path = p.path.rstrip("/")
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def fetch_status(fetches: list[dict]) -> dict[str, dict]:
    """URL ごとの最後の取得の結果。同じ URL を取り直したら新しいほうを使う。"""
    return {url_key(f["url"]): f for f in fetches if isinstance(f.get("url"), str)}


def sentence_problems(src: Path, where: str, sentences) -> list[str]:
    """文の並びを照合する。根拠の無い文・原文に無い引用を、場所つきで返す。"""
    bad = []
    if not isinstance(sentences, list) or not sentences:
        return [f"{where}: no sentences"]
    for i, s in enumerate(sentences, 1):
        if not isinstance(s, dict) or not isinstance(s.get("text"), str) or not s["text"].strip():
            bad.append(f"{where}, sentence {i}: no text")
            continue
        quotes = s.get("quotes")
        if not isinstance(quotes, list) or not quotes:
            bad.append(f"{where}, sentence {i} ({s['text'][:30]}): no evidence quotation")
            continue
        for q in quotes:
            if not isinstance(q, dict) or not report.verify(src, q)[0]:
                loc = f"{q.get('file')}:{q.get('line')}" if isinstance(q, dict) else "?"
                bad.append(f"{where}, sentence {i} ({s['text'][:30]}): quotation not found in source {loc}")
    return bad


CLAIM_STATUS = ("supported", "partially supported", "unsupported")
SITES_SHOWN = 10


def check(topic: Path, data: dict) -> list[str]:
    src = topic / "sources"
    bad = []
    levels = data.get("levels") or {}
    for key, name, _ in LEVELS:
        lv = levels.get(key)
        if not isinstance(lv, dict):
            bad.append(f"{name} is missing")
            continue
        bad += sentence_problems(src, f"{name} summary", lv.get("summary"))
        for t in lv.get("terms") or []:
            bad += sentence_problems(src, f"{name} term {t.get('term')}", t.get("sentences"))
        for a in lv.get("answers") or []:
            bad += sentence_problems(src, f"{name} point {a.get('claim')}", a.get("sentences"))
        # 確かめる問いの答えも解説と同じく原文で裏づける。問いの文そのものは論点の答えを含まないので照合しない
        for q in lv.get("quiz") or []:
            if not isinstance(q, dict) or not str(q.get("q") or "").strip():
                bad.append(f"{name} quiz: no question")
                continue
            bad += sentence_problems(src, f"{name} answer to {q['q'][:20]}", q.get("answer"))
    for c in data.get("claims") or []:
        if not isinstance(c, dict) or report.OLD_STATUS.get(c.get("status"), c.get("status")) not in CLAIM_STATUS:
            bad.append(f"claim status must be one of {', '.join(CLAIM_STATUS)}: {json.dumps(c, ensure_ascii=False)[:60]}")
    if data.get("people_places"):
        bad += sentence_problems(src, "people and venues", data["people_places"])
    return bad


esc = report.esc


def link(url: str, text: str) -> str:
    """http(s) だけをリンクにする。javascript: などはそのまま文字で出す。"""
    try:
        if urlsplit(url).scheme in ("http", "https"):
            return f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(text or url)}</a>'
    except ValueError:
        pass
    return esc(text or url)


def sentence_html(src: Path, s: dict) -> str:
    """1文を開閉できる行にする。押すと根拠の引用がその文のすぐ下に開く。

    根拠を節の末尾にまとめると、文と引用が離れて、読み手が両者を突き合わせる手間が増える
    （分割注意効果）。文と引用を同じ場所に置く。
    """
    quotes = []
    for q in s["quotes"]:
        _, ctx, c0, _ = report.verify(src, q)
        quotes.append(report.quote_html(src, q, ctx, c0))
    return (f'<details class="s"><summary>{esc(s["text"])}<span class="mk">Sources {len(quotes)}</span></summary>'
            + "".join(quotes) + "</details>")


def sentences_html(src: Path, sentences: list[dict]) -> str:
    return '<div class="lines">' + "".join(sentence_html(src, s) for s in sentences) + "</div>"


def level_html(src: Path, key: str, name: str, lead: str, lv: dict) -> str:
    # タブと同じ名前を本文の見出しに重ねない（同じ情報を二重に見せると負担になる）。読み上げ用にだけ残す
    body = [f'<section class="lv {key}" id="{key}"><h2 class="sr">{esc(name)}（{esc(lead)}）</h2>',
            "<h3>Summary</h3>", sentences_html(src, lv["summary"])]
    if lv.get("terms"):
        body.append("<h3>Terms</h3><dl>")
        for t in lv["terms"]:
            body.append(f"<dt>{esc(t['term'])}</dt><dd>{sentences_html(src, t['sentences'])}</dd>")
        body.append("</dl>")
    if lv.get("answers"):
        body.append("<h3>Research points</h3>")
        for a in lv["answers"]:
            body.append(f'<h4>{esc(a["claim"])}</h4>{sentences_html(src, a["sentences"])}')
    if lv.get("quiz"):
        # 自分で思い出してから答えを見る形にする。読み返すより、自分をテストするほうが定着する（Dunlosky ら 2013）
        body.append('<h3>Check your understanding</h3>')
        for q in lv["quiz"]:
            body.append(f'<div class="quiz"><p class="q">{esc(q["q"])}</p><details class="ans"><summary>Show answer</summary>'
                        + sentences_html(src, q["answer"]) + "</details></div>")
    body.append("</section>")
    return "\n".join(body)


def levels_html(src: Path, data: dict) -> str:
    """3編を切り替えて1編ずつ見せる。全部を縦に並べると、初学者には多すぎ、慣れた人には重複が多い
    （熟達化の逆転効果）。切り替えは CSS だけで動き、選んだ編の記憶だけを小さなスクリプトが受け持つ。"""
    inputs = "".join(f'<input type="radio" name="lv" id="t-{k}"{" checked" if i == 0 else ""}>'
                     for i, (k, _, _) in enumerate(LEVELS))
    tabs = "".join(f'<label for="t-{k}">{esc(n)}<small>{esc(lead)}</small></label>' for k, n, lead in LEVELS)
    sections = "".join(level_html(src, k, n, lead, data["levels"][k]) for k, n, lead in LEVELS)
    return f'<div class="levels sheet" id="explain">{inputs}<div class="tabs">{tabs}</div>{sections}</div>'


def overview_html(data: dict) -> str:
    """冒頭は3つに絞る: 調べたこと・調査を区切った理由・調査の論点ごとの根拠の有無。詳しいものは下へ（概観から詳細へ）。"""
    out = ['<dl class="overview">',
           f"<dt>Question</dt><dd>{esc(data['question'])}</dd>",
           f"<dt>Stop reason</dt><dd>{esc(data['stopped'])}</dd>"]
    if data.get("claims"):
        statuses = [report.OLD_STATUS.get(c["status"], c["status"]) for c in data["claims"]]
        items = "".join(f'<li><span class="st st{CLAIM_STATUS.index(st)}">{st}</span>{esc(c["claim"])}</li>'
                        for c, st in zip(data["claims"], statuses))
        out.append(f'<dt>Research points</dt><dd><ul class="claims">{items}</ul></dd>')
    out.append("</dl>")
    return "".join(out)


def hit_status(h: dict, fetched: dict[str, dict]) -> tuple[dict | None, str | None]:
    for u in [h["url"], *h.get("also", [])]:
        k = url_key(u)
        if k in fetched:
            return fetched[k], k
    return None, None


def sites_html(records: list[dict], fetched: dict[str, dict]) -> str:
    """どのサイトを見て、何件保存・失敗したか。操作の順に並べるより、どこを見たかの要約のほうが
    後から読む人の理解を妨げない（Block ら 2022）。件数は共通の目盛りの横棒で比べる（位置が最も正確に読める）。"""
    sites: dict[str, list[int]] = {}
    for r in records:
        for h in r["hits"]:
            try:
                host = (urlsplit(h["url"]).hostname or "?").removeprefix("www.")
            except ValueError:
                host = "?"
            row = sites.setdefault(host, [0, 0, 0])
            row[0] += 1
            f, _ = hit_status(h, fetched)
            if f is not None:
                row[1 if f.get("status") == "OK" else 2] += 1
    if not sites:
        return ""
    top = sorted(sites.items(), key=lambda kv: (-kv[1][0], kv[0]))
    peak = top[0][1][0]

    def table(items) -> str:
        rows = "".join(f'<tr><th scope="row">{esc(host)}</th><td class="bar"><span style="width:{n / peak * 100:.1f}%"></span></td>'
                       f"<td>{n}</td><td>{ok or ''}</td><td>{ng or ''}</td></tr>" for host, (n, ok, ng) in items)
        return ('<div class="tablewrap"><table class="sites"><thead><tr><th>Site</th><th></th>'
                '<th>Hits</th><th>Saved</th><th>Failed</th></tr></thead><tbody>' + rows + "</tbody></table></div>")
    # 先頭は上位の SITES_SHOWN 件だけ。残りは畳む（一度に見せる量を絞る）
    out = "<h3>Referenced sites</h3>" + table(top[:SITES_SHOWN])
    if len(top) > SITES_SHOWN:
        out += f"<details><summary>{len(top) - SITES_SHOWN} more sites</summary>{table(top[SITES_SHOWN:])}</details>"
    return out


def records_html(records: list[dict], fetched: dict[str, dict]) -> str:
    """検索の全記録。サイトごとの集計を先に置き、検索語ごとの全件は周ごとに折りたたむ。"""
    linked = set()
    total_hits = sum(len(r["hits"]) for r in records)
    empty = sum(1 for r in records if not r["hits"])
    body = ['<section class="sheet" id="record"><h2>Research record<span class="status">Generated</span></h2>',
            f'<p class="note">Queries {len(records)} · hits {total_hits} · no-result queries {empty}</p>',
            sites_html(records, fetched),
            "<h3>All results by query</h3>"]
    for rnd in sorted({r["round"] for r in records}):
        rs = [r for r in records if r["round"] == rnd]
        n_hits = sum(len(r["hits"]) for r in rs)
        body.append(f"<details><summary>Round {esc(str(rnd))} (queries {len(rs)}, hits {n_hits})</summary>")
        for r in rs:
            body.append(f'<h4>“{esc(r["query"])}”<span class="meta">{esc(r["by"])} · {esc(r["method"])}</span></h4>')
            if not r["hits"]:
                body.append('<p class="note">0 hits</p>')
                continue
            body.append("<ul class=\"hits\">")
            for h in r["hits"]:
                f, k = hit_status(h, fetched)
                if k:
                    linked.add(k)
                if f is None:
                    tag = '<span class="tag">Not fetched</span>'
                elif f.get("status") == "OK":
                    tag = f'<span class="tag ok">Saved</span><code>{esc(f.get("file", ""))}</code>'
                else:
                    tag = f'<span class="tag ng">Fetch failed</span><span class="cn">{esc(f.get("reason", ""))}</span>'
                body.append(f"<li>{tag} {link(h['url'], h['title'])}</li>")
            body.append("</ul>")
        body.append("</details>")
    loose = [f for k, f in fetched.items() if k not in linked]
    if loose:
        body.append('<h3>Sources fetched outside search results</h3><ul class="hits">')
        for f in loose:
            st = "Saved" if f.get("status") == "OK" else "Fetch failed"
            detail = f.get("file", "") if f.get("status") == "OK" else f.get("reason", "")
            via = f'<span class="cn">{esc(f["via"])}</span>' if f.get("via") else ""
            body.append(f'<li><span class="tag">{st}</span> {link(f["url"], f["url"])} <code>{esc(detail)}</code>{via}</li>')
        body.append("</ul>")
    body.append("</section>")
    return "\n".join(body)


CSS = report.CSS + """
nav{display:flex;flex-wrap:wrap;gap:.4rem 1rem;margin:1rem 0;font-size:.9rem}
a{color:var(--accent)}
h3{font-size:1.05rem;margin:2rem 0 .3rem}
h4{font-size:.95rem;margin:1.2rem 0 .2rem}
dt{font-weight:600;margin-top:1rem}
dd{margin:0}
.overview{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.2rem 1rem 1rem;margin:1.2rem 0}
.overview dt{font-size:.8rem;color:var(--sub);font-weight:400;margin-top:.8rem}
ul.claims{list-style:none;padding:0;margin:.3rem 0 0}
ul.claims li{margin:.35rem 0;display:flex;gap:.5rem;align-items:baseline}
.st{flex:none;font-size:.72rem;border:1px solid var(--accent);color:var(--accent);border-radius:99px;padding:0 .45rem}
.st1{border-style:dotted}
.st2{border-style:dashed;color:var(--sub);border-color:var(--sub)}
.levels>input{position:absolute;opacity:0;pointer-events:none}
/* 3編のタブは同じ幅で1段に並べる。折り返すと、スマホで Lv3 だけが2段目に落ちた（2026-10-22 かしぇ） */
.tabs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.4rem;margin:0;border-bottom:1px solid var(--line)}
@media (max-width:600px){.tabs label{padding:.4rem .5rem .45rem;font-size:.92rem}.tabs label small{font-size:.62rem;line-height:1.35}}
/* 紙の中の最初の見出し（要旨など）は、ピンと切り替えの札の下に少し間を取る */
.lv>h3:first-of-type{margin-top:1.4rem}
.tabs label{cursor:pointer;padding:.45rem .9rem .5rem;border:1px solid var(--line);border-bottom:none;border-radius:8px 8px 0 0;color:var(--sub);font-weight:600}
.tabs label small{display:block;font-weight:400;font-size:.7rem}
.lv{display:none}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
#t-lv1:checked~.lv1,#t-lv2:checked~.lv2,#t-lv3:checked~.lv3{display:block}
#t-lv1:checked~.tabs label[for=t-lv1],#t-lv2:checked~.tabs label[for=t-lv2],#t-lv3:checked~.tabs label[for=t-lv3]{color:var(--ink);background:var(--card);border-color:var(--accent)}
.levels>input:focus-visible~.tabs{outline:2px solid var(--accent);outline-offset:2px}
.lines{margin:.3rem 0}
details.s{margin:.15rem 0;font-size:1rem}
details.s>summary{list-style:none;cursor:pointer;padding:.15rem 0;color:var(--ink)}
details.s>summary::-webkit-details-marker{display:none}
.mk{font-size:.68rem;color:var(--accent);border:1px solid var(--line);border-radius:4px;padding:0 .3rem;margin-left:.4rem;white-space:nowrap;vertical-align:.1em}
details.s[open]>summary .mk{background:var(--hit)}
details.s .quote{margin:.4rem 0 .8rem}
.quiz{border-left:3px solid var(--line);padding-left:.8rem;margin:1rem 0}
.quiz .q{margin:.2rem 0;font-weight:600}
.meta{font-weight:400;font-size:.8rem;color:var(--sub);margin-left:.5rem}
ul.hits{padding-left:1.1rem;font-size:.88rem}
ul.hits li{margin:.25rem 0;overflow-wrap:anywhere}
.tag.ok{border-color:var(--accent);color:var(--accent)}
.tag.ng{border-style:dashed}
.cn{font-size:.8rem;color:var(--sub);margin-left:.3rem}
.tablewrap{overflow-x:auto}
table.sites{border-collapse:collapse;font-size:.82rem;width:100%;font-variant-numeric:tabular-nums}
table.sites th,table.sites td{padding:.2rem .3rem;text-align:right;white-space:nowrap}
table.sites th[scope=row]{text-align:left;font-weight:400;max-width:9rem;overflow:hidden;text-overflow:ellipsis}
table.sites thead th{color:var(--sub);font-weight:400;border-bottom:1px solid var(--line)}
td.bar{width:30%;min-width:3rem}
td.bar span{display:block;height:.55rem;background:var(--accent);opacity:.55;border-radius:2px}
"""

# 選んだ編を覚えておくだけのスクリプト。保存できない環境（非公開の窓など）でも、ページは CSS だけで動く
REMEMBER_JS = ("try{var k='imozuru-digest-lv',v=localStorage.getItem(k),e=v&&document.getElementById(v);"
               "if(e)e.checked=true;document.querySelectorAll('input[name=lv]').forEach(function(i){"
               "i.addEventListener('change',function(){try{localStorage.setItem(k,i.id)}catch(x){}})})}catch(x){}")


def build(topic: Path, check_only: bool) -> int:
    data = json.loads((topic / "digest.json").read_text(encoding="utf-8"))
    # 壊れた記録を飛ばすと調査の全件と誤認させるので、理由を出して前のページを残す
    try:
        records = [normalize_record(r, None, None) for r in load_jsonl(topic / QUERIES)]
    except ValueError as e:
        print(f"Could not read {topic / QUERIES}: {e}", file=sys.stderr)
        return 1
    bad = check(topic, data)
    if bad:
        print("Some sentences failed evidence verification (missing evidence or a quotation mismatch):")
        print("\n".join("  " + b for b in bad))
        return 1
    print(f"Verification OK: every explanatory sentence has a source quotation; {len(records)} query records")
    if check_only:
        return 0

    src = topic / "sources"
    fetched = fetch_status(load_jsonl(topic / FETCH_LOG))
    # 冒頭・解説・人物と媒体・未解決の点・調査記録を、それぞれ1枚の紙にする
    nav = ['<a href="#explain">Explanation</a>']
    sections = [levels_html(src, data)]
    if data.get("people_places"):
        sections.append('<section class="sheet" id="people"><h2>Key people and venues</h2>' + sentences_html(src, data["people_places"]) + "</section>")
        nav.append('<a href="#people">People and venues</a>')
    if data.get("unresolved"):
        sections.append('<section class="sheet" id="open"><h2>Unresolved points</h2><ul>'
                        + "".join(f"<li>{esc(u)}</li>" for u in data["unresolved"]) + "</ul></section>")
        nav.append('<a href="#open">Unresolved points</a>')
    sections.append(records_html(records, fetched))
    nav.append('<a href="#record">Research record</a>')
    # 操作の案内は書かない（2026-10-21 かしぇ「長々と注釈や操作案内を書きたくない」）。調査記録が機械の生成であることは見出しの札が示す
    body = ['<section class="sheet">', f"<h1>{esc(data['title'])}</h1>", overview_html(data),
            "<nav>" + "".join(nav) + "</nav></section>"]
    body += sections
    body.append(f'<footer>The imozuru detailed report and original sources are in {esc(src.name)}/ beside this page.</footer>')

    # スクリプトは中身のハッシュで許可する（skin.page）。'unsafe-inline' にすると、原文から紛れ込んだ文字列も動きうる
    page = skin.page(f"{data['title']} — Detailed report", "\n".join(body),
                     extra_head=f"<style>{CSS}</style>", scripts=(REMEMBER_JS,))
    out = topic / "digest.html"
    # 同じ場所に排他的に作り、仕込まれたリンクを踏まず、途中で止まっても前のページを残す
    fd, tmp = tempfile.mkstemp(prefix=".digest.html.", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(page)
        os.replace(tmp, out)
    finally:
        Path(tmp).unlink(missing_ok=True)
    print(f"→ {out}")
    return 0


def add(argv: list[str]) -> int:
    topic = Path(argv[0]).expanduser()
    round_, by = None, None
    rest = argv[1:]
    while rest:
        flag = rest.pop(0)
        if flag == "--round" and rest and re.fullmatch(r"[0-9]+", rest[0]):
            round_ = int(rest.pop(0))
        elif flag == "--by" and rest:
            by = rest.pop(0)
        else:
            print(f"Unknown option: {flag}", file=sys.stderr)
            return 2
    try:
        raw = json.loads(sys.stdin.read() or "[]")
        records = [normalize_record(r, round_, by) for r in (raw if isinstance(raw, list) else [raw])]
    except ValueError as e:  # 壊れた JSON も ValueError
        print(f"Could not add records: {e}", file=sys.stderr)
        return 1
    topic.mkdir(parents=True, exist_ok=True)
    n = append_records(topic, records)
    print(f"Added {n} records to {topic / QUERIES} ({sum(len(r['hits']) for r in records)} hits)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    if args[0] == "add" and len(args) >= 2:
        return add(args[1:])
    return build(Path(args[0]).expanduser(), "--check" in args[1:])


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
