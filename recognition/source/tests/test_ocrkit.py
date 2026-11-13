"""Пакет распознавания ocrkit: точность, порядок страниц, кэш, отмена, оцифрованный PDF, поиск кодов.

Запуск: python tests/test_ocrkit.py [--engine rapid|windows|tesseract] [--fast]
Страницы рисуются программой (коды из латиницы и цифр, таблица, поворот, наклон),
превращаются в PDF-сканы и распознаются.
Пороги взяты из замеров (docs/recognition/README.md) с запасом. Тест годится и для переносимой копии: пакет берётся из
packdoc.ocrkit, а если его нет, из ocrkit рядом.
"""
from __future__ import annotations

import glob
import os
import random
import re
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pymupdf  # noqa: E402

try:
    from packdoc import ocrkit  # noqa: E402
except ImportError:                 # опубликованная копия: пакет ocrkit лежит рядом
    import ocrkit  # noqa: E402

PORTABLE = os.environ.get('OCRKIT_PORTABLE') or ''      # папка второй копии пакета для сверки; не задана: сверки нет


class Checker:
    def __init__(self):
        self.fails = []

    def check(self, cond, text):
        print(('  ok   ' if cond else '  FAIL ') + text)
        if not cond:
            self.fails.append(text)


# ------------------------------------------------------------------------------------- искусственные страницы

def make_codes(rng, n):
    """Кодовые слова вида кодов изделий, плавок и заказов, с «трудными» знаками 0 O 1 I 5 S 8 B 2 Z."""
    out = []
    letters = 'ABCDEFGHJKLMNPQRSTUVWXYZIO'
    for i in range(n):
        kind = i % 4
        if kind == 0:       # код изделия: 3 буквы + 7 цифр
            s = ''.join(rng.choice(letters) for _ in range(3)) + ''.join(rng.choice('0123456789') for _ in range(7))
        elif kind == 1:     # номер плавки: буква + цифры + буква
            s = rng.choice(letters) + ''.join(rng.choice('0123456789') for _ in range(6)) + rng.choice(letters)
        elif kind == 2:     # номер партии с дефисом
            s = 'R' + ''.join(rng.choice('0123456789') for _ in range(6)) + '-' + ''.join(
                rng.choice('0123456789') for _ in range(2))
        else:               # смешанный: трудные знаки подряд
            s = ''.join(rng.choice('0O1I5S8B2Z') for _ in range(4)) + ''.join(rng.choice('0123456789ABCDEF')
                                                                              for _ in range(5))
        out.append(s)
    return out


def draw_page(doc, marker: str, codes: list, fontsize: float = 8.0):
    """Страница A4 с заголовком, абзацем и таблицей позиций: в каждой строке таблицы номер, код и описание."""
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 60), 'MILL TEST CERTIFICATE', fontsize=14, fontname='hebo')
    page.insert_text((60, 80), f'Certificate No. {marker}   According to EN 10204/3.1', fontsize=9)
    page.insert_text((60, 96), 'The material described herein has been tested and inspected in accordance with the',
                     fontsize=8)
    page.insert_text((60, 107), 'standard and the specification of the order and satisfies the requirements.', fontsize=8)
    y = 130
    x0, x1, x2, x3 = 55, 100, 220, 540
    rows = [('Line', 'IDENT CODE', 'Description')] + [(str(i + 1), c, f'ASME B16.5 CL{150 * (1 + i % 4)} BLIND RF '
                                                       f'{2 + i % 9}" A350 LF2') for i, c in enumerate(codes)]
    for r in rows:
        page.draw_line((x0, y), (x3, y), width=0.6)
        page.insert_text((x0 + 6, y + 12), r[0], fontsize=fontsize)
        page.insert_text((x1 + 6, y + 12), r[1], fontsize=fontsize, fontname='helv')
        page.insert_text((x2 + 6, y + 12), r[2], fontsize=fontsize)
        y += 17
    page.draw_line((x0, y), (x3, y), width=0.6)
    for x in (x0, x1, x2, x3):
        page.draw_line((x, 130), (x, y), width=0.6)
    page.insert_text((60, y + 30), 'Heat treatment: normalizing at temperature 900 C, cooled in still air.', fontsize=8)
    return page


