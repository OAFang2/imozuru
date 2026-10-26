# The basis for imozuru

This file records which original source supports each rule in `SKILL.md`. The sources were acquired during the 2026-10-09 trial (topic: research on information seeking) by using this skill itself. Line numbers refer to the `.txt` or `.md` produced by `scripts/fetch.py` from the URLs below. PDFs were converted with `pdftotext -layout`, and HTML with `ax`; different conversion methods can shift line numbers, so search for the short quotation. Before changing a rule, reread its source here.

## Source locations

| Filename | URL |
|---|---|
| `bates-1989-berrypicking` | https://www.pages.gseis.ucla.edu/faculty/bates/articles/berrypicking.pdf |
| `ellis-1987-phd-behavioural-model` | https://etheses.whiterose.ac.uk/id/eprint/2975/1/DX093006.pdf |
| `kuhlthau-1991-inside-search-process` | https://tefkos.rutgers-sci.domains/Courses/612/Articles/Kuhlthau_Inside_91.pdf |
| `arxiv-2609.01294-explore-before-committing` | https://arxiv.org/pdf/2609.01294 |
| `arxiv-2608.02009-halt` | https://arxiv.org/pdf/2608.02009 |
| `arxiv-2602.03304-to-search-or-not` | https://arxiv.org/pdf/2602.03304 |
| `kastner-2007-horizon-estimate` | https://pmc.ncbi.nlm.nih.gov/articles/PMC2655834/ |
| `eumesy-unknown-terms` | https://speakerdeck.com/eumesy/examine-the-meaning-of-unknown-terms |
| `arxiv-2505.09316-scent-of-knowledge` | https://arxiv.org/pdf/2505.09316 |

## Rules and sources

| Rule | Source | Location in source |
|---|---|---|
| A question changes during searching; research proceeds by collecting pieces | Bates 1989, “The design of browsing and berrypicking techniques for the online search interface” | `bates-1989-berrypicking.txt` |
| Six following moves: backward chaining, forward chaining, journal browsing, area scanning, subject search, and author search | Bates 1989 | same file, L295–355 |
| Citation chasing avoids the problem of expressing the subject in search terms | Bates 1989 | same file, L297–302 |
| starting / chaining / browsing / differentiating / monitoring / extracting | Ellis 1987 dissertation, the basis of the paywalled 1989 Journal of Documentation paper | `ellis-1987-phd-behavioural-model.txt` L64; contents L165–276 |
| Confusion is normal at the start; extensive notes invite premature closure; list facts that may fit | Kuhlthau 1991, “Inside the Search Process” | `kuhlthau-1991-inside-search-process.txt` L345–368 |
| Committing to one direction reinforces it in later searches; branch hypotheses and compare them first | Zhou et al. 2026, “Explore Before Committing” (HypoSearch), arXiv:2609.01294 | `arxiv-2609.01294-explore-before-committing.txt`, abstract |
| Stop from whether evidence supports every research point, not from confidence | Roh & Han 2026, “HALT”, arXiv:2608.02009 | `arxiv-2608.02009-halt.txt`, abstract |
| Two search errors are over-searching after enough evidence and under-searching before enough evidence | Zhang et al. 2026, “To Search or Not to Search”, arXiv:2602.03304 | `arxiv-2602.03304-to-search-or-not.txt`, abstract |
| Estimate unseen total material from overlap between two independent searches (capture-mark-recapture) | Kastner et al. 2007, “Estimating the Horizon of Articles to Decide When to Stop Searching” | `kastner-2007-horizon-estimate.md` L51 |
| Learn unknown terms from coarse to fine: encyclopedia → specialist encyclopedia → textbook or lecture notes → survey → original work | Sho Yokoi 2017, “Examine the meaning of unknown terms” | `eumesy-unknown-terms.md` L343–373 |
| Choose the next lead by information scent | Pirolli & Card 1999, “Information foraging” (paywalled, DOI 10.1037/0033-295x.106.4.643) | Original not acquired; an LLM application is in `arxiv-2505.09316-scent-of-knowledge.txt` |

## Process facts learned in trials

- Search-result URLs age badly: 5 of 11 were 404 pages, login walls, or HTML pretending to be PDF. Inspect content with `fetch.py`.
- Three papers on LLM search agents cited no human information-seeking work by Bates, Kuhlthau, Ellis, or Marchionini, and only one cited information-foraging theory. The AI-search literature scarcely used this body of work.
- OpenAlex title search works better when restricted to titles and sorted by citations; full-text search ranks commentary and duplicate records highly. Prefer DOI because registered titles sometimes omit subtitles.
- Searcher subagents reliably report what they did not search, which becomes the next round's lead list.

## Canonical works not yet read

- Pirolli & Card 1999 (paywalled); Marchionini 2006, “Exploratory search” (paywalled).
- Case & Given 2016, *Looking for Information*, a textbook frequently cited with Bates and Kuhlthau.
- Wilson 1999, “Models in information behaviour research”; Booth 2010, “How much searching is enough?”; TARCiS 2024 guidance for citation chasing.

## Findings from three trial topics (2026-10-09)

- Topics: information-seeking research (manual parent), editable AI-generated images (paper-rich field, twice), and flat-color painting (a field with only personal sources).
- Points framed only as “does X exist?” all filled in one round while missing the request's central condition. Keep one condition verbatim from the request and make more than half require reading sources.
- `chain.py` follows older citations well, but not citations for papers only about a year old.
- In personal-practice fields, `chain.py` and DOI lookup may be irrelevant; `fetch.py` and web search do the work. Normal search largely missed the painting topic, while deeper search found note, YouTube, and blogs immediately. Important material clustered behind login walls on X, pixiv, and Xiaohongshu.
- Short CJK articles can be genuine. A 2,904-character article failed the 3,000-character English-paper threshold.
- The same applies to PDFs: when more than 30% of non-space characters are CJK, use the 1,000-character HTML threshold.
- Searcher reports usually derive from snippets. In one trial, the report's description of Type-R did not match the source.
