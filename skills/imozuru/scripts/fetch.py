#!/usr/bin/env python3
"""Download one original source, verify it, and save it under sources/.

YouTube captions are stored as timestamped Markdown without downloading video.
X pages fail without fetching because their bodies require login. PMC articles
are retrieved from Europe PMC full-text XML when needed.

Usage:
    fetch.py <sources_dir> <name> <url>
Output (one tab-separated line):
    OK   <name> <saved file> <character count> <first line>
    FAIL <name> <reason>
"""

import codecs
import contextlib
import http.client
import io
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from xml.parsers import expat

# 本文として読める最低の文字数。404 ページや認証の壁は数百〜数千文字に収まる。
# PDFs are commonly long English papers, so use a higher threshold. HTML uses a lower threshold
# so that short Chinese and other CJK articles still pass.
# （大陸塗りの試行で 2,904 字の本物の記事が 3,000 の下限で落ちた）。
MIN_TEXT_CHARS = 3000
MIN_HTML_CHARS = 1000
# Chinese and other CJK PDFs can contain substantive text with fewer characters than English.
# When the share of CJK characters exceeds this ratio, use the HTML threshold.
CJK_RATIO_FOR_PDF = 0.3
# CJK characters: kana, Han characters, and full-width letters, numbers, and punctuation.
_CJK = re.compile(r"[\u3000-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]")
MAX_BYTES = 40_000_000
# 外のコマンド（pdftotext・pdftoppm・tesseract・ax・yt-dlp）1回の時間の上限（秒）。
# 細工した PDF やページで止まらなくなるのを防ぐ。ふつうの論文は数秒〜数十秒で終わる。
TOOL_TIMEOUT = 300
# OCR にかけるページ数の上限。文字の層が無い古い論文は数十ページに収まる。
# 何千ページもある PDF で、画像の書き出しが手元のディスクを埋めないようにする。
OCR_MAX_PAGES = 60
# OCR の読み取りの確信度（tesseract の 0〜100）の、字数で重みを付けた平均の下限。
# 言語データが合わないと、意味のない英字の列が数千字返って字数の下限を通ってしまう（#11）。
# 2026-10-21 の計測: 和書のスキャンを eng で読むと 18〜25、jpn で読むと 72〜82。
# 英語の古い論文（1987・1993）は 87〜96。合成した縦書き・横書きの和文も同じ分かれ方をした。
OCR_MIN_CONFIDENCE = 50
# Add installed language data in preference order: Simplified and Traditional Chinese first;
# Japanese horizontal/vertical packs remain optional compatibility choices (#11).
OCR_EXTRA_LANGS = ("chi_sim", "chi_tra", "jpn", "jpn_vert")
# IMOZURU_OCR_LANG で受け付ける形（eng・jpn+eng など）。tesseract の引数に入るので、ほかの字は通さない
OCR_LANG_FORM = re.compile(r"[A-Za-z_]+(\+[A-Za-z_]+)*")
# 本文ではなく壁に当たったと分かる言い回し。小文字で照合する。
WALL_PATTERNS = (
    "404 not found",
    "page not found",
    "access denied",
    "you do not currently have access",
    "sign in to continue",
    "log in to continue",
    "enable javascript",
    "are you a robot",
    "captcha",
    # Anubis（ボット除けの計算をさせるページ）。HAL などが使う。中身の無い 1,000 字ほどのページが
    # HTML の下限を越えて OK になった（2026-10-20）
    "you're not a bot",
)


class _Redirect308(urllib.request.HTTPRedirectHandler):
    """Python 3.10 でも 308 の転送をたどる。

    3.10 の HTTPRedirectHandler は http_error_308 を持たず、redirect_request も 308 をはねる
    （どちらも 3.11 で足された）。308 は 307 と同じく「同じメソッドのまま別の URL へ」なので、
    307 として扱わせる。3.11 以降では動きは変わらない（openhands.dev などが 308 を返す・#3）。
    """

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 転送の応答の本体は読まずに閉じる。素の http_error_302 は次へ進む前に本体を最後まで
        # 読むので、大きな本体を付けた転送で MAX_BYTES の上限を素通りする（3.12 の実装で確認）
        fp.close()
        problem = url_problem(newurl)
        if problem:
            raise UnsafeURL(f"redirect target {newurl}: {problem}")
        return super().redirect_request(req, fp, 307 if code == 308 else code, msg, headers, newurl)


# 宛先が残っているときに1つの宛先を待つ上限（秒）。IPv6 の経路が無い PC では、IPv6 の宛先ごとに
# 呼び出し側の timeout（fetch は90秒、watch は60秒）まで待ってから IPv4 へ進んでいた（4つの IPv6 で最大6分）
CONNECT_TRY_SECONDS = 10


def _connect_order(addrs: list[str]) -> list[str]:
    """OS が先に返した種類から、IPv6 と IPv4 を交互に並べる（RFC 8305 の4節）。

    片方の種類に経路が無くても、もう片方を2番目に試せる。
    """
    v6 = [a for a in addrs if ":" in a]
    v4 = [a for a in addrs if ":" not in a]
    first, second = (v6, v4) if addrs and ":" in addrs[0] else (v4, v6)
    order = []
    for i in range(max(len(first), len(second))):
        order += first[i:i + 1] + second[i:i + 1]
    return order


def _real_timeout(timeout):
    """既定の timeout の印を、いま使われる秒数（None は待ち続ける）に置き換える。"""
    return socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout


def _dial(ip: str, port: int, timeout, source_address):
    """確かめた IP へつなぐ。テストはここを差し替える。"""
    return socket.create_connection((ip, port), timeout, source_address)


def _pinned_connection(host: str, port: int, timeout, source_address):
    """つなぐ直前に名前を引き直し、確かめた IP へそのままつなぐ。

    url_problem で名前を引いてから urllib がつなぐまでの間に、引き先を 127.0.0.1 などへ
    変えられると検査をすり抜ける（DNS rebinding）。つなぐ時に引いた結果をもう一度確かめ、
    その IP にじかにつなぐので、検査した宛先と実際の宛先がずれない。HTTPS の証明書の確認と
    SNI はホスト名のまま行われる。
    プロキシを通すときは名前をプロキシが引くので、この固定では DNS rebinding を防げない。
    """
    try:
        addrs = list(resolve(host))
    except (OSError, UnicodeError, ValueError) as e:
        raise UnsafeURL(f"cannot resolve host ({e})")
    if os.environ.get("IMOZURU_ALLOW_PRIVATE") != "1":
        for addr in addrs:
            for ip in _addresses_inside(ipaddress.ip_address(addr.split("%", 1)[0])):
                if not ip.is_global:
                    raise UnsafeURL(f"destination resolved to a local or private address immediately before connection ({ip})")
    err = None
    full = _real_timeout(timeout)
    order = _connect_order(addrs)
    cut = []
    for i, addr in enumerate(order):
        short = i < len(order) - 1 and (full is None or full > CONNECT_TRY_SECONDS)
        try:
            sock = _dial(addr, port, CONNECT_TRY_SECONDS if short else timeout, source_address)
        except TimeoutError as e:
            err = e
            if short:
                cut.append(addr)
            continue
        except OSError as e:
            err = e
            continue
        if short:
            # create_connection は待ち時間をそのままソケットに残す。つないだあとの読み込みは元の timeout で待つ
            sock.settimeout(full)
        return sock
    # 短く待って切った宛先は、ほかが全部だめだったときだけ元の timeout で待ち直す。10秒より遅くつながる宛先を失わない
    for addr in cut:
        try:
            return _dial(addr, port, timeout, source_address)
        except OSError as e:
            err = e
    raise err or OSError(f"no address found for {host}")