def scan_pdf(path: str, pages: list, dpi: int = 200, turn: int = 0, skew: float = 0.0, jpeg: int = 75,
             with_text_first: bool = False):
    """PDF из «сканов»: страницы рисуются, рендерятся в картинку (с поворотом turn и наклоном skew) и кладутся
    картинками JPEG. pages: [(метка, коды)]. with_text_first: первая страница остаётся с текстовым слоем."""
    src = pymupdf.open()
    for marker, codes in pages:
        draw_page(src, marker, codes)
    out = pymupdf.open()
    z = dpi / 72.0
    for i, page in enumerate(src):
        if with_text_first and i == 0:
            out.insert_pdf(src, from_page=0, to_page=0)
            continue
        m = pymupdf.Matrix(z, z)
        if turn or skew:
            m = m.prerotate(turn + skew)
        pix = page.get_pixmap(matrix=m, colorspace=pymupdf.csRGB, alpha=False)
        data = pix.tobytes('jpeg', jpg_quality=jpeg)
        w, h = pix.width / z, pix.height / z
        p = out.new_page(width=w, height=h)
        p.insert_image(p.rect, stream=data)
    out.save(path)
    out.close()
    src.close()


def found_exact(pt, codes):
    stream = ocrkit.normalize_code(pt.text)
    return sum(1 for c in codes if ocrkit.normalize_code(c) in stream)


# ------------------------------------------------------------------------------------------------ часть 1

def unit_codes(c: Checker):
    print('#### поиск известных кодов (без распознавания)')
    text = ('Line\tIDENT CODE\tDescription\n2\tZBK0OO0752\tASME B16.5 CL150\t1\nHeat 31807249\tZEK73OO281 x\n'
            'ZEK7300286\nXX ZEK730028 YY\nZZ ZEK7300381 YY\nQQZBK0000752WW\nР731845\u201403')
    cands = ['ZBK0000752', 'ZEK7300281', 'ZEK7300286', '31807249', 'NOPE123456', 'P731845-03']
    hits = ocrkit.find_codes(text, cands)
    by = {}
    for h in hits:
        by.setdefault(h.code, []).append(h)
    c.check('NOPE123456' not in by, 'кода, которого нет в тексте, нет и в находках')
    c.check(any(h.kind == 'exact' and h.bounded for h in by.get('31807249', [])), 'точная находка отдельным словом')
    c.check(any(h.kind == 'lookalike' and h.errors == 2 and h.hard == 0 and h.row == 1 for h in by.get('ZBK0000752', [])),
            'O вместо 0: находка «с похожими знаками», настоящих ошибок 0')
    c.check(any(h.kind == 'exact' and not h.bounded and h.row == 6 for h in by.get('ZBK0000752', [])),
            'код внутри более длинного слова: найден, но помечен как неотделённый')
    c.check(any(h.kind == 'exact' and h.row == 3 for h in by.get('ZEK7300286', [])) and
            not any(h.row == 3 for h in by.get('ZEK7300281', [])),
            'соседний код, отличающийся одним знаком, не принят за нужный: место отдано точному кандидату')
    amb = [h for h in hits if h.row == 4]
    c.check(len(amb) == 2 and all(h.ambiguous and h.kind == 'fuzzy' for h in amb),
            'обрезанный код подходит двум кандидатам: оба помечены как неоднозначные')
    c.check(any(h.kind == 'fuzzy' and h.hard == 1 and h.row == 5 for h in by.get('ZEK7300281', [])),
            'одна настоящая ошибка: находка вида fuzzy')
    c.check(any(h.kind == 'exact' for h in by.get('P731845-03', [])),
            'кириллическая Р и длинное тире приводятся к латинице и дефису')
    inside = 'сертификат QQZEK7300381WW от 2026'
    c.check(not ocrkit.find_codes(inside, ['ZEK7300281']) and
            [h.kind for h in ocrkit.find_codes(inside, ['ZEK7300281'], fuzzy_inside=True)] == ['fuzzy'],
            'код с настоящей ошибкой внутри более длинного слова не принимается (только по просьбе fuzzy_inside)')
    strict = ocrkit.find_codes(text, cands, max_errors=0)
    c.check(not any(h.kind == 'fuzzy' for h in strict) and any(h.kind == 'lookalike' for h in strict),
            'max_errors=0: остаются точные находки и находки с похожими знаками')
    c.check(ocrkit.normalize_code(' zb k0000752 ') == 'ZBK0000752' and ocrkit.same_code('ZBK0000752', 'ZBKOOOO752')
            and not ocrkit.same_code('ZEK7300281', 'ZEK7300286') and ocrkit.same_code('ZEK7300281', 'ZEK7300286', 1),
            'normalize_code и same_code')


