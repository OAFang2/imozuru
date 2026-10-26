#!/usr/bin/env python3
"""Render the dashboard guide images in assets/guide-*.webp.

This development tool is run only after the interface changes. It captures fictional research
so private real topics never enter checked-in images, then overlays numbered callouts whose
accessible explanations remain as text in the dashboard.

Usage:
    make_guide_shots.py    Regenerate all guide images and assets/screenshots/dashboard.webp.
                           Requires Playwright WebKit and Pillow.
"""

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard  # noqa: E402
import palette  # noqa: E402
import skin  # noqa: E402

# Fictional research used only in screenshots.
MOCK = [
    ("sunset-red", "Why sunsets are red", "Which properties of light make the evening sky look red?", "Sources for 6/6 claims", 2,
     ["https://journals.example.org/a", "https://www.example.edu/b", "https://zh.example.org/c"]),
    ("tea-ceremony", "Origins of tea ceremony etiquette", "When and by whom was tea ceremony etiquette established?", "Stopped after round 1 (sources for 3/5 claims)", 1,
     ["https://archive.example.jp/a", "https://www.example.ac.jp/b"]),
    ("bird-songs", "Regional dialects in birdsong", "Why does one bird species sing differently by region?", "Saturated (11 of 14 results already known)", 3,
     ["https://journals.example.org/c", "https://www.example.edu/d"]),
    ("bread-yeast", "Why sourdough bread fails", "Why does sourdough bread sometimes fail to rise?", "Limit reached", 1, ["https://blog.example.net/a"]),
    ("type-history", "Origins of Ming typefaces", "Where did the shapes of Ming typefaces originate?", "Key primary source is paywalled", 2,
     ["https://archive.example.jp/b", "https://www.example.ac.jp/c"]),
    ("moss-garden", "Caring for moss gardens", "How can a moss garden be maintained without drying out?", "Sources for 4/4 claims", 2, ["https://blog.example.net/b"]),
    ("old-maps", "Reading historical maps", "What do symbols on old city maps represent?", "Stopped after four rounds", 4,
     ["https://archive.example.jp/c", "https://zh.example.org/d"]),
    ("glass-color", "How colored glass is made", "Which additives produce colors in glass?", "Sources for 5/5 claims", 2, ["https://www.example.edu/e"]),
]
# 一覧が2ページ以上になるよう、題だけの調査を足す（1ページに収まるとページ送りが出ず、図で指せない）
MOCK += [(f"extra-{i}", title, f"How should {title.lower()} be researched?", reason, 1, ["https://blog.example.net/c"])
         for i, (title, reason) in enumerate([
             ("How lacquer dries", "Sources for 3/3 claims"), ("How handmade paper is formed", "Stopped after round 1 (sources for 2/4 claims)"), ("Origins of constellation names", "Sources for 4/4 claims"),
             ("Fixing dye colors", "Saturated"), ("Wiring bonsai", "Limit reached"), ("Festival music rhythms", "Sources for 3/3 claims"),
             ("Textile pattern history", "Stopped after round 1"), ("Printing woodblock art", "Sources for 5/5 claims")])]
CLAIMS = ["Where is knowledge in this field collected?", "What is the standard method?", "Where does it fail?", "Is there primary research behind it?"]

PHONE = (390, 760)
PHONE_TALL = (390, 1180)  # 紙面は札が縦に3枚続くので、文書と論点の札まで入る高さで撮る
PC = (1280, 760)
PC_TALL = (1280, 1000)
# 書き出す幅（px）。スマホの絵は紙の上で約170px に、PC の絵は約560px に縮めて出す。高精細の画面で2倍あれば足りる
WIDTH = 400
WIDTH_PC = 1120
QUALITY = 72