class _PinnedMixin:
    """接続を _pinned_connection で作る。プロキシを通すとき（CONNECT）はプロキシへ素直につなぐ。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = self._pinned_create

    def _pinned_create(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        if self._tunnel_host:
            return socket.create_connection(address, timeout, source_address)
        return _pinned_connection(address[0], address[1], timeout, source_address)


class _PinnedHTTPConnection(_PinnedMixin, http.client.HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedMixin, http.client.HTTPSConnection):
    pass


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        if req.has_proxy():
            return self.do_open(http.client.HTTPConnection, req)
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        kwargs = {"context": self._context}
        if hasattr(self, "_check_hostname"):  # 3.11 まで
            kwargs["check_hostname"] = self._check_hostname
        return self.do_open(_PinnedHTTPSConnection, req, **kwargs)


_OPENER = urllib.request.build_opener(_Redirect308, _PinnedHTTPHandler, _PinnedHTTPSHandler)


class UnsafeURL(Exception):
    """取りに行ってはいけない URL。呼び手はほかの取得の失敗と同じく FAIL にする。"""


def resolve(host: str) -> list[str]:
    """ホスト名を引いて IP アドレスを返す。テストはここを差し替える。

    OS が返した順（IPv6 と IPv4 のどちらを先に試すか）を保つ。_pinned_connection はこの順の先頭の種類から、
    IPv6 と IPv4 を交互につなぐ。
    """
    return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, None)))


def url_problem(url: str) -> str | None:
    """取りに行ってはいけない URL なら理由を、よければ None を返す。

    URL も転送の先も、検索結果のページを書いた他人が決める。127.0.0.1 や社内の宛先を書かれると、
    手元で動いているサービス（開発用のサーバー・ローカルの AI・ブラウザのデバッグ口）の中身を
    sources/ に写してしまう（2026-10-20 のレビュー）。名前を引いた先が全部インターネット上の
    宛先のときだけ通す。10進や16進で書いた IP も、名前を引けば 127.0.0.1 などになるので同じく止まる。
    名前を引いてから接続するまでの間に引き先を変える手（DNS rebinding）は、_pinned_connection が
    つなぐ直前に引き直して確かめた IP へじかにつなぐことで防ぐ。
    プロキシを通すときは名前をプロキシが引くので、この固定では DNS rebinding を防げない。

    ホストの部分に「@」か「%」があれば断る。urlsplit は「x@a.example」の a.example を見るが、
    urllib は x@a.example をそのまま引き、「%61」は urllib だけが a に戻す。検査とつなぐ先がずれる。
    VPN などで外の名前まで内側の宛先に引ける環境では、環境変数 IMOZURU_ALLOW_PRIVATE=1 で
    宛先の検査だけを外せる（http(s) の検査は残る）。
    """
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname
    except ValueError:
        return "malformed URL"
    if p.scheme.lower() not in {"http", "https"}:
        return "only http(s) URLs may be fetched"
    if not host:
        return "URL has no host"
    if "@" in p.netloc or "%" in p.netloc:
        return "URL host contains @ or %"
    if os.environ.get("IMOZURU_ALLOW_PRIVATE") == "1":
        return None
    try:
        addrs = resolve(host)
    except (OSError, UnicodeError, ValueError) as e:
        return f"cannot resolve host ({e})"
    for addr in addrs:
        for ip in _addresses_inside(ipaddress.ip_address(addr.split("%", 1)[0])):  # IPv6 の %eth0 を外す
            if not ip.is_global:
                return (f"refusing local or private address ({ip}); "
                        "IMOZURU_ALLOW_PRIVATE=1 disables this check for VPN environments that resolve public names privately")
    return None


def _addresses_inside(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list:
    """IP と、その IPv6 の中に埋め込まれた IPv4 を返す。

    64:ff9b::7f00:1（NAT64）や ::127.0.0.1 は IPv6 としては「インターネット上」に数えられるが、
    中の IPv4 は 127.0.0.1。NAT64 のある回線では手元へ届く（2026-10-20 の2回目のレビュー）。
    """
    found = [ip]
    if ip.version == 6:
        low32 = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if ip.ipv4_mapped:
            found.append(ip.ipv4_mapped)
        if ip.sixtofour:
            found.append(ip.sixtofour)
        if ip.teredo:
            found.extend(ip.teredo)
        if (ip in ipaddress.ip_network("::/96") or ip in ipaddress.ip_network("64:ff9b::/96")
                or ip in ipaddress.ip_network("64:ff9b:1::/48")):
            found.append(low32)
    return found


def ascii_url(url: str) -> str:
    """パスとクエリにそのまま入った日本語の字などを % で符号化する。

    urllib は要求の行を ASCII で組むので、辞書の項目名や自治体のファイル名を符号化せずに
    書いたリンクは UnicodeEncodeError で止まる（和竿の漆塗りの試行で 36 本中 2 本）。
    safe に % を入れるので、符号化済みの URL は変わらない。国際化ドメイン名は扱わない。
    """
    p = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(p.path, safe="/%:@!$&'()*+,;=~")
    query = urllib.parse.quote(p.query, safe="=&%/:+?@!$'()*,;~")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, p.fragment))


# ふだんの名乗り。ブラウザでないと本文を返さないサイトがあるので、ブラウザの形で始める。
BROWSER_UA = "Mozilla/5.0 imozuru"
# HAL（hal.science）は、ブラウザの名乗りには閲覧用の HTML を、それ以外の名乗りには PDF を返す。
# PDF を頼み直すときはこちらで名乗る（2026-10-20 に確かめた）。
PLAIN_UA = "imozuru"


def download(url: str, user_agent: str = BROWSER_UA) -> tuple[bytes, str]:
    """URL を取りに行き、本体と Content-Type を返す。

    MAX_BYTES を超える本体や、Content-Length に届かずに切れた本体は、取得ごと失敗にする。
    切れた原文を OK として残すと、後ろの部分を引用できないのに「原文に無い」と読み違える。
    read(n) は接続が途中で切れても例外を出さないので、残りの長さ（res.length）で確かめる。
    """
    problem = url_problem(url)
    if problem:
        raise UnsafeURL(problem)
    req = urllib.request.Request(ascii_url(url), headers={"User-Agent": user_agent})
    with _OPENER.open(req, timeout=90) as res:
        body = res.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError(f"body exceeds {MAX_BYTES:,} bytes")
        if isinstance(res.length, int) and res.length > 0:
            raise ValueError("body was truncated")
        return body, res.headers.get("Content-Type", "")


def pdf_save(url: str, out: Path) -> bool:
    """名乗りを変えて頼み直し、PDF が返ってきたら out に保存して True を返す。

    前は ax に取り直させていた（ax の名乗りは Bun/…）。ax は自分で通信し、転送の先を
    こちらで確かめられないので、検査つきの download で頼み直す。
    """
    try:
        body, _ = download(url, user_agent=PLAIN_UA)
    except Exception:  # 取れなければ HTML のまま進める
        return False
    if body[:5] != b"%PDF-":
        return False
    out.write_bytes(body)
    return True


# Shift_JIS と名乗るページの多くは実際には Windows の拡張（①・髙 など）を含むので、上位互換の cp932 で読む
_CHARSET_ALIASES = {"shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932",
                    "windows-31j": "cp932", "ms_kanji": "cp932"}
# 名乗りを信じてよい文字コード。文書に使われるものだけに絞る。名乗りはページを書いた他人が決めるので、
# punycode のように長さの2乗の時間がかかるもの（40MB で何時間も止まる）や、utf-7・unicode_escape の
# ように文書の文字コードではないものを試させない（2026-10-20 の2回目のレビュー）。
_DOCUMENT_CODECS = {codecs.lookup(n).name for n in (
    "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be",
    "ascii", "cp932", "shift_jis", "euc_jp", "euc_jis_2004", "iso2022_jp", "iso2022_jp_2",
    "gb2312", "gbk", "gb18030", "big5", "big5hkscs", "euc_kr", "cp949", "iso2022_kr",
    "latin-1", "iso8859_2", "iso8859_3", "iso8859_4", "iso8859_5", "iso8859_6", "iso8859_7",
    "iso8859_8", "iso8859_9", "iso8859_10", "iso8859_13", "iso8859_14", "iso8859_15", "iso8859_16",
    "cp1250", "cp1251", "cp1252", "cp1253", "cp1254", "cp1255", "cp1256", "cp1257", "cp1258",
    "koi8_r", "koi8_u", "mac_roman", "tis_620", "cp874")}
# 先頭の BOM で分かる文字コード。UTF-32 の BOM は UTF-16 の BOM で始まるので先に見る
_BOMS = ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
         (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))


def _declared_charsets(body: bytes, ctype: str) -> list[str]:
    """Content-Type と HTML の meta が名乗る文字コードを、この順に返す。"""
    found = re.findall(r"charset\s*=\s*[\"']?([\w.:-]+)", ctype or "", re.I)
    # meta はファイルの頭に置かれる決まり（HTML 仕様で先頭 1024 バイト以内）。余裕を見て 4096 まで見る
    found += re.findall(rb"<meta[^>]*charset\s*=\s*[\"']?([\w.:-]+)", body[:4096], re.I)
    names = [(n.decode("ascii", "ignore") if isinstance(n, bytes) else n).lower() for n in found]
    return [_CHARSET_ALIASES.get(n, n) for n in names]


def decode_text(body: bytes, ctype: str = "") -> str:
    """本文を、書かれている文字コードで文字にする。

    いつも UTF-8 で読んでいたため、Shift_JIS の日本語記事が U+FFFD の羅列になって中身が消えた
    （pju-hoge さんのフォークで見つかった・2026-10-15）。名乗りは嘘のこともあるので、
    BOM → UTF-8 → 名乗った文字コード → 日本語の定番の順に「化けずに読めるか」で試し、最初に通ったものを使う。
    どれも通らなければ、置換文字のいちばん少ないものを採る。

    UTF-8 を名乗りより先に試すのは、latin-1 のようにどんな並びでも読めてしまう名乗りがあるため。
    UTF-8 の文法に合う並びは、ほかの文字コードの文章からはまず偶然には生まれない。
    名乗りはページを書いた他人が決めるので、_DOCUMENT_CODECS に無いものは飛ばす
    （zlib_codec と名乗るだけで fetch.py が LookupError で止まった・2026-10-20 のレビュー）。
    """
    for bom, codec in _BOMS:
        if body.startswith(bom):
            try:
                return body.decode(codec)
            except UnicodeError:
                break
    candidates = []
    for name in ["utf-8", *_declared_charsets(body, ctype), "cp932", "euc_jp"]:
        try:
            info = codecs.lookup(name)
        except LookupError:  # 知らない名乗り（x-user-defined など）は飛ばす
            continue
        if info.name not in _DOCUMENT_CODECS:
            continue
        if info.name not in candidates:
            candidates.append(info.name)
    for codec in candidates:
        try:
            return body.decode(codec)
        except UnicodeError:
            continue
    decoded = []
    for codec in candidates:
        try:
            decoded.append(body.decode(codec, errors="replace"))
        except UnicodeError:  # idna など、置き換えでも読めない文字コードがある
            continue
    return min(decoded, key=lambda t: t.count("�"))


def mostly_cjk(text: str) -> bool:
    """本文の空白以外の字のうち、和文の字の割合が CJK_RATIO_FOR_PDF を超えるか。"""
    chars = re.sub(r"\s", "", text)
    return bool(chars) and len(_CJK.findall(chars)) / len(chars) > CJK_RATIO_FOR_PDF


def missing(*commands: str) -> list[str]:
    """手元に無い外部コマンドの名前を返す。無いまま呼ぶと FileNotFoundError で止まるため。"""
    return [c for c in commands if shutil.which(c) is None]


def run_tool(cmd: list[str], capture_output: bool = False, **kwargs) -> subprocess.CompletedProcess | None:
    """外のコマンドを TOOL_TIMEOUT 秒の上限つきで動かす。上限を超えたら子の処理ごと止めて None を返す。

    subprocess.run の timeout は、直接動かしたコマンドしか止めない。Windows で ax や yt-dlp が
    .cmd を経由して動くと、孫の処理が出力の管をつかんだまま残り、run が戻らなくなる
    （2026-10-20 の2回目のレビュー）。新しい組で動かし、時間切れのときは組ごと止める。
    """
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with subprocess.Popen(cmd, **kwargs) as proc:
        try:
            out, err = proc.communicate(timeout=TOOL_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return None
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _kill_tree(proc: subprocess.Popen) -> None:
    """proc と、そこから動いた処理をまとめて止める。管は読まない（孫がつかんでいると戻らない）。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()