def unit_engines(c: Checker, engine: str):
    print('#### движки на этой машине')
    info = ocrkit.engines()
    for name, d in info.items():
        print(f'       {name:10} {"доступен" if d["available"] else "нет: " + d["reason"]}')
    c.check(all(d['available'] or d['reason'] for d in info.values()), 'у недоступного движка названа причина')
    c.check(info.get(engine, {}).get('available', False), f'движок {engine} доступен')
    try:
        ocrkit.recognize_image(__file__, engine='нет такого')
        ok = False
    except ocrkit.EngineUnavailable as e:
        ok = 'нет такого' in str(e)
    except Exception:
        ok = False
    c.check(ok, 'неизвестное имя движка: понятная ошибка EngineUnavailable')


def unit_synthetic(c: Checker, engine: str, tmp: str, fast: bool):
    rng = random.Random(20261112)
    print('#### точность на искусственных сканах')
    pages = [(f'SYN-{i + 1:03d}', make_codes(rng, 24)) for i in range(3)]
    base = os.path.join(tmp, 'syn.pdf')
    scan_pdf(base, pages)
    cache = os.path.join(tmp, 'cache')
    t0 = time.perf_counter()
    res = ocrkit.recognize_pdf(base, engine=engine, cache_dir=cache, workers=3)
    cold = time.perf_counter() - t0
    total = sum(len(codes) for _m, codes in pages)
    exact = sum(found_exact(res[i], codes) for i, (_m, codes) in enumerate(pages))
    fuzzy = 0
    for i, (_m, codes) in enumerate(pages):
        got = {h.code for h in ocrkit.find_codes(res[i], codes, max_errors=1) if not h.ambiguous}
        fuzzy += len(got)
    print(f'       кодовых слов {total}: точно {exact} ({exact / total:.1%}), через find_codes {fuzzy} '
          f'({fuzzy / total:.1%}); {cold:.1f} с на 3 страницы (с загрузкой моделей)')
    # коды здесь нарочно трудные (вперемешку 0 O 1 I 5 S 8 B 2 Z шрифтом без засечек, 8 пт): по замерам точно
    # читается около 92 %, остальное добирает поиск по известным кодам
    c.check(exact / total >= 0.85, f'точное совпадение трудных кодов не ниже 85 % ({exact}/{total})')
    c.check(fuzzy / total >= 0.98, f'find_codes с допуском на одну ошибку находит не меньше 98 % ({fuzzy}/{total})')
    c.check(all(res[i].source == engine and res[i].rotation == 0 for i in range(3)), 'источник = имя движка, поворот 0')
    ln = next((x for x in res[0].lines if 'CERTIFICATE' in x.text.upper().replace(' ', '')), None)
    c.check(ln is not None and 40 < ln.bbox[0] < 80 and 40 < ln.bbox[1] < 65 and ln.bbox[3] < 70,
            f'координаты строки заголовка на своём месте в точках страницы ({ln.bbox if ln else None})')
    rows = res[0].text.split('\n')
    code0 = ocrkit.normalize_code(pages[0][1][0])
    row = next((r for r in rows if code0 in ocrkit.normalize_code(r)), '')
    c.check('\t' in row and 'ASME' in row.upper(), 'строка таблицы собрана в одну зрительную строку: код и описание '
            'через табуляцию')

    print('#### кэш')
    t0 = time.perf_counter()
    res2 = ocrkit.recognize_pdf(base, engine=engine, cache_dir=cache)
    warm = time.perf_counter() - t0
    print(f'       повторный запуск {warm:.3f} с против {cold:.1f} с')
    c.check(all(res2[i].cached and res2[i].text == res[i].text for i in range(3)) and warm < max(0.5, cold / 10),
            'повторный запуск берёт страницы из кэша: текст тот же, время в разы меньше')
    copy = os.path.join(tmp, 'другое имя.pdf')
    shutil.copy(base, copy)
    c.check(all(p.cached for p in ocrkit.recognize_pdf(copy, engine=engine, cache_dir=cache).values()),
            'тот же файл под другим именем попадает в тот же кэш (ключ по содержимому)')
    c.check(not any(p.cached for p in ocrkit.recognize_pdf(base, engine=engine, cache_dir=cache, pages=[0],
                                                           dpi=150).values()),
            'другие параметры (dpi) = другой ключ кэша')
    sig = ocrkit.engine_signature(engine)
    cache_mod = sys.modules[ocrkit.__name__ + '.cache']
    core_mod = sys.modules[ocrkit.__name__ + '.core']
    d_now = cache_mod.Cache(cache, base, engine, {'algo': core_mod.ALGO, 'sig': sig, 'dpi': 0, 'opt': {}}).dir
    d_model = cache_mod.Cache(cache, base, engine, {'algo': core_mod.ALGO, 'sig': sig + '+другая модель', 'dpi': 0,
                                                    'opt': {}}).dir
    d_algo = cache_mod.Cache(cache, base, engine, {'algo': core_mod.ALGO + 1, 'sig': sig, 'dpi': 0, 'opt': {}}).dir
    c.check(os.path.isdir(d_now) and len({d_now, d_model, d_algo}) == 3 and (engine != 'rapid' or '.onnx:' in sig),
            f'ключ кэша зависит от подписи движка и версии обработки (подпись: {sig[:60]}...)')
    st = ocrkit.clean_cache(cache, max_age_days=-1)
    c.check(st['left'] == 0 and st['removed'] >= 4 and not os.listdir(cache), f'чистка кэша убирает старое ({st})')

    print('#### поворот и наклон')
    turned = [(90, 90), (180, 180), (270, 270)] if not fast else [(90, 90)]
    for turn, _ in turned:
        p = os.path.join(tmp, f'turn{turn}.pdf')
        scan_pdf(p, pages[:1], turn=turn)
        r = ocrkit.recognize_pdf(p, engine=engine)[0]
        ex = found_exact(r, pages[0][1])
        # картинку повернули на turn по часовой: чтобы поставить прямо, её надо довернуть до полного оборота
        c.check(r.rotation == (360 - turn) % 360 and ex >= 22,
                f'страница, повёрнутая на {turn}: найден поворот {r.rotation}, кодов {ex}/24')
        ln = next((x for x in r.lines if 'CERTIFICATE' in x.text.upper().replace(' ', '')), None)
        ok = ln is not None and ((turn in (90, 270)) == ((ln.bbox[3] - ln.bbox[1]) > (ln.bbox[2] - ln.bbox[0])))
        c.check(ok, 'прямоугольник строки дан в координатах исходной (повёрнутой) страницы')
    p = os.path.join(tmp, 'skew.pdf')
    scan_pdf(p, pages[:1], skew=1.5)
    r = ocrkit.recognize_pdf(p, engine=engine)[0]
    ex = found_exact(r, pages[0][1])
    c.check(ex >= 22 and r.rotation == 0, f'наклон 1.5 градуса: кодов {ex}/24 (определён наклон {r.skew:.2f})')

    print('#### порядок страниц, ход и отмена при параллельной работе')
    many = [(f'PAGE-{i + 1:03d}', make_codes(rng, 6)) for i in range(8)]
    p = os.path.join(tmp, 'many.pdf')
    scan_pdf(p, many, with_text_first=True)
    seen = []
    r = ocrkit.recognize_pdf(p, engine=engine, workers=4, progress=lambda d, t: seen.append((d, t)))
    order_ok = list(r) == list(range(8)) and all(
        ocrkit.normalize_code(many[i][0]) in ocrkit.normalize_code(r[i].text) for i in range(8))
    c.check(order_ok, 'страницы выданы по порядку, на каждой своя метка (4 потока)')
    c.check(seen == [(i + 1, 8) for i in range(8)], f'ход выполнения: {len(seen)} вызовов от 1 до 8')
    c.check(r[0].source == 'layer' and all(r[i].source == engine for i in range(1, 8)),
            'смешанный PDF: страница с текстовым слоем не распознаётся, сканы распознаются')
    r_all = ocrkit.recognize_pdf(p, engine=engine, pages=[0], only_scans=False)
    c.check(r_all[0].source == engine, 'only_scans=False: страница с текстом распознаётся')
    r_sub = ocrkit.recognize_pdf(p, engine=engine, pages=[5, 2], workers=2)
    c.check(list(r_sub) == [5, 2], 'pages=[5, 2]: вернулись только названные страницы')
    cache2 = os.path.join(tmp, 'cache2')
    state = {'n': 0}

    def prog(d, t):
        state['n'] = d
    try:
        ocrkit.recognize_pdf(p, engine=engine, workers=2, cache_dir=cache2, progress=prog,
                             cancel=lambda: state['n'] >= 3)
        cancelled, part = False, {}
    except ocrkit.Cancelled as e:
        cancelled, part = True, e.pages
    c.check(cancelled and 3 <= len(part) < 8, f'отмена после 3 страниц: поднято Cancelled, готово {len(part)} из 8')
    r3 = ocrkit.recognize_pdf(p, engine=engine, workers=2, cache_dir=cache2)
    c.check(sum(1 for x in r3.values() if x.cached) >= len(part) - 1 and len(r3) == 8,
            'после отмены готовые страницы остались в кэше, повторный запуск доделывает остальные')
    try:
        ocrkit.recognize_pdf(p, pages=[99], engine=engine)
        bad = False
    except ocrkit.OcrError:
        bad = True
    c.check(bad, 'страницы с таким номером нет: понятная ошибка OcrError')

    print('#### память: число работников не больше, чем помещается')
    free = ocrkit.available_memory_mb()
    w_small, w_mid, w_big = (ocrkit.default_workers(engine, available_mb=a) for a in (900, 3000, 64000))
    print(f'       свободно сейчас {free:.0f} МБ; работников при 900 МБ свободных: {w_small}, при 3 ГБ: {w_mid}, '
          f'при 64 ГБ: {w_big}; по умолчанию сейчас: {ocrkit.default_workers(engine)}')
    c.check(free and free > 0 and w_small == 1 and 1 <= w_mid <= w_big <= 6,
            'мало свободной памяти: один работник; много: по числу ядер, но не больше 6')
    c.check(ocrkit.workers_by_memory(engine, available_mb=64000, memory_limit_mb=300) == 1,
            'свой предел памяти 300 МБ: один работник даже на большой машине')
    info = []
    r_mem = ocrkit.recognize_pdf(p, engine=engine, workers=4, memory_limit_mb=300, plan=info.append)
    c.check(info and info[0]['workers'] == 1 and list(r_mem) == list(range(8)),
            f'recognize_pdf(workers=4, memory_limit_mb=300): работает в 1 поток и доходит до конца ({info})')
    info = []
    ocrkit.recognize_pdf(p, engine=engine, workers=3, memory_limit_mb=0, pages=[1, 2, 3], plan=info.append)
    c.check(info and info[0]['workers'] == 3, 'memory_limit_mb=0 снимает ограничение: столько потоков, сколько просили')

    print('#### часть страницы и страница со свойством /Rotate')
    info = []
    band = ocrkit.recognize_pdf(base, pages=[0], engine=engine, clip=(0, 0, 1, 0.2), plan=info.append)[0]
    top = [ln for ln in band.lines if 'CERTIFICATE' in ln.text.upper().replace(' ', '')]
    c.check(bool(top) and all(ln.bbox[3] <= 0.2 * 842 + 6 for ln in band.lines) and
            ocrkit.normalize_code(pages[0][1][-1]) not in ocrkit.normalize_code(band.text),
            f'clip=(0, 0, 1, 0.2): распознана только верхняя полоса ({len(band.lines)} кусков), координаты страницы')
    c.check(bool(info) and {k: info[0][k] for k in ('pages', 'layer', 'cached', 'recognize')} ==
            {'pages': 1, 'layer': 0, 'cached': 0, 'recognize': 1} and info[0]['workers'] == 1,
            f'plan сообщает состав работы: {info}')
    try:
        ocrkit.recognize_pdf(base, pages=[0], engine=engine, clip=(0.5, 0.5, 0.2, 0.9))
        bad = False
    except ocrkit.OcrError:
        bad = True
    c.check(bad, 'пустая вырезка: понятная ошибка OcrError')
    rotated = os.path.join(tmp, 'rotate_prop.pdf')
    with pymupdf.open(base) as d:
        d[0].set_rotation(90)       # страница со свойством /Rotate 90: на экране лежит боком
        d.save(rotated)
    r = ocrkit.recognize_pdf(rotated, pages=[0], engine=engine)[0]
    dst_r = os.path.join(tmp, 'rotate_prop_ocr.pdf')
    ocrkit.make_searchable_pdf(rotated, dst_r, results={0: r})
    code = pages[0][1][0]
    hit = [h for h in ocrkit.find_codes(r, [code], max_errors=0)]
    with pymupdf.open(dst_r) as d:
        found = d[0].search_for(code)
    same = bool(hit) and bool(found) and abs(hit[0].bbox[0] - found[0].x0) < 8 and abs(hit[0].bbox[1] - found[0].y0) < 8
    c.check(r.rotation == 270 and r.page_rotation == 90 and same,
            f'/Rotate 90: поворот для распознавания {r.rotation}, рамка кода совпадает с поиском PyMuPDF '
            f'({[round(v) for v in hit[0].bbox] if hit else None} и {[round(v) for v in found[0]] if found else None})')

    print('#### оцифрованный PDF (невидимый текстовый слой)')
    dst = os.path.join(tmp, 'searchable.pdf')
    st = ocrkit.make_searchable_pdf(base, dst, engine=engine)
    d0, d1 = pymupdf.open(base), pymupdf.open(dst)
    words = sum(len(pg.get_text('words')) for pg in d0)
    c.check(words == 0 and st['recognized'] == 3, f'в исходном скане текста нет; слой положен на {st["recognized"]} стр.')
    found = placed = 0
    for i, (_m, codes) in enumerate(pages):
        page = d1[i]
        stream = ocrkit.normalize_code(page.get_text())
        for code in codes:
            if ocrkit.normalize_code(code) in stream:
                found += 1
            hits = page.search_for(code)
            if hits:
                # код стоит во второй колонке таблицы (x 100..220), строки идут с шагом 17 пт от y = 147
                row = codes.index(code)
                y = 130 + 17 * (row + 1) + 8
                if any(95 < h.x0 < 125 and abs((h.y0 + h.y1) / 2 - y) < 6 for h in hits):
                    placed += 1
    print(f'       из оцифрованного PDF извлекается кодов {found}/{total}, поиск ставит на своё место {placed}/{total}')
    c.check(found >= exact - 1, 'текст извлекается из оцифрованного PDF (не меньше, чем распознано)')
    c.check(placed >= 0.9 * found, 'найденные поиском коды стоят в своей ячейке таблицы')
    pix0 = d0[0].get_pixmap(dpi=50)
    pix1 = d1[0].get_pixmap(dpi=50)
    c.check(pix0.samples == pix1.samples, 'вид страницы не изменился (текст невидимый)')
    size0, size1 = os.path.getsize(base), os.path.getsize(dst)
    c.check(size1 < size0 * 1.15 + 20000, f'картинки не пережаты: размер {size0 // 1024} -> {size1 // 1024} КБ')
    d0.close()
    d1.close()
    turned_pdf = os.path.join(tmp, 'turn90.pdf')
    dst2 = os.path.join(tmp, 'searchable90.pdf')
    ocrkit.make_searchable_pdf(turned_pdf, dst2, engine=engine)
    with pymupdf.open(dst2) as d2:
        stream = ocrkit.normalize_code(d2[0].get_text())
        got = sum(1 for code in pages[0][1] if ocrkit.normalize_code(code) in stream)
        hit = d2[0].search_for(pages[0][1][0])
        tall = bool(hit) and (hit[0].y1 - hit[0].y0) > (hit[0].x1 - hit[0].x0)
    c.check(got >= 22 and tall, f'страница, лежащая боком: слой положен вдоль текста, кодов {got}/24')

    print('#### картинка и командная строка')
    doc = pymupdf.open(base)
    pix = doc[0].get_pixmap(dpi=200)
    png = os.path.join(tmp, 'page.png')
    pix.save(png)
    doc.close()
    ri = ocrkit.recognize_image(png, engine=engine)
    c.check(found_exact(ri, pages[0][1]) >= 22 and ri.width == pix.width,
            'recognize_image по файлу картинки: коды найдены, координаты в пикселях')
    import subprocess
    pkg = ocrkit.__name__
    out_dir = os.path.join(tmp, 'cli')
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(ocrkit.__file__)))
               if pkg == 'ocrkit' else ROOT, PYTHONIOENCODING='utf-8')
    run = subprocess.run([sys.executable, '-m', pkg, base, '--out', out_dir, '--engine', engine, '--json',
                          '--searchable', '--pages', '1-2', '--workers', '2'], capture_output=True, text=True,
                         encoding='utf-8', errors='replace', env=env)
    names = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
    c.check(run.returncode == 0 and {'syn.txt', 'syn.json', 'syn.pdf'} <= set(names),
            f'python -m {pkg}: код возврата {run.returncode}, файлы {[n for n in names if not n.startswith(".")]}')
    if run.returncode != 0:
        print(run.stderr[-800:])
    else:
        txt = open(os.path.join(out_dir, 'syn.txt'), encoding='utf-8').read()
        c.check('страница 1' in txt and 'страница 2' in txt and 'страница 3' not in txt, '--pages 1-2: две страницы')
    run = subprocess.run([sys.executable, '-m', pkg, '--engines'], capture_output=True, text=True, encoding='utf-8',
                         errors='replace', env=env)
    c.check(run.returncode == 0 and engine in run.stdout, '--engines печатает список движков')


