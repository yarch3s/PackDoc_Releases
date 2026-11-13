"""Поиск заранее известных кодов (коды изделий, номера плавок, заказов) в распознанном тексте.

Распознавание путает похожие знаки: 0 и O, 1 и I и l, 5 и S, 8 и B, 2 и Z. Если код известен заранее (из инвойса),
его надёжнее искать с допуском на такие подмены, чем сравнивать распознанное слово с кодом знак в знак.

    hits = find_codes(page_text, ['EXF0041207', 'EXB0100652'], max_errors=1)
    for h in hits:
        print(h.code, h.found, h.kind, h.errors, h.bbox)

Виды находки (kind):
    'exact'     код стоит в тексте знак в знак (после normalize_code);
    'lookalike' отличается только похожими знаками (0/O, 1/I, 5/S ...): почти наверняка тот же код;
    'fuzzy'     есть настоящая ошибка (другой знак, пропуск или лишний знак), не больше max_errors; такая находка
                принимается, только если стоит отдельным словом.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# кириллические буквы, неотличимые от латинских: приводятся к латинице всегда
_HOMO = str.maketrans('АВЕКМНОРСТУХІЈЅ', 'ABEKMHOPCTYXIJS')
_DASHES = '\u2010\u2011\u2012\u2013\u2014\u2212\ufe63\uff0d\u00ad'

# группы похожих знаков: внутри группы подмена считается «лёгкой». Первый знак группы = представитель.
# Состав по замерам подмен на сертификатах (docs/LOGIC_ocr.md) и по общему опыту распознавания.
LOOKALIKE_GROUPS = ('0OQD', '1IL|!', '2Z', '5S', '8B', '6G')
_SKEL = {}
for _g in LOOKALIKE_GROUPS:
    for _c in _g:
        _SKEL[_c] = _g[0]
_SKEL_TABLE = str.maketrans(_SKEL)


def normalize_code(s: str) -> str:
    """Привести кодовое слово к виду для сравнения: совместимая форма Юникода (полноширинные знаки -> обычные),
    заглавные буквы, без пробелов, любые тире -> дефис, кириллические двойники латинских букв -> латиница.
    Похожие знаки (0/O, 1/I) НЕ сливаются: для этого есть skeleton()."""
    s = unicodedata.normalize('NFKC', s or '')
    for d in _DASHES:
        if d in s:
            s = s.replace(d, '-' if d != '\u00ad' else '')
    s = ''.join(s.split()).upper()
    return s.translate(_HOMO)


def skeleton(s: str) -> str:
    """«Скелет» нормализованного кода: похожие знаки слиты в один (O, Q, D -> 0; I, L -> 1; Z -> 2; S -> 5; B -> 8;
    G -> 6). У кодов, отличающихся только похожими знаками, скелеты равны."""
    return s.translate(_SKEL_TABLE)


def same_code(a: str, b: str, max_errors: int = 0) -> bool:
    """Одинаковы ли два кодовых слова с допуском на похожие знаки и не более max_errors настоящих ошибок."""
    a, b = skeleton(normalize_code(a)), skeleton(normalize_code(b))
    if a == b:
        return True
    return max_errors > 0 and _edit_distance(a, b, max_errors) <= max_errors


@dataclass
class CodeHit:
    """Находка кода в тексте.

    code: код-кандидат как его передали; found: что стоит в тексте (как распознано, без пробелов);
    kind: 'exact' | 'lookalike' | 'fuzzy'; exact: True только у kind == 'exact';
    errors: сколько знаков отличается всего; hard: из них настоящих ошибок (не похожие знаки);
    row: номер зрительной строки текста; line: номер куска в PageText.lines (первый кусок находки) или -1;
    bbox: прямоугольник кусков, в которых стоит находка (координаты страницы), либо None для простого текста;
    bounded: находка отделена от соседних букв и цифр (стоит отдельным словом, а не внутри более длинного);
    ambiguous: на это же место с той же точностью подходит другой кандидат;
    conf: уверенность движка в куске текста, где стоит находка (наименьшая из кусков; 1.0 для простого текста).
    """
    code: str
    found: str
    kind: str
    exact: bool
    errors: int
    hard: int
    row: int
    line: int
    bbox: tuple | None
    bounded: bool
    ambiguous: bool = False
    conf: float = 1.0


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Расстояние Левенштейна, не больше limit + 1."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(v)
            if v < best:
                best = v
        if best > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def _best_in_window(pat: str, win: str, limit: int):
    """Лучшее вхождение pat в win как подстроки с ошибками (алгоритм Селлерса).
    Возвращает (ошибок, начало, конец) либо None, если ошибок больше limit."""
    m = len(pat)
    n = len(win)
    # D[i][j]: минимум правок для pat[:i] против подстроки win, кончающейся в j; start[i][j]: её начало
    prev = [0] * (n + 1)
    pstart = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        cstart = [0] + [0] * n
        pc = pat[i - 1]
        for j in range(1, n + 1):
            sub = prev[j - 1] + (pc != win[j - 1])
            dele = prev[j] + 1          # знак образца пропущен в тексте
            ins = cur[j - 1] + 1        # лишний знак в тексте
            if sub <= dele and sub <= ins:
                cur[j] = sub
                cstart[j] = pstart[j - 1]
            elif dele <= ins:
                cur[j] = dele
                cstart[j] = pstart[j]
            else:
                cur[j] = ins
                cstart[j] = cstart[j - 1]
        prev, pstart = cur, cstart
    best = None
    for j in range(1, n + 1):
        if prev[j] <= limit and (best is None or prev[j] < best[0]):
            best = (prev[j], pstart[j], j)
    return best


class _Row:
    """Зрительная строка: текст без пробелов, для каждого знака номер куска (Line)."""
    __slots__ = ('norm', 'skel', 'seg', 'breaks')

    def __init__(self, segments, first_index):
        chars, seg, breaks = [], [], []
        for k, text in enumerate(segments):
            t = unicodedata.normalize('NFKC', text or '')
            for word in t.split():
                breaks.append(len(chars))       # перед этим знаком в исходном тексте был пробел или край куска
                w = normalize_code(word)
                chars.append(w)
                seg.extend([first_index + k] * len(w))
        # пересчёт breaks: позиции в склеенной строке
        pos, out = 0, set()
        for w in chars:
            out.add(pos)
            pos += len(w)
        out.add(pos)
        self.norm = ''.join(chars)
        self.skel = skeleton(self.norm)
        self.seg = seg
        self.breaks = out


def _rows(page_text):
    """Зрительные строки страницы: [(Row, bbox кусков)] из PageText либо из простого текста."""
    if isinstance(page_text, str):
        text, lines = page_text, None
    else:
        text, lines = page_text.text, page_text.lines
    rows = []
    idx = 0
    for raw in text.split('\n'):
        segs = raw.split('\t')
        rows.append(_Row(segs, idx))
        idx += len(segs)
    if lines is not None and idx != len(lines):
        # текст и куски не сходятся (текст правили): считаем каждый кусок отдельной строкой
        rows = [_Row([ln.text], i) for i, ln in enumerate(lines)]
    return rows, lines


def find_codes(page_text, candidates, max_errors: int = 1, max_lookalike: int = 4, min_fuzzy_len: int = 6,
               fuzzy_inside: bool = False) -> list:
    """Найти известные коды в тексте страницы.

    page_text: PageText (тогда у находок есть прямоугольник) либо простой текст (строки через перевод строки);
    candidates: коды, которые ищем (как в инвойсе). Лучше передавать ВСЕ известные коды отгрузки, а не только
    нужные сейчас: тогда чужой похожий код не будет принят за нужный с ошибкой;
    max_errors: сколько настоящих ошибок допускать (0 = только точные и с похожими знаками);
    max_lookalike: сколько подмен похожих знаков допускать; min_fuzzy_len: коды короче этого ищутся только точно;
    fuzzy_inside: принимать ли находку с настоящей ошибкой внутри более длинного слова (по умолчанию нет: на
    сертификатах так «находился» код, которого на странице нет, внутри длинного номера сертификата).
    Возвращает список CodeHit, упорядоченный по месту в тексте. Одно место отдаётся лучшему кандидату; если лучших
    несколько, у всех стоит ambiguous=True.
    """
    rows, lines = _rows(page_text)
    cands = []
    seen = set()
    for c in candidates:
        n = normalize_code(str(c))
        if n and n not in seen:
            seen.add(n)
            cands.append((str(c), n, skeleton(n)))
    raw = []        # (row, start, end, hard, look, code)
    for code, n, sk in cands:
        m = len(sk)
        for ri, row in enumerate(rows):
            if len(row.skel) < m - max_errors:
                continue
            found_exact = False
            pos = row.skel.find(sk)
            while pos >= 0:
                look = sum(1 for a, b in zip(n, row.norm[pos:pos + m]) if a != b)
                if look <= max_lookalike:
                    raw.append((ri, pos, pos + m, 0, look, code))
                    found_exact = True
                pos = row.skel.find(sk, pos + 1)
            if found_exact or max_errors <= 0 or m < min_fuzzy_len:
                continue
            # с ошибками: хотя бы один из (max_errors + 1) кусков образца стоит в тексте точно
            parts = max_errors + 1
            step = m // parts
            tried = set()
            for pi in range(parts):
                a = pi * step
                b = m if pi == parts - 1 else a + step
                piece = sk[a:b]
                pos = row.skel.find(piece)
                while pos >= 0:
                    w0 = max(0, pos - a - max_errors)
                    w1 = min(len(row.skel), pos - a + m + max_errors)
                    if (w0, w1) not in tried:
                        tried.add((w0, w1))
                        got = _best_in_window(sk, row.skel[w0:w1], max_errors)
                        if got and got[0] > 0:
                            e, s, t = got
                            raw.append((ri, w0 + s, w0 + t, e, 0, code))
                    pos = row.skel.find(piece, pos + 1)
    # одно место = лучший кандидат
    raw = sorted(set(raw), key=lambda r: (r[0], r[1], r[3], r[4]))
    hits = []
    used = []
    for r in sorted(raw, key=lambda r: (r[3], r[4], -(r[2] - r[1]))):
        ri, s, t, hard, look, code = r
        clash = [u for u in used if u[0] == ri and u[1] < t and s < u[2]]
        if clash:
            same = [u for u in clash if (u[3], u[4]) == (hard, look) and u[5] != code and (u[1], u[2]) == (s, t)]
            if not same:
                continue
        row = rows[ri]
        bounded = (s in row.breaks or not row.norm[s - 1:s].isalnum()) and \
                  (t in row.breaks or not row.norm[t:t + 1].isalnum())
        if hard and not bounded and not fuzzy_inside:
            continue
        used.append(r)
        found = row.norm[s:t]
        seg = row.seg[s] if s < len(row.seg) else -1
        bbox = None
        conf = 1.0
        if lines is not None and seg >= 0:
            segs = sorted(set(row.seg[s:t]))
            conf = min(float(lines[i].conf) for i in segs)
            xs0 = [lines[i].bbox[0] for i in segs]
            ys0 = [lines[i].bbox[1] for i in segs]
            xs1 = [lines[i].bbox[2] for i in segs]
            ys1 = [lines[i].bbox[3] for i in segs]
            bbox = (min(xs0), min(ys0), max(xs1), max(ys1))
        kind = 'exact' if hard == 0 and look == 0 else ('lookalike' if hard == 0 else 'fuzzy')
        hits.append(CodeHit(code=code, found=found, kind=kind, exact=kind == 'exact', errors=hard + look, hard=hard,
                            row=ri, line=seg if lines is not None else -1, bbox=bbox, bounded=bounded,
                            conf=conf))
    for h in hits:
        for g in hits:
            if g is not h and g.row == h.row and g.code != h.code and g.found == h.found and \
                    (g.hard, g.errors) == (h.hard, h.errors):
                h.ambiguous = True
    hits.sort(key=lambda h: (h.row, h.line, h.code))
    return hits