def pdf_to_text(pdf: Path) -> str:
    """pdftotext で段組みを保ったまま文字にする。行番号で原文を指せるように -layout を使う。

    Xpdf の pdftotext（Git for Windows に入っている）は既定で Latin-1 を出すので、
    -enc UTF-8 で出力の文字コードを決める。poppler の pdftotext も同じ引数を受け付ける（#3）。
    """
    if missing("pdftotext"):
        return ""
    out = pdf.with_suffix(".txt")
    done = run_tool(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), str(out)],
                    stderr=subprocess.DEVNULL)
    # 時間切れの書きかけ、途中で失敗した書きかけ、圧縮を解くと何 GB にもなる PDF の文字は使わない。
    # 書きかけを使うと、後半を引用できないのに「原文に無い」と読み違える
    if done is None or done.returncode != 0 or (out.exists() and out.stat().st_size > MAX_BYTES):
        out.unlink(missing_ok=True)
        return ""
    return out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""


def ocr_langs() -> str:
    """Choose Tesseract languages.

    IMOZURU_OCR_LANG overrides automatic selection. Otherwise start with English and append
    installed packs in OCR_EXTRA_LANGS order: chi_sim, chi_tra, then optional Japanese packs.
    If the installed-language list cannot be read, retain the established eng-only fallback.
    Match complete lines so jpn is not confused with jpn_vert; older Tesseract releases may
    print their language list to stderr, so inspect both streams.
    """
    chosen = os.environ.get("IMOZURU_OCR_LANG", "").strip()
    if chosen:
        if OCR_LANG_FORM.fullmatch(chosen):
            return chosen
        print(f"Ignoring malformed IMOZURU_OCR_LANG: {clean(chosen)}", file=sys.stderr)
    res = run_tool(["tesseract", "--list-langs"], capture_output=True, encoding="utf-8", errors="replace")
    have = set() if res is None else {line.strip() for line in f"{res.stdout}\n{res.stderr}".splitlines()}
    return "+".join([lang for lang in OCR_EXTRA_LANGS if lang in have] + ["eng"])