def unit_quality(c: Checker, tmp: str):
    """Режимы чтения движка rapid: «быстро» (одна модель) и «точнее» (вторая модель перечитывает строки с цифрами),
    слияние двух чтений по знакам, правки по смыслу поля, ключ кэша."""
    print('#### режимы чтения: быстро и точнее')
    rapid = sys.modules[ocrkit.__name__ + '.engines.rapid']
    fixes = sys.modules.get(ocrkit.__name__ + '.fixes') or __import__(ocrkit.__name__ + '.fixes', fromlist=['x'])
    c.check(fixes.fix_line('ASME VII UW-51') == 'ASME VIII UW-51' and fixes.fix_line('ASMEVIUW-51') == 'ASMEVIIIUW-51'
            and fixes.fix_line('ASME V UW-51 A') == 'ASME VIII UW-51 A',
            'правка: римская цифра перед пунктом UW читается как VIII')
    c.check(fixes.fix_line('ASTM A350 LP2 CL1') == 'ASTM A350 LF2 CL1' and fixes.fix_line('A350LP2') == 'A350LF2',
            'правка: марка по ASTM A350 пишется LF, а не LP')
    same = ['EXF0041207 FLANGE WN RF', 'NOV UW 5', 'ASME VIII UW-51', 'A350 LF2', 'SA350LP', 'LP2 A350']
    c.check(all(fixes.fix_line(t) == t for t in same), 'правки не трогают обычный текст')
    m = rapid.merge_readings
    one = [1.0] * 40
    c.check(m('KO7-12-340', one, 'K07-12-340', one) == 'K07-12-340', 'слияние: знак второй модели при равной '
            'уверенности')
    c.check(m('ASME B16.9 CL150', one, 'ASMEB16.9CL150', one) == 'ASME B16.9 CL150', 'слияние: пробелы основной '
            'модели остаются')
    c.check(m('163/171/158', one, '163/171//158', one) == '163/171//158' and m('20274.15', one, '2027.4.15', one)
            == '2027.4.15', 'слияние: знак, который прочла только вторая модель, добавляется')
    low = [0.3] + [1.0] * 20
    c.check(m('QX731846DM52', low, 'X731846DM52', one) == 'QX731846DM52', 'слияние: крайний знак слова не теряется')
    mid = [1.0, 1.0, 0.4] + [1.0] * 20
    c.check(m('AB1CD0001', mid, 'ABCD0001', one) == 'ABCD0001' and m('AB1CD0001', one, 'ABCD0001', one)
            == 'AB1CD0001', 'слияние: лишний знак убирается, только если основная модель в нём не уверена')
    sure = [1.0, 1.0, 1.0] + [1.0] * 20
    weak = [1.0, 0.5, 1.0] + [1.0] * 20
    c.check(m('A0B', sure, 'AOB', weak) == 'A0B' and m('A0B', weak, 'AOB', weak) == 'AOB',
            'слияние: уверенный знак основной модели остаётся')
    sig_fast = ocrkit.engine_signature('rapid', quality='fast')
    sig_acc = ocrkit.engine_signature('rapid', quality='accurate')
    c.check(sig_fast != sig_acc and sig_fast.endswith('|fast') and sig_acc.endswith('|accurate')
            and rapid.REC2_MODEL in sig_acc and rapid.REC2_MODEL not in sig_fast,
            'подпись движка различает режимы (в режиме «точнее» в неё входит вторая модель)')
    try:
        ocrkit.engine_signature('rapid', quality='как-нибудь')
        ok = False
    except ocrkit.OcrError as e:
        ok = 'как-нибудь' in str(e)
    c.check(ok, 'неизвестный режим: понятная ошибка OcrError')
    # без файла второй модели режим «точнее» тихо становится режимом «быстро» (и подпись это показывает)
    bare = os.path.join(tmp, 'models_fast')
    os.makedirs(bare, exist_ok=True)
    for n in (rapid.DET_MODEL, rapid.REC_MODEL, rapid.CLS_MODEL):
        shutil.copy(os.path.join(rapid.models_dir(), n), bare)
    old = os.environ.get('OCRKIT_MODELS')
    os.environ['OCRKIT_MODELS'] = bare
    try:
        eng = rapid.Engine(quality='accurate')
        c.check(eng.quality == 'fast' and eng.rec2 is None and ocrkit.engine_signature('rapid').endswith('|fast'),
                'без файла второй модели режим «точнее» работает как «быстро»')
        del eng
    finally:
        if old is None:
            os.environ.pop('OCRKIT_MODELS', None)
        else:
            os.environ['OCRKIT_MODELS'] = old
    rng = random.Random(20261113)
    pages = [(f'QLT-{i + 1:03d}', make_codes(rng, 24)) for i in range(2)]
    pdf = os.path.join(tmp, 'quality.pdf')
    scan_pdf(pdf, pages)
    cache = os.path.join(tmp, 'cache_quality')
    total = sum(len(codes) for _m, codes in pages)
    got = {}
    for q in ('fast', 'accurate'):
        t0 = time.perf_counter()
        res = ocrkit.recognize_pdf(pdf, engine='rapid', cache_dir=cache, workers=1, quality=q)
        got[q] = (sum(found_exact(res[i], codes) for i, (_m, codes) in enumerate(pages)), time.perf_counter() - t0,
                  any(p.cached for p in res.values()))
    print(f'       трудных кодов {total}: быстро {got["fast"][0]} за {got["fast"][1]:.1f} с, точнее '
          f'{got["accurate"][0]} за {got["accurate"][1]:.1f} с')
    c.check(not got['fast'][2] and not got['accurate'][2], 'у каждого режима свой ключ кэша')
    c.check(got['accurate'][0] >= got['fast'][0] - 1 and got['accurate'][0] / total >= 0.85,
            f'режим «точнее» читает трудные коды не хуже режима «быстро» ({got["accurate"][0]} и {got["fast"][0]} '
            f'из {total})')
    res = ocrkit.recognize_pdf(pdf, engine='rapid', cache_dir=cache, workers=1)
    c.check(all(p.cached for p in res.values()) == (rapid.QUALITY in got),
            f'режим по умолчанию ({rapid.QUALITY}) берёт страницы из кэша своего режима')