def build_mock(root: Path) -> list[dict]:
    """Build fictional research folders and load them through dashboard.collect."""
    rows = ["| 日付 | 話題 | 型 | 回した人 | 調査を区切った理由 | 周 | 原文 | 感覚と合った？ |", "|---|---|---|---|---|---|---|---|"]
    for i, (slug, title, question, reason, rounds, urls) in enumerate(MOCK):
        d = root / slug
        (d / "sources").mkdir(parents=True)
        for j in range(len(urls) * 3):
            (d / "sources" / f"s{j}.md").write_text("x", encoding="utf-8")
        table = "".join(f"| Source {j} | s{j}.md | {u} | Round 1 |\n" for j, u in enumerate(urls))
        claims = "".join(f"- [{'x' if k < 3 else ' '}] {c}\n" for k, c in enumerate(CLAIMS))
        (d / "map.md").write_text(f"# map: {slug}\n\nQuestion: {question}\nTitle: {title}\n\n## Research points\n{claims}\n"
                                  f"## Original sources (sources/)\n| Source | File | Original URL | Round |\n|---|---|---|---|\n{table}", encoding="utf-8")
        date = f"2026-{10 - i // 28:02d}-{28 - i % 28:02d}"
        (d / "log.md").write_text(f"# log\n\n## 0周目（{date}）\n\n" + "".join(f"## {r}周目\n\n" for r in range(1, rounds + 1)), encoding="utf-8")
        # 検索と取得の記録（架空）。無いと紙面の「検索と取得」が「記録なし」になる
        recs = [{"round": r, "by": "scout", "method": "WebSearch", "query": f"{title} {k}",
                 "hits": [{"title": f"Source {k}-{h}", "url": f"{urls[h % len(urls)]}/{r}{k}{h}"} for h in range(6)]}
                for r in range(1, rounds + 1) for k in range(4)]
        (d / "queries.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in recs), encoding="utf-8")
        logs = [{"url": f"{u}/{j}", "status": "OK" if j % 4 else "FAIL", "file": f"s{j}.md", "reason": "404"}
                for j, u in enumerate(urls * 4)]
        (d / "fetch-log.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in logs), encoding="utf-8")
        for name in ("digest.html", "report.html"):
            (d / name).write_text(mock_doc(title, question), encoding="utf-8")
        rows.append(f"| {date} | {slug} | 掘り | Monet | {reason} | {rounds} | {len(urls) * 3} | Looks right |")
    runs = root.parent / "runs.md"
    runs.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return dashboard.collect(root, dashboard.load_runs(runs))


def mock_doc(title: str, question: str) -> str:
    """Return a short fictional page with the same paper treatment as a full digest."""
    body = (f'<section class="sheet"><h1>{title}</h1><dl class="overview"><dt>Question</dt><dd>{question}</dd>'
            '<dt>Why the research stopped</dt><dd>Every claim has a primary source.</dd></dl></section>'
            '<section class="sheet"><h2>Summary</h2><p>Evidence-based explanation appears here, supported by quotations from sources.</p>'
            '<p>Select a sentence to open its supporting quotation.</p></section>')
    return skin.page(f"{title} — Detailed report", body, extra_head=f"<style>{skin.CSS}</style>")


# 指す部品の上に番号の印を重ねるスクリプト。印は部品を囲む枠と、枠の左上の丸い番号
MARK_JS = """(marks) => { const c = '%s';
for (const [sel, n] of marks) { const el = document.querySelector(sel); if (!el) throw new Error('no ' + sel);
  const r = el.getBoundingClientRect(), box = document.createElement('div'), dot = document.createElement('div');
  // 枠は画面の内側に収める。画面いっぱいの部品（文書の枠）を囲むと、右と下の辺が画面の外に出て見えなかった
  const x0 = Math.max(2, r.left - 3), y0 = Math.max(2, r.top - 3);
  const x1 = Math.min(innerWidth - 2, r.right + 3), y1 = Math.min(innerHeight - 2, r.bottom + 3);
  box.style.cssText = `position:fixed;left:${x0}px;top:${y0}px;width:${x1 - x0}px;height:${y1 - y0}px;box-sizing:border-box;`
    + `border:3px solid ${c};border-radius:10px;box-shadow:0 0 0 1.5px #fff;z-index:99999;pointer-events:none`;
  dot.textContent = n;
  dot.style.cssText = `position:fixed;left:${Math.max(2, r.left - 14)}px;top:${Math.max(2, r.top - 14)}px;width:30px;height:30px;`
    + (/^[0-9]+$/.test(n) ? `background:${c};color:#fff;box-shadow:0 0 0 2px #fff;` : `background:#fff;color:${c};box-shadow:inset 0 0 0 2.5px ${c},0 0 0 2px #fff;`)
    + `border-radius:50%%;font:700 18px/30px system-ui,sans-serif;text-align:center;`
    + `z-index:100000`;
  document.body.append(box, dot); } }""" % palette.SWATCH["beni"]


def shoot(page, path: Path, marks: list[tuple[str, str]], clip: tuple[int, int, int, int] | None = None, width: int = 0) -> None:
    """clip は切り抜く範囲（CSS の px で x・y・幅・高さ）。説明に要る所だけを残し、紙の上で読み流す長さを縮める。
    width は書き出す幅（px）。0 なら切り抜いた幅の2倍（高精細の画面で縮めずに出せる大きさ）で、WIDTH_PC を上限にする"""
    from PIL import Image
    page.evaluate(MARK_JS, marks)
    page.wait_for_timeout(150)
    box = {"x": clip[0], "y": clip[1], "width": clip[2], "height": clip[3]} if clip else None
    img = Image.open(io.BytesIO(page.screenshot(clip=box))).convert("RGB")
    width = width or min(WIDTH_PC, (clip[2] if clip else page.viewport_size["width"]) * 2)
    img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    img.save(path, "WEBP", quality=QUALITY, method=6)
    print(f"→ {path} ({path.stat().st_size / 1024:.0f} KB)")


def main() -> int:
    from playwright.sync_api import sync_playwright
    import os
    # 入れるボタンと送るボタンは条件つきで出る部品（窓口で開いたとき・送り先のフォームがあるとき）。図には写したいので、撮るときだけ出す。
    # 送り先は例の値で、送りはしない
    os.environ["IMOZURU_FEEDBACK_FORM"] = "https://docs.google.com/forms/d/e/EXAMPLE/viewform?entry.1=不具合&entry.2=BODY"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "research"
        root.mkdir()
        topics = build_mock(root)
        page_file = root / "index.html"
        page_file.write_text(dashboard.render(topics, root), encoding="utf-8")
        url = page_file.as_uri()
        first = f"#t-{MOCK[0][0]}"
        doc = f"#doc/{MOCK[0][0]}/digest.html"
        side = 300  # パソコンのサイドバーの既定の幅（18rem）に、右端のつまみの分を足した幅
        # 端末ごとに、各画面をどの状態で開き、どこを切り抜くか。指す部品と番号は dashboard.GUIDE_SCREENS の表だけが決める
        plan = {
            "sp": {"home": (PHONE, "", False, (0, 0, PHONE[0], 250)),
                   "side": (PHONE, "", True, (0, 0, PHONE[0], 290)),
                   "topic": (PHONE_TALL, first, False, (0, 225, PHONE[0], 675)),
                   "doc": (PHONE, doc, False, None)},
            "pc": {"home": (PC, "", False, (side, 0, PC[0] - side, 520)),
                   "side": (PC, "", False, (0, 0, side, PC[1])),
                   "topic": (PC_TALL, first, False, (side, 0, PC[0] - side, 900)),
                   "doc": (PC, doc, False, None)},
        }
        with sync_playwright() as p:
            browser = p.webkit.launch()
            assets = dashboard.LOGO.parent
            for dev, screens in plan.items():
                for key, title, legend in dashboard.GUIDE_SCREENS:
                    size, hash_, open_side, clip = screens[key]
                    pg = browser.new_page(viewport={"width": size[0], "height": size[1]}, device_scale_factor=2)
                    pg.goto(url + hash_)
                    pg.wait_for_timeout(1400)  # 書体と枠の中の文書の読み込み、出てくる動きの終わりを待つ
                    pg.evaluate("document.getElementById('inst').hidden=false")
                    if open_side:
                        pg.click(".menu")
                        pg.wait_for_timeout(900)
                    marks = [(sel.format(dev=dev, t=first), n) for sel, n, _, _ in dashboard.screen_parts(legend, dev)]
                    shoot(pg, assets / f"guide-{dev}-{key}.webp", marks, clip, WIDTH if dev == "sp" else 0)
                    pg.close()
            # README に載せる図。印を付けず、サイドバーとトピック詳細が並ぶパソコンの画面を1枚撮る（2026-10-22 かしぇ「図も載せよう」）。
            # README はリポジトリの直下にあるので、図もスキルの外（assets/screenshots/）に置く。スキルを入れた人の手元には要らない
            readme = dashboard.LOGO.parents[3] / "assets" / "screenshots" / "dashboard.webp"
            readme.parent.mkdir(parents=True, exist_ok=True)
            pg = browser.new_page(viewport={"width": PC[0], "height": PC[1]}, device_scale_factor=2)
            pg.goto(url + first)
            pg.wait_for_timeout(1400)
            shoot(pg, readme, [], None, 1440)
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