def ocr_pdf(pdf: Path) -> tuple[str, str]:
    """文字の層が無い PDF（紙を読み取った画像だけの古い論文）を、手元の tesseract で文字にする。

    Spencer ら 1995 の PDF は取れたのに pdftotext が1文字しか返さなかった（2026-10-10）。
    1ページずつ 200dpi の画像にして、ocr_langs() の言語で読む。OCR_MAX_PAGES を超えるページは起こさない。
    返すのは (本文, 使わなかった理由)。次のときは本文を空にして理由を返す。
    - 時間切れか失敗で読めなかったページがある。欠けたまま残すと、後半が「原文に無い」と読み違える
    - 確信度の平均が OCR_MIN_CONFIDENCE より低い。言語データが合わず意味のない字の列になっている（#11）
    pdftoppm か tesseract が無ければ何もせず空を返す（呼び手が FAIL に無いコマンドを書く）。
    .txt には書かない。書くかどうかは呼び手（ocr_if_longer）が決める。
    """
    if missing("pdftoppm", "tesseract"):
        return "", ""
    tmp = pdf.parent / f".{pdf.stem}-ocr"
    tmp.mkdir(exist_ok=True)
    try:
        done = run_tool(["pdftoppm", "-r", "200", "-l", str(OCR_MAX_PAGES), "-png", str(pdf), str(tmp / "p")],
                        stderr=subprocess.DEVNULL)
        if done is None:
            return "", "timed out while rendering the PDF as images"
        # 途中で落ちると、書き出せたページの画像だけが残る。残りを OCR すると欠けた本文が成功扱いになる（#14）
        if done.returncode != 0:
            return "", f"failed while rendering the PDF as images (pdftoppm exit code {done.returncode})"
        langs = ocr_langs()
        pages, failed, weighted, chars = [], [], 0.0, 0
        for img in sorted(tmp.glob("p-*.png")):
            number = img.stem.split("-")[-1]
            out = tmp / img.stem
            # 字の間の空白を残す設定が無いと、和文の字のあいだに空白が入る（#11）
            res = run_tool(["tesseract", str(img), str(out), "-l", langs,
                            "-c", "preserve_interword_spaces=1",
                            "-c", "tessedit_create_txt=1", "-c", "tessedit_create_tsv=1"],
                           capture_output=True, encoding="utf-8", errors="replace")
            txt, tsv = out.with_suffix(".txt"), out.with_suffix(".tsv")
            if res is None or res.returncode != 0 or not txt.exists():
                failed.append(number)
                continue
            pages.append(f"=== page {number} ===\n{txt.read_text(encoding='utf-8', errors='replace')}")
            # tsv の列: … conf（11列目）・text（12列目）。単語でない行の conf は -1
            for row in tsv.read_text(encoding="utf-8", errors="replace").splitlines()[1:] if tsv.exists() else []:
                cols = row.split("\t")
                if len(cols) >= 12 and cols[11].strip():
                    try:
                        conf = float(cols[10])
                    except ValueError:
                        continue
                    if conf >= 0:
                        weighted += conf * len(cols[11].strip())
                        chars += len(cols[11].strip())
    finally:
        for leftover in tmp.iterdir():  # 読み終えた画像と、時間切れで止めた pdftoppm の書きかけ
            leftover.unlink()
        tmp.rmdir()
    if failed:
        return "", f"OCR could not read pages {','.join(failed)}"
    if chars and weighted / chars < OCR_MIN_CONFIDENCE:
        return "", (f"OCR confidence is low (average {weighted / chars:.0f}; languages {langs}). "
                    "The language data may not match (for Chinese, install chi_sim and optionally chi_tra; "
                    "Japanese packs jpn and jpn_vert are optional)")
    return "\n".join(pages), ""


def ocr_if_longer(pdf: Path, text: str) -> tuple[str, str]:
    """文字の層が薄い PDF を OCR にかけ、OCR のほうが長いときだけ .txt を置き換えて返す。

    OCR の道具が無いときや起こせなかったときに、pdftotext が取れた分まで空で上書きしていた
    （2026-10-20 のレビュー）。OCR の誤りは残るので、.txt の先頭に OCR だと書いて、
    読む人が原本と照らせるようにする。返すのは (本文, OCR を使わなかった理由)。
    """
    ocr, note = ocr_pdf(pdf)
    if len(ocr.strip()) <= len(text.strip()):
        return text, note
    pdf.with_suffix(".txt").write_text(
        f"(OCR text. Pages after {OCR_MAX_PAGES} were not processed. "
        "OCR errors may remain; verify quotations against the original PDF.)\n\n" + ocr, encoding="utf-8")
    return ocr, ""


HAS_AX = shutil.which("ax") is not None


def md_link(text: str, href: str) -> str:
    """原文のリンクを、開いても危険な処理が動かず markdown の形が崩れないように残す。"""
    href = href.strip()
    # JavaScript: や data: のリンクは、.md を開いた先で押すとページの中で動く。http(s) だけ残す。
    # 全角の「＃」などを含む URL は urlsplit が ValueError を出す。リンク1本でページ全体を FAIL にしない
    try:
        scheme = urllib.parse.urlsplit(href).scheme.lower()
    except ValueError:
        return text
    if scheme not in {"http", "https"}:
        return text
    # 括弧と空白が URL の中にあると、[文字](URL) の形がそこで切れる
    href = re.sub(r"[()\s]", lambda m: urllib.parse.quote(m.group(0), safe=""), href)
    # 「\」を先に符号化する。後にすると、元からある「\]」が「\\]」になって「]」が効き直す
    text = text.replace("\\", "\\\\").replace("[", r"\[").replace("]", r"\]")
    return f"[{text}]({href})"


