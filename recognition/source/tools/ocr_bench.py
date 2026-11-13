"""Замеры инструмента распознавания ocrkit на своих документах (данных внутри скрипта нет: папка задаётся).

python ocr_bench.py engines
    какие движки доступны на этой машине
python ocr_bench.py truth <папка с PDF> [--engine rapid] [--step 4] [--variant скан200] [--procs 3] [--out файл.json]
    точность без ручной разметки: страницы с текстовым слоем рендерятся в картинку «как скан» (разрешение, шум,
    сжатие JPEG, наклон, поворот), распознаются и сравниваются со своим текстовым слоем. Числа: доля найденных
    кодовых слов (латиница с цифрами от 6 знаков и числа от 6 цифр) точно и с одной ошибкой, частые подмены знаков
python ocr_bench.py speed <папка с PDF> [--engine rapid] [--pages 60] [--workers 1,2,3,4,6,8] [--mode threads]
    скорость на сканах (страницы без текстового слоя): страниц в минуту при разном числе рабочих, холодный старт,
    память процесса
Варианты ухудшения: чисто200, скан150, скан200, скан300, скан200+1.5°, скан200 jpeg40, скан200 боком,
скан200 вверх ногами.
Скрипт работает и в репозитории PackDoc (пакет packdoc.ocrkit), и в переносимой папке (пакет ocrkit рядом).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import numpy as np  # noqa: E402
import pymupdf  # noqa: E402

try:
    from packdoc import ocrkit  # noqa: E402
    from packdoc.ocrkit import prep  # noqa: E402
    from packdoc.ocrkit.engines import create as _create, pick as _pick  # noqa: E402
except ImportError:
    import ocrkit  # noqa: E402
    from ocrkit import prep  # noqa: E402
    from ocrkit.engines import create as _create, pick as _pick  # noqa: E402

SPLIT_RX = re.compile(r'[\s()\[\]{}:;,"“”«»]+')
VARIANTS = {
    'чисто200': dict(dpi=200, blur=False, noise=0, jpeg=0),
    'скан200': dict(dpi=200),
    'скан150': dict(dpi=150),
    'скан300': dict(dpi=300),
    'скан200+1.5°': dict(dpi=200, skew=1.5),
    'скан200 jpeg40': dict(dpi=200, jpeg=40, noise=6),
    'скан200 боком': dict(dpi=200, rot90=1),
    'скан200 вверх ногами': dict(dpi=200, rot90=2),
}


def pdfs(folder: str) -> list:
    return sorted(glob.glob(os.path.join(folder, '**', '*.pdf'), recursive=True))


def code_kind(tok: str) -> str:
    """'code' = латиница с цифрами, 'num' = только цифры с разделителями, '' = не кодовое слово."""
    t = tok.strip('.-/*#№+=<>≤≥~\'')
    if not t or not t.isascii():
        return ''
    al = [ch for ch in t if ch.isalnum()]
    if len(al) < 6 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9\-/.*#+]*', t):
        return ''
    digits = sum(ch.isdigit() for ch in al)
    if digits == 0:
        return ''
    return 'code' if len(al) > digits else 'num'


def norm(s: str) -> str:
    s = unicodedata.normalize('NFKC', s)
    for a in '\u2010\u2011\u2012\u2013\u2014\u2212\ufe63\uff0d':
        s = s.replace(a, '-')
    return ''.join(s.split()).upper()


def damaged_words(page, words, dpi=144, keep=0.9) -> set:
    """Слова, испорченные картинкой поверх текста (печать с непрозрачным краем стирает знаки под собой): номера
    слов, у которых после наложения картинок осталось меньше keep тёмных точек текста. Сравниваются два рендера
    страницы: как есть и без картинок. Такие слова прочитать нельзя, в истину они не идут."""
    tmp = pymupdf.open()
    tmp.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
    clean = tmp[0]
    xrefs = {im[0] for im in clean.get_images(full=True)}
    if not xrefs:
        tmp.close()
        return set()
    for x in xrefs:
        try:
            clean.delete_image(x)
        except Exception:
            pass
    z = dpi / 72.0
    m = pymupdf.Matrix(z, z)
    pc = clean.get_pixmap(matrix=m, colorspace=pymupdf.csGRAY, alpha=False)
    g1 = np.frombuffer(pc.samples, dtype=np.uint8).reshape(pc.height, pc.width)
    p0 = page.get_pixmap(matrix=m, colorspace=pymupdf.csRGB, alpha=False)
    # точка текста «жива», если осталась чёрной: под цветным штрихом и под белым краем печати самый яркий канал большой
    g0 = np.frombuffer(p0.samples, dtype=np.uint8).reshape(p0.height, p0.width, 3).max(axis=2)[:pc.height, :pc.width]
    bad = set()
    for i, w in enumerate(words):
        x0, y0, x1, y1 = (int(v * z) for v in w[:4])
        ink = g1[y0:y1 + 1, x0:x1 + 1] < 128
        n = int(ink.sum())
        if n < 5:
            continue
        gone = int((g0[y0:y1 + 1, x0:x1 + 1][ink].astype(np.int16) - g1[y0:y1 + 1, x0:x1 + 1][ink] > 50).sum())
        if n - gone < keep * n:
            bad.add(i)
    tmp.close()
    return bad


def page_truth(page) -> list:
    words = page.get_text('words')
    bad = damaged_words(page, words)
    out = []
    for i, w in enumerate(words):
        if i in bad:
            continue
        for tok in SPLIT_RX.split(unicodedata.normalize('NFKC', w[4])):
            tok = tok.strip('.-/*#№+=<>≤≥~\'')
            k = code_kind(tok)
            if k:
                out.append((norm(tok), k))
    return out


def degrade(page, dpi=200, skew=0.0, blur=True, noise=4.0, jpeg=75, rot90=0, seed=0):
    """Рендер страницы «как скан»."""
    z = dpi / 72.0
    m = pymupdf.Matrix(z, z)
    if skew:
        m = m.prerotate(skew)
    pix = page.get_pixmap(matrix=m, colorspace=pymupdf.csRGB, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(np.float32)
    if blur:
        img[1:-1] = img[:-2] * 0.25 + img[1:-1] * 0.5 + img[2:] * 0.25
        img[:, 1:-1] = img[:, :-2] * 0.25 + img[:, 1:-1] * 0.5 + img[:, 2:] * 0.25
    if noise:
        img += np.random.default_rng(seed).normal(0, noise, img.shape).astype(np.float32)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if jpeg:
        pm = pymupdf.Pixmap(pymupdf.csRGB, img.shape[1], img.shape[0], img.tobytes(), False)
        pm2 = pymupdf.Pixmap(pm.tobytes('jpeg', jpg_quality=jpeg))
        img = np.frombuffer(pm2.samples, dtype=np.uint8).reshape(pm2.height, pm2.width, pm2.n)[:, :, :3].copy()
    if rot90:
        img = np.ascontiguousarray(np.rot90(img, k=-rot90))
    return img


def _one(a: str, b: str):
    if len(a) == len(b):
        d = [(x, y) for x, y in zip(a, b) if x != y]
        return d[0] if len(d) == 1 else None
    if len(a) == len(b) + 1:
        for i in range(len(a)):
            if a[:i] + a[i + 1:] == b:
                return a[i], ''
    if len(a) + 1 == len(b):
        for i in range(len(b)):
            if b[:i] + b[i + 1:] == a:
                return '', b[i]
    return None


def fuzzy(stream: str, pat: str):
    """(0, None) точно; (1, (знак истины, знак распознанный)) с одной ошибкой; (None, None) не найдено."""
    if pat in stream:
        return 0, None
    m = len(pat)
    half = m // 2
    left, right = pat[:half], pat[half:]
    pos = stream.find(left)
    while pos >= 0:
        s = pos + half
        for ln in (len(right), len(right) - 1, len(right) + 1):
            e = _one(right, stream[s:s + ln])
            if e:
                return 1, e
        pos = stream.find(left, pos + 1)
    pos = stream.find(right)
    while pos >= 0:
        for ln in (half, half - 1, half + 1):
            if pos - ln >= 0:
                e = _one(left, stream[pos - ln:pos])
                if e:
                    return 1, e
        pos = stream.find(right, pos + 1)
    return None, None


_E = {}


def _init(engine, options, variant):
    sys.stdout.reconfigure(encoding='utf-8')
    _E['eng'] = _create(_pick(engine), threads=1, **options)
    _E['var'] = VARIANTS[variant]


def _work(job):
    path, pno = job
    with pymupdf.open(path) as doc:
        page = doc[pno]
        codes = page_truth(page)
        img = degrade(page, seed=pno, **_E['var'])
    t0 = time.perf_counter()
    items, rot, skew = _E['eng'].read(img)
    sec = time.perf_counter() - t0
    order = prep.reading_order(items, skew)
    stream = norm('\n'.join('\t'.join(items[i][1] for i in row) for row in order))
    res = {'code': [0, 0, 0], 'num': [0, 0, 0], 'conf': [], 'sec': sec, 'rot': rot}
    for code, kind in codes:
        r = res[kind]
        r[0] += 1
        e, pair = fuzzy(stream, code)
        if e == 0:
            r[1] += 1
            r[2] += 1
        elif e == 1:
            r[2] += 1
            res['conf'].append(pair)
    return res


def cmd_truth(a) -> int:
    jobs = []
    for path in pdfs(a.folder):
        with pymupdf.open(path) as doc:
            jobs += [(path, i) for i, page in enumerate(doc) if prep.has_text_layer(page)]
    jobs = jobs[::a.step]
    if not jobs:
        print('в папке нет страниц с текстовым слоем')
        return 1
    options = json.loads(a.options) if a.options else {}
    t0 = time.perf_counter()
    if a.procs <= 1:
        _init(a.engine, options, a.variant)
        results = [_work(j) for j in jobs]
    else:
        import multiprocessing as mp
        with mp.Pool(a.procs, initializer=_init, initargs=(a.engine, options, a.variant)) as pool:
            results = pool.map(_work, jobs)
    wall = time.perf_counter() - t0
    tot = {'code': [0, 0, 0], 'num': [0, 0, 0]}
    conf = Counter()
    for r in results:
        for k in tot:
            for i in range(3):
                tot[k][i] += r[k][i]
        conf.update(tuple(x) for x in r['conf'])
    n = tot['code'][0] + tot['num'][0]
    ex = tot['code'][1] + tot['num'][1]
    one = tot['code'][2] + tot['num'][2]
    print(f'движок {a.engine}, вариант {a.variant}: страниц {len(jobs)}, кодовых слов {n}')
    print(f'  точно {ex / max(1, n):.1%}, с одной ошибкой {one / max(1, n):.1%}; латиница с цифрами '
          f'{tot["code"][1] / max(1, tot["code"][0]):.1%}, числа {tot["num"][1] / max(1, tot["num"][0]):.1%}')
    print(f'  в среднем {sum(r["sec"] for r in results) / len(results):.2f} с на страницу в одном рабочем; всего {wall:.0f} с')
    print('  частые подмены (истина>распознано):', ' '.join(f'{x or "∅"}>{y or "∅"}:{c}' for (x, y), c in conf.most_common(12)))
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            json.dump({'engine': a.engine, 'variant': a.variant, 'pages': len(jobs), 'words': n, 'exact': ex,
                       'one_error': one, 'by_kind': tot, 'confusions': conf.most_common(40)}, f, ensure_ascii=False,
                      indent=1)
    return 0


def cmd_speed(a) -> int:
    try:
        import psutil
    except ImportError:
        psutil = None
    jobs = []
    for path in pdfs(a.folder):
        with pymupdf.open(path) as doc:
            jobs += [(path, i) for i, page in enumerate(doc) if not prep.has_text_layer(page)]
    if not jobs:
        print('в папке нет страниц-сканов')
        return 1
    step = max(1, len(jobs) // a.pages)
    jobs = jobs[::step][:a.pages]
    by_file = {}
    for path, i in jobs:
        by_file.setdefault(path, []).append(i)
    print(f'страниц-сканов в замере: {len(jobs)} из {len(by_file)} файлов; движок {a.engine}, режим {a.mode}; ядер '
          f'{os.cpu_count()}')
    options = json.loads(a.options) if a.options else {}
    t0 = time.perf_counter()
    ocrkit.recognize_pdf(jobs[0][0], pages=[jobs[0][1]], engine=a.engine, workers=1, **options)
    print(f'холодный старт (загрузка моделей + первая страница): {time.perf_counter() - t0:.1f} с')
    for nw in [int(x) for x in a.workers.split(',')]:
        ocrkit.release_engines()
        t0 = time.perf_counter()
        c0 = time.process_time()
        n = 0
        for path, pages in by_file.items():
            # memory_limit_mb=0: меряем ровно столько потоков, сколько просили (в работе число потоков
            # ограничивает свободная память)
            n += len(ocrkit.recognize_pdf(path, pages=pages, engine=a.engine, workers=nw, mode=a.mode,
                                          memory_limit_mb=0, **options))
        wall = time.perf_counter() - t0
        cpu = time.process_time() - c0
        mem = f', память процесса {psutil.Process().memory_info().rss / 1e6:.0f} МБ' if psutil else ''
        print(f'  рабочих {nw}: {wall:6.1f} с, {wall / n:5.2f} с/стр, {60 * n / wall:5.1f} стр/мин, время процессора '
              f'{cpu / n:5.2f} с/стр{mem}', flush=True)
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description='Замеры ocrkit')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('engines')
    t = sub.add_parser('truth')
    t.add_argument('folder')
    t.add_argument('--engine', default='auto')
    t.add_argument('--step', type=int, default=4)
    t.add_argument('--variant', default='скан200', choices=list(VARIANTS))
    t.add_argument('--procs', type=int, default=3)
    t.add_argument('--options', help='настройки движка в JSON')
    t.add_argument('--out')
    s = sub.add_parser('speed')
    s.add_argument('folder')
    s.add_argument('--engine', default='auto')
    s.add_argument('--pages', type=int, default=60)
    s.add_argument('--workers', default='1,2,3,4,6,8')
    s.add_argument('--mode', default='threads', choices=['threads', 'processes'])
    s.add_argument('--options', help='настройки движка в JSON')
    a = ap.parse_args()
    if a.cmd == 'engines':
        for name, info in ocrkit.engines().items():
            print(f'{name:10} {"доступен" if info["available"] else "нет: " + info["reason"]}')
        return 0
    if a.cmd == 'truth':
        return cmd_truth(a)
    if a.cmd == 'speed':
        return cmd_speed(a)
    ap.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