def unit_no_network(c: Checker):
    print('#### самодостаточность')
    pkg_dir = os.path.dirname(os.path.abspath(ocrkit.__file__))
    bad = []
    net = re.compile(r'^\s*(import|from)\s+(urllib|requests|http|socket|ftplib|httpx|aiohttp)\b', re.M)
    alien = re.compile(r'^\s*(from\s+packdoc(?!\.ocrkit)|import\s+packdoc(?!\.ocrkit)|from\s+\.\.\.)', re.M)
    for fn in glob.glob(os.path.join(pkg_dir, '**', '*.py'), recursive=True):
        src = open(fn, encoding='utf-8').read()
        if net.search(src) or alien.search(src):
            bad.append(os.path.relpath(fn, pkg_dir))
    c.check(not bad, f'в пакете нет сетевых модулей и обращений к остальному продукту {bad or ""}')
    if os.path.isdir(PORTABLE) and os.path.abspath(PORTABLE) != pkg_dir:
        diff = []
        for base, other in ((pkg_dir, PORTABLE), (PORTABLE, pkg_dir)):
            for fn in glob.glob(os.path.join(base, '**', '*'), recursive=True):
                rel = os.path.relpath(fn, base)
                if os.path.isdir(fn) or '__pycache__' in rel or rel.endswith('.pyc'):
                    continue
                o = os.path.join(other, rel)
                if not os.path.isfile(o) or open(fn, 'rb').read() != open(o, 'rb').read():
                    diff.append(rel)
        c.check(not diff, f'переносимая копия {PORTABLE} совпадает с пакетом '
                          f'{("(расходятся: " + ", ".join(sorted(set(diff))[:6]) + ")") if diff else ""}')
    else:
        print('       переносимой копии рядом нет: сверка пропущена')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    engine = 'rapid'
    if '--engine' in sys.argv:
        engine = sys.argv[sys.argv.index('--engine') + 1]
    fast = '--fast' in sys.argv
    c = Checker()
    tmp = tempfile.mkdtemp(prefix='ocrkit_test_')
    try:
        unit_codes(c)
        unit_no_network(c)
        unit_engines(c, engine)
        if ocrkit.engines().get(engine, {}).get('available'):
            unit_synthetic(c, engine, tmp, fast)
            if engine == 'rapid':
                unit_quality(c, tmp)
    finally:
        ocrkit.release_engines()
        shutil.rmtree(tmp, ignore_errors=True)
    print('РАСПОЗНАВАНИЕ (ocrkit):', 'OK' if not c.fails else f'ОШИБОК {len(c.fails)}')
    return 1 if c.fails else 0


if __name__ == '__main__':
    sys.exit(main())