class _TextExtractor(HTMLParser):
    """ax が無い環境で、HTML から本文の文字を取り出す。

    ax ほど本文を見分けられない。<main> か <article> があって中身が十分なら、その中だけを使う。
    無いページではナビゲーションやフッターの文字も混ざるが、行番号で原文を指すには足りる。
    リンクは「後ろへ」たどる手がかりなので `[文字](URL)` で残す。
    """

    FOCUS = {"main", "article"}

    SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}
    BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside",
             "li", "tr", "br", "hr", "pre", "blockquote", "table", "ul", "ol", "dl", "dt", "dd",
             "figure", "figcaption"}
    HEADING = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts: list[str] = []
        self.skip = 0
        self.href: str | None = None
        self.link_text: list[str] = []
        self.focus = 0
        self.focus_parts: list[str] = []

    def _emit(self, piece: str) -> None:
        self.parts.append(piece)
        if self.focus:
            self.focus_parts.append(piece)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif self.skip:
            return
        elif tag in self.FOCUS:
            self.focus += 1
        elif tag in self.HEADING:
            self._emit("\n\n" + self.HEADING[tag])
        elif tag in self.BLOCK:
            self._emit("\n")
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if href and not href.startswith("#"):
                try:
                    self.href = urllib.parse.urljoin(self.base, href)
                except ValueError:  # 閉じていない「[」などの壊れた href。リンクにせず文字だけ残す
                    return
                self.link_text = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif tag in self.FOCUS:
            self.focus = max(0, self.focus - 1)
        elif tag in self.HEADING or tag in self.BLOCK:
            self._emit("\n")
        elif tag == "a" and self.href is not None:
            text = " ".join("".join(self.link_text).split())
            self._emit(md_link(text, self.href) if text else "")
            self.href = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.href is not None:
            self.link_text.append(data)
        else:
            self._emit(data)

    def text(self) -> str:
        focused = "".join(self.focus_parts)
        # 中身の薄い <main>（ページの殻だけ）を掴んだときは全体に戻す
        raw = focused if len(focused.strip()) >= MIN_HTML_CHARS else "".join(self.parts)
        lines = (" ".join(line.split()) for line in raw.splitlines())
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def html_to_md(url: str, out: Path, body: bytes | None = None, ctype: str = "") -> str:
    """本文を markdown にする。要約はしない。

    取得はいつも download（宛先の検査つき）で行う。ax には取ってきた HTML をファイルで渡し、
    markdown への変換（--all で全文）だけをさせる。ax に URL を渡すと ax が自分で通信し、
    たどった転送の先をこちらで確かめられない（2026-10-20 の2回目のレビュー）。
    ax はファイルを UTF-8 として読むので、こちらで文字にしてから UTF-8 で書いて渡す。
    ax が無ければ標準ライブラリで文字を取り出す。
    body を渡されたら取り直さずにそれを使う。ctype は body の Content-Type（文字コードの名乗りを読む）。
    """
    if body is None:
        body, ctype = download(url)
    if body[:5] == b"%PDF-":
        # 転送先が PDF だった。呼び手が「%PDF」で気づいて PDF として取り直す
        text = "%PDF"
    elif HAS_AX:
        page = out.with_name(f".{out.stem}-page.html")
        page.write_text('<meta charset="utf-8">' + decode_text(body, ctype), encoding="utf-8")
        # 置き場は作業用の一時フォルダで「-」では始まらないが、念のため「--」の後ろに置く
        res = run_tool(["ax", "--md", "--all", "--", str(page)], capture_output=True,
                       encoding="utf-8", errors="replace")
        page.unlink(missing_ok=True)
        text = res.stdout if res else ""
    else:
        parser = _TextExtractor(url)
        parser.feed(decode_text(body, ctype))
        text = parser.text()
    out.write_text(text, encoding="utf-8")
    return text


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                 "youtube-nocookie.com", "www.youtube-nocookie.com"}
ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
X_POST = re.compile(r"^https?://(www\.)?(x|twitter)\.com/")
# 字幕は数分の動画でも千文字に届かないことがあるので、論文より低い下限を使う。
MIN_TRANSCRIPT_CHARS = 300


def is_youtube(url: str) -> bool:
    """YouTube の動画の URL か。ホスト名で見る。

    URL のどこかに「youtu.be/」があれば YouTube と見なしていたので、
    https://evil.example/?next=youtu.be/ のような他人のページまで yt-dlp に渡っていた
    （2026-10-20 のレビュー）。yt-dlp は何でも読む汎用の読み取りを持っているので、YouTube だけに絞る。
    """
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower().rstrip(".")
    if host == "youtu.be":
        return len(p.path) > 1
    return host in YOUTUBE_HOSTS and re.match(r"/(watch|shorts/|live/|embed/)", p.path) is not None


def arxiv_id(url: str) -> str | None:
    """arXiv の論文の URL なら番号を返す。ホスト名で見る。

    URL のどこかに「arxiv.org/abs/…」があれば当てていたので、他人のページの URL に
    書き込まれた番号の論文を、そのページの原文として保存しえた（2026-10-20 の2回目のレビュー）。
    """
    p = urllib.parse.urlsplit(url)
    if (p.hostname or "").lower().rstrip(".") not in ARXIV_HOSTS:
        return None
    m = re.match(r"/(?:pdf|abs)/(\d{4}\.\d{4,5}(?:v\d+)?)", p.path)
    return m.group(1) if m else None


def youtube_to_md(url: str, out: Path) -> str:
    """Download captions only (never video) and convert them to timestamped text.

    Prefer manual English and Chinese captions. If none exist, request only the video's
    original-language automatic captions and remove repeated adjacent cues. Record whether
    captions are manual or automatic so readers know when to verify a quotation against video.

    Do not request machine-translated automatic captions: yt-dlp can receive a YouTube 429 on
    the translated track and stop before preserving the original track. The manual pass also
    obtains the spoken-language metadata; the automatic pass requests only that language's
    ``-orig`` track. If it is unknown, fall back to ``en-orig,zh-orig``. Explicit UTF-8 output
    avoids cp932 metadata output on Windows.
    """
    tmp = out.parent / f".{out.stem}-subs"
    tmp.mkdir(exist_ok=True)
    kind, langs = "manual", "en,zh,zh-Hans,zh-Hant"
    printed = ""
    for flag in ("--write-subs", "--write-auto-subs"):
        meta = run_tool(
            # --no-playlist: watch?v=…&list=… の URL で、再生リストの全部の字幕を取りに行かない
            ["yt-dlp", "--no-update", "--no-playlist", "--skip-download", flag, "--encoding", "utf-8",
             "--sub-langs", langs, "--sub-format", "vtt", "--no-simulate",
             "--print", "%(title)s\t%(uploader)s\t%(upload_date)s\t%(language|)s",
             "-o", str(tmp / "v.%(ext)s"), "--", url],
            capture_output=True, encoding="utf-8", errors="replace")
        if meta is None:  # 時間切れ。字幕なしとして呼び手が FAIL にする
            break
        printed = meta.stdout
        if any(tmp.glob("*.vtt")):
            break
        kind = "automatic (may contain errors; verify quotations against the video)"
        # Metadata may say en-US while the caption is en-orig. If unknown, prefer original English and Chinese.
        spoken = printed.rstrip("\n").split("\t")[-1].split("-")[0]
        # 言語の名乗りは動画の持ち主が決める。「all」などで字幕を全部落とさせないよう、言語の形だけ通す
        langs = f"{spoken}-orig" if re.fullmatch(r"[a-z]{2,3}", spoken) else "en-orig,zh-orig"
    vtts = sorted((p for p in tmp.glob("*.vtt") if p.stat().st_size <= MAX_BYTES),
                  key=lambda p: (not any(tag in p.name for tag in (".en", ".zh-Hans", ".zh-Hant", ".zh")), p.name))
    lines, last = [], ""
    if vtts:
        stamp = "00:00"
        for raw in vtts[0].read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"(\d\d):(\d\d):(\d\d)\.\d+ -->", raw)
            if m:
                h, mi, s = (int(x) for x in m.groups())
                stamp = f"{h * 60 + mi:02d}:{s:02d}"
                continue
            # [^<>] にするのは、「<」が並んで「>」の無い行で、1つずつ行末まで読み直して遅くなるのを防ぐため
            text = re.sub(r"<[^<>]*>", "", raw).strip()
            if not text or text in ("WEBVTT",) or text.startswith(("Kind:", "Language:")):
                continue
            if text != last:
                lines.append(f"[{stamp}] {text}")
                last = text
    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()
    head = printed.strip().split("\t")
    title = head[0] if head and head[0] else url
    info = " / ".join(head[1:3]) if len(head) > 1 else ""
    body = "\n".join(lines)
    out.write_text(f"# {title}\n\n{url}\n{info}\nCaptions: {kind + ' ' + vtts[0].name if vtts else 'none'}\n\n{body}\n",
                   encoding="utf-8")
    return body


PMC_PATHS = {"pmc.ncbi.nlm.nih.gov": r"/articles/(PMC\d+)", "www.ncbi.nlm.nih.gov": r"/pmc/articles/(PMC\d+)",
             "europepmc.org": r"/article/PMC/(PMC\d+)"}


def pmc_id(url: str) -> str | None:
    """PMC の記事の URL なら PMCID を返す。arxiv_id と同じく、URL のどこかではなくホスト名とパスで見る。"""
    p = urllib.parse.urlsplit(url)
    pattern = PMC_PATHS.get((p.hostname or "").lower().rstrip("."))
    m = re.fullmatch(pattern + r"/?", p.path, re.I) if pattern else None
    return m.group(1).upper() if m else None


EUROPE_PMC_XML = "https://www.ebi.ac.uk/europepmc/webservices/rest/{}/fullTextXML"
# 長い論文にも余裕を持たせつつ、小さな XML でメモリや再帰の上限を使い切らせない。
JATS_MAX_ELEMENTS = 200_000
JATS_MAX_DEPTH = 200
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
# 段落の中の飾りや参照。前後に空白を足すと「Smith et al. ( 2020 )」のように崩れるので、そのままつなぐ
_JATS_INLINE = {"italic", "bold", "sup", "sub", "sc", "underline", "monospace", "roman", "strike",
                "overline", "xref", "ext-link", "uri", "named-content", "styled-content",
                "inline-formula", "abbrev"}
# 本文の文字ではないもの（図の差し替え用の説明や ID）
_JATS_SKIP = {"alt-text", "object-id", "graphic", "inline-graphic", "media"}
# 見出しを持つ入れ物。title が無ければ下の既定の見出しを使う（無いものは見出しなしで中へ）
_JATS_SECTIONS = {"sec": "", "abstract": "Abstract", "trans-abstract": "Abstract", "ack": "Acknowledgements",
                  "app": "Appendix", "ref-list": "References", "glossary": "Glossary", "boxed-text": ""}


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _jats_raw(el) -> str:
    parts = [el.text or ""]
    for child in el:
        tag = _local(child.tag)
        if tag in _JATS_SKIP:
            inner = ""
        elif tag == "alternatives":
            # 同じ式が TeX・MathML・画像で並ぶ。読める TeX を1つだけ残す
            kids = list(child)
            pick = next((k for k in kids if _local(k.tag) == "tex-math"), kids[0] if kids else None)
            inner = _jats_raw(pick) if pick is not None else ""
        else:
            inner = _jats_raw(child).strip()
            href = child.get(_XLINK_HREF)
            if tag in ("ext-link", "uri") and href and href != inner:
                inner = md_link(inner, href)
        parts.append(inner if tag in _JATS_INLINE else f" {inner} ")
        parts.append(child.tail or "")
    return "".join(parts)


def _jats_flat(el) -> str:
    """要素の文字を1行にする。文献の <surname>Smith</surname><given-names>J</given-names> も語の間を空ける。"""
    if el is None:
        return ""
    text = " ".join(_jats_raw(el).split())
    return re.sub(r"([(\[])\s+", r"\1", re.sub(r"\s+([,.;:)\]])", r"\1", text))


def _jats_blocks(el, level: int, out: list[str]) -> None:
    """本文の入れ物をたどり、見出しは1行、段落・図表の説明・文献は1つずつ1行にする。"""
    for child in el:
        tag = _local(child.tag)
        if tag in ("title", "label") or tag in _JATS_SKIP:
            continue  # 見出しは親が書く
        if tag in _JATS_SECTIONS:
            title = _jats_flat(child.find("title")) or _JATS_SECTIONS[tag]
            label = _jats_flat(child.find("label"))
            if title:
                out += ["", "#" * min(level, 6) + " " + " ".join(filter(None, [label, title])), ""]
            _jats_blocks(child, level + 1 if title else level, out)
        elif tag in ("p", "disp-formula", "disp-quote", "def-item", "fn", "statement"):
            line = _jats_flat(child)
            if line:
                out.append(line)
        elif tag in ("list",):
            out += ["- " + _jats_flat(item) for item in child if _local(item.tag) == "list-item"]
        elif tag in ("fig", "table-wrap", "supplementary-material"):
            label = _jats_flat(child.find("label")) or {"fig": "Figure", "table-wrap": "Table"}.get(tag, "Supplement")
            caption = _jats_flat(child.find("caption"))
            out.append(f"[{label}] {caption}".rstrip())
            for row in child.iter():
                if _local(row.tag) == "tr":
                    out.append(" | ".join(_jats_flat(cell) for cell in row))
            for foot in child.findall("table-wrap-foot"):
                _jats_blocks(foot, level, out)
        elif tag == "ref":
            label = _jats_flat(child.find("label"))
            cites = [c for c in child if _local(c.tag) != "label"]
            text = " ".join(_jats_flat(c) for c in cites)
            doi = next((p.text for p in child.iter() if _local(p.tag) == "pub-id"
                        and p.get("pub-id-type") == "doi" and p.text), None)
            if doi and f"doi.org/{doi.strip()}" not in text:
                text += f" https://doi.org/{doi.strip()}"
            out.append(" ".join(filter(None, [f"[{label}]" if label else "", text])))
        else:
            _jats_blocks(child, level, out)  # body・back・fn-group・notes などはそのまま中へ


def jats_to_md(xml: bytes, url: str, xml_url: str) -> str:
    """Europe PMC の全文 XML（JATS）を、見出し・段落ごとの行にした markdown にする。要約はしない。

    XML をそのまま文字にすると、雑誌名・ID・著者が区切りなく1行目につながって読めなかった
    （2026-10-19 の試しで Dully 2021 など）。題名と書誌は頭の数行にまとめ、本文は行番号で指せる形にする。
    """
    def refuse_entity(*args):
        raise ValueError("XML declares an ENTITY")

    count = depth = 0

    def start(tag, attrs):
        nonlocal count, depth
        count += 1
        depth += 1
        if count > JATS_MAX_ELEMENTS or depth > JATS_MAX_DEPTH:
            raise ValueError("XML element count or nesting depth exceeds the limit")

    def end(tag):
        nonlocal depth
        depth -= 1

    # JATS の DOCTYPE は許すが、実体の宣言・外部参照は文字コードに関係なく断る。
    # 木を作る前に上限も検査し、細工した XML でメモリを使い切らせない。外部 DTD は読まない。
    parser = expat.ParserCreate()
    parser.EntityDeclHandler = parser.ExternalEntityRefHandler = refuse_entity
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(xml, True)
    root = ET.fromstring(xml)
    meta = root.find("front/article-meta")
    if meta is None:
        meta = ET.Element("article-meta")
    journal = _jats_flat(root.find("front/journal-meta/journal-title-group/journal-title"))
    year = next((y.text for y in meta.iter("year") if y.text), "")
    ids = {a.get("pub-id-type"): (a.text or "").strip() for a in meta.findall("article-id")}
    requested = pmc_id(url)
    if requested and any(not re.fullmatch(r"(?:PMC)?" + requested[3:], (a.text or "").strip(), re.I)
                         for a in meta.findall("article-id") if a.get("pub-id-type") == "pmcid"):
        raise ValueError("XML PMCID does not match the requested article")
    authors = []
    for contrib in meta.iter("contrib"):
        if contrib.get("contrib-type") != "author":
            continue
        name = contrib.find("name")
        authors.append(" ".join(filter(None, [_jats_flat(name.find("given-names")),
                                              _jats_flat(name.find("surname"))]))
                       if name is not None else _jats_flat(contrib.find("collab")))
    out = [f"# {_jats_flat(meta.find('title-group/article-title')) or url}", "", url,
           f"Full text: Europe PMC XML ({xml_url})",
           " / ".join(filter(None, [journal, year, ids.get("doi") and f"https://doi.org/{ids['doi']}",
                                    ids.get("pmcid")])),
           "Authors: " + ", ".join(a for a in authors if a)]
    keywords = [_jats_flat(k) for k in meta.iter("kwd")]
    if keywords:
        out.append("Keywords: " + ", ".join(keywords))
    # 要旨は article-meta の中にあるので、見出しつきの節として本文・巻末と同じ流れに乗せる
    parts = ET.Element("article")
    parts.extend([*meta.findall("abstract"), *meta.findall("trans-abstract"),
                  *(n for n in (root.find("body"), root.find("back")) if n is not None)])
    _jats_blocks(parts, 2, out)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def clean(s: str) -> str:
    """端末に出す文字列から、制御文字（色やクリップボードを操る ESC の並び）と改行の仲間を空白にする。

    他人が決めた文字列（本文・転送先の URL・HTTP のエラーの文）は、1行の出力の列や行を増やしたり、
    端末を操ったりできる（2026-10-20 の2回目のレビュー）。
    文字の向きを変える制御文字（U+202E など）と幅の無い空白も消す。ZWJ・ZWNJ は絵文字や一部の文字で要るので残す。
    """
    return re.sub(r"[\s\x00-\x1f\x7f-\x9f  \u200b\u200e\u200f\u202a-\u202e\u2060-\u2069\ufeff]+", " ", s).strip()


def first_line(text: str) -> str:
    """本文の最初の意味のある1行。端末に出すので clean() を通す。"""
    for line in text.splitlines():
        line = clean(line)
        if len(line) > 8:
            return line[:100]
    return ""


def save_verified(saved: Path, kept: Path, destination: Path) -> Path:
    """検査済みの原文を移し、途中で失敗したら保存済みの原文を戻す。"""
    files = [saved, kept] if saved != kept else [kept]
    files = [p for p in files if p.parent != destination]
    backups, moved = {}, []
    # 強制終了や電源断のときは退避した写しが残るだけで、自動では戻らない（その備えが要るなら復旧の記録を残す形にする）
    try:
        # PDF と本文の片方だけ新しくなると引用元が食い違うので、両方を退避してから移す。
        for source in files:
            target = destination / source.name
            if target.exists() or target.is_symlink():
                with tempfile.NamedTemporaryFile(prefix=f".{target.name}-", dir=destination, delete=False) as tmp:
                    backup = Path(tmp.name)
                try:
                    target.replace(backup)
                except BaseException:
                    backup.unlink()
                    raise
                backups[target] = backup
        for source in files:
            target = destination / source.name
            source.replace(target)
            moved.append(target)
    except BaseException:
        for target in moved:
            target.unlink()
        for target, backup in backups.items():
            backup.replace(target)
        raise
    for backup in backups.values():
        backup.unlink()
    return destination / kept.name


def valid_source_name(name: str) -> bool:
    """保存する原文の名前は、パスではなく単一のファイル名に限る。

    Windows で使えない字（<>"|?*）は、そこで保存が OSError で止まるので先に断る。
    字の向きを変える制御文字（U+202E など）は、名前の見た目を偽れるので断る。
    """
    device = name.split(".", 1)[0].rstrip(" ").upper()
    return (bool(name.strip(" .")) and name == name.rstrip(" .")
            and not any(c in name for c in "/\\:<>\"|?*")
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)
            and not re.search(r"[‎‏‪-‮⁦-⁩]", name)
            and device not in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            and not re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", device))


def fetch_source(sources: Path, name: str, url: str, destination: Path) -> int:
    """sources は今回の取得専用の一時置き場。取得・変換・検査中は保存済みの原文に触れない。"""
    if not valid_source_name(name):
        print(f"FAIL\t{name!r}\tsource name must be a single filename")
        return 1
    # file:// や手元の宛先を渡すと、urllib が手元のファイルやサービスの中身を sources/ に写してしまう。
    # ax と yt-dlp に渡す前にもここで止める（両者がたどる転送の先までは見られない）
    problem = url_problem(url)
    if problem:
        print(f"FAIL\t{name}\t{clean(problem)}")
        return 1

    if X_POST.match(url):
        print(f"FAIL\t{name}\tX requires login; keep the URL in map.md")
        return 1
    if is_youtube(url):
        if missing("yt-dlp"):
            print(f"FAIL\t{name}\tyt-dlp is missing, so captions cannot be downloaded")
            return 1
        kept = sources / f"{name}.md"
        body = youtube_to_md(url, kept)
        if len(body) < MIN_TRANSCRIPT_CHARS:
            kept.unlink(missing_ok=True)
            print(f"FAIL\t{name}\tmissing or too-short captions ({len(body)} characters)")
            return 1
        kept = save_verified(kept, kept, destination)
        print(f"OK\t{name}\t{kept}\t{len(body)}\t{first_line(body)}")
        return 0

    pmc = pmc_id(url)
    if pmc:
        # PMC の記事ページは urllib には captcha を返す（2026-10-19）。同じ本文を Europe PMC が
        # 公開の API で全文 XML として配っているので、そちらを正規の別ルートとして取る
        xml_url = EUROPE_PMC_XML.format(pmc)
        try:
            text = jats_to_md(download(xml_url)[0], url, xml_url)
        except Exception as e:  # 全文が無い（OA でない）と 404 などが返る。XML が壊れていても同じく取れない
            print(f"FAIL\t{name}\tEurope PMC has no full-text XML ({type(e).__name__}: {clean(str(e))}); "
                  "PMC uses a captcha, so ask the user to open it in a browser")
            return 1
        kept = saved = sources / f"{name}.md"
        kept.write_text(text, encoding="utf-8")
        return verify_and_save(name, text, saved, kept, destination)

    try:
        body, ctype = download(url)
    except Exception as e:  # HTTP エラーも接続失敗も、呼び手にとっては同じ「取れない」
        print(f"FAIL\t{name}\t{type(e).__name__}: {clean(str(e))}")
        return 1
    # メディアタイプは大文字と小文字を区別しない。「Text/Plain」で返すサーバーもある
    ctype = ctype.lower()
    ocr_note = ""  # OCR の結果を使わなかった理由。FAIL の理由に添える

    if not body.strip():
        # 中身の無い 200/202 を返すサーバーがある（Cornell の論文置き場・2026-10-10）。
        # ブラウザでないと中身を渡さないボット対策のことが多い＝ユーザーのブラウザで開いてもらう候補
        print(f"FAIL\t{name}\tempty response (possibly bot protection; a normal browser may work)")
        return 1
    if body[:5] == b"%PDF-":
        saved = sources / f"{name}.pdf"
        saved.write_bytes(body)
        text = pdf_to_text(saved)
        kept = saved.with_suffix(".txt")
        if len(text.strip()) < 200 and not arxiv_id(url):
            text, ocr_note = ocr_if_longer(saved, text)  # 文字の層が無い PDF。arXiv は下で HTML 版に切り替える
    elif ctype.startswith(("text/plain", "text/markdown", "application/json", "text/csv")):
        # GitHub の README やデータ（raw.githubusercontent.com）はこの形で来る
        # （和田三造の配色のデータで FAIL した・2026-10-10）。中身をそのまま残す。
        # HTML の判定より先に見る。先頭が `<!-- AUTO GENERATED -->` の markdown を
        # HTML と見なして ax に回し、改行が全部消えた（chrome-devtools-mcp の tool-reference・2026-10-11）。
        ext = ".json" if "json" in ctype else ".csv" if "csv" in ctype else ".md"
        kept = saved = sources / f"{name}{ext}"
        text = decode_text(body, ctype)
        kept.write_text(text, encoding="utf-8")
    elif "html" in ctype or body.lstrip()[:1] == b"<":
        kept = sources / f"{name}.md"
        text = html_to_md(url, kept, body, ctype)
        saved = kept
        # HAL は、ブラウザの名乗りには閲覧用の HTML を、それ以外の名乗りには PDF 本体を返す。
        # 前は ax の取り直しで PDF を受け取っていた。いまは名乗りを変えて自分で頼み直す。
        # ax を使っていたころと同じく、HTML のページには2回頼むことになる
        pdf = sources / f"{name}.pdf"
        if pdf_save(url, pdf) and pdf.read_bytes()[:5] == b"%PDF-":
            pdf_text = pdf_to_text(pdf)
            if len(pdf_text.strip()) < 200:
                pdf_text, _ = ocr_if_longer(pdf, pdf_text)  # 使えなければ HTML の本文が残る
            pdf_floor = MIN_HTML_CHARS if mostly_cjk(pdf_text) else MIN_TEXT_CHARS
            # PDF の本文が下限に届いたときだけ PDF に切り替える。届かないなら HTML の本文を残す。
            # 先に HTML を消すと、PDF が文字の層の無い画像だったときに両方を失う（2026-10-20 のレビュー）
            if len(pdf_text) >= pdf_floor or text.lstrip().startswith("%PDF"):
                kept.unlink(missing_ok=True)
                saved, text, kept = pdf, pdf_text, pdf.with_suffix(".txt")
            else:
                pdf.unlink(missing_ok=True)
                pdf.with_suffix(".txt").unlink(missing_ok=True)
        else:
            pdf.unlink(missing_ok=True)
            if text.lstrip().startswith("%PDF"):
                kept.unlink(missing_ok=True)
                print(f"FAIL\t{name}\tcould not save the redirected PDF")
                return 1
    else:
        print(f"FAIL\t{name}\tunsupported format: {ctype or 'unknown'}")
        return 1

    # arXiv の PDF は pdftotext で本文が0文字になることがある（2026-10-09 の試行で
    # COLE・Accordion）。同じ論文の HTML 版なら取れたので、短いときだけ切り替える。
    paper = arxiv_id(url)
    if paper and len(text) < MIN_TEXT_CHARS:
        # HTML 版は別の名前に取ってから比べる。先に元を消すと、HTML 版が取れなかったときに何も残らない。
        # 元が abs ページの .md だと同じ名前になるので、一時の名前を使う
        trial = sources / f".{name}-arxiv.md"
        try:
            html_text = html_to_md(f"https://arxiv.org/html/{paper}", trial)
        except Exception:  # HTML 版が無い論文もある。そのときは元のまま下限で判定する
            html_text = ""
        if len(html_text) > len(text):
            for p in (saved, kept):
                p.unlink(missing_ok=True)
            kept = saved = sources / f"{name}.md"
            trial.replace(kept)
            text = html_text
        else:
            trial.unlink(missing_ok=True)

    return verify_and_save(name, text, saved, kept, destination, ocr_note)


def verify_and_save(name: str, text: str, saved: Path, kept: Path, destination: Path,
                    ocr_note: str = "") -> int:
    """短すぎる本文と壁を FAIL にし、通ったものだけを保存先に移して OK を出す。"""
    # PDF は英語の論文を基準に高い下限。和文の PDF だけは HTML と同じ下限にする
    floor = MIN_TEXT_CHARS if saved.suffix == ".pdf" and not mostly_cjk(text) else MIN_HTML_CHARS
    head = text[:4000].lower()
    wall = next((p for p in WALL_PATTERNS if p in head), None)
    if len(text) < floor or (wall and len(text) < MIN_TEXT_CHARS * 4):
        for p in (saved, kept):
            p.unlink(missing_ok=True)
        reason = f"access wall detected ({wall})" if wall else f"body is too short ({len(text)} characters)"
        # PDF の文字起こしに要るコマンドが欠けていたなら、短い理由はたいていそれ
        lacking = missing("pdftotext", "pdftoppm", "tesseract") if saved.suffix == ".pdf" else []
        if lacking:
            reason += f"; missing {'/'.join(lacking)}"
        elif ocr_note and saved.suffix == ".pdf":
            reason += f"; {ocr_note}"
        print(f"FAIL\t{name}\t{reason}")
        return 1

    kept = save_verified(saved, kept, destination)
    print(f"OK\t{name}\t{kept}\t{len(text)}\t{first_line(text)}")
    return 0


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    sources, name, url = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    if not valid_source_name(name):
        print(f"FAIL\t{name!r}\tsource name must be a single filename")
        return 1
    sources.mkdir(parents=True, exist_ok=True)
    # 変換や FAIL の後始末は一時置き場だけで行う。同名の原文を検査前に上書きしない。
    # 保存先の中に作るので、成功時の replace はファイルシステムをまたがない。
    # 結果の行は画面に出すのと同じものを取得の記録にも残す。詳細レポート（digest.py）が
    # 検索で挙がった URL と突き合わせて〔保存〕〔取得失敗〕を付ける。URL は OK の行に出ないので、ここで書く
    buf = io.StringIO()
    try:
        with tempfile.TemporaryDirectory(prefix=".imozuru-fetch-", dir=sources) as tmp, \
                contextlib.redirect_stdout(buf):
            code = fetch_source(Path(tmp), name, url, sources)
    finally:
        sys.stdout.write(buf.getvalue())
    record_fetch(sources, name, url, buf.getvalue())
    return code


def record_fetch(sources: Path, name: str, url: str, printed: str) -> None:
    """最後の OK/FAIL の行を、調べものの置き場の fetch-log.jsonl に1行足す。書けなくても取得の結果は変えない。

    置き場は sources/ の親。sources/ には原文だけを置く決まりなので、記録は外に出す。
    保存先が sources という名前でないとき（手で別の場所を指したとき）は、親が何か分からないので書かない。
    """
    if sources.name != "sources":
        return
    lines = [l for l in printed.splitlines() if l.startswith(("OK\t", "FAIL\t"))]
    if not lines:
        return
    parts = lines[-1].split("\t")
    entry = {"url": url, "name": name, "status": parts[0]}
    if parts[0] == "OK" and len(parts) > 2:
        entry["file"] = Path(parts[2]).name
    elif len(parts) > 2:
        entry["reason"] = parts[2]
    try:
        with (sources.parent / "fetch-log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    # 本文の先頭の一行を出すので、端末の文字コードに無い字（cp932 の à など）で落ちないようにする
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
