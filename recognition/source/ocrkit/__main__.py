"""Запуск из командной строки.

python -m packdoc.ocrkit <pdf или папка> [--out папка] [--engine auto|rapid|windows|tesseract] [--workers N]
       [--searchable] [--json] [--pages 1-5,8] [--cache папка | --no-cache] [--all-pages] [--dpi N]
       [--quality fast|accurate]
python -m packdoc.ocrkit --engines        какие движки доступны на этой машине

По умолчанию рядом с каждым PDF создаётся папка ocr_out, в неё пишется <имя>.txt (текст по страницам);
--json: ещё <имя>.json (строки с координатами); --searchable: ещё <имя>.pdf с невидимым текстовым слоем.
Номера страниц в командной строке считаются с единицы.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time


def parse_pages(spec: str, n: int) -> list:
    """'1-5,8' -> [0, 1, 2, 3, 4, 7] (в командной строке страницы с единицы)."""
    out = []
    for part in spec.split(','):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition('-')
        lo = int(a)
        hi = int(b) if b else lo
        out.extend(range(lo - 1, min(hi, n)))
    return sorted({p for p in out if 0 <= p < n})


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    from . import engines, make_searchable_pdf, recognize_pdf
    from .types import OcrError
    ap = argparse.ArgumentParser(prog='ocrkit', description='Распознавание PDF-сканов: текст с координатами, '
                                 'оцифрованный PDF с поиском. Работает локально, без сети.')
    ap.add_argument('source', nargs='?', help='PDF или папка с PDF')
    ap.add_argument('--out', help='папка результатов (по умолчанию ocr_out рядом с файлом)')
    ap.add_argument('--engine', default='auto', help='движок: auto, rapid, windows, tesseract')
    ap.add_argument('--workers', type=int, default=None, help='сколько страниц распознавать одновременно')
    ap.add_argument('--searchable', action='store_true', help='сделать копию PDF с невидимым текстовым слоем')
    ap.add_argument('--json', action='store_true', help='записать строки с координатами в JSON')
    ap.add_argument('--pages', help='страницы с единицы: 1-5,8')
    ap.add_argument('--cache', help='папка кэша (по умолчанию .ocr_cache в папке результатов)')
    ap.add_argument('--no-cache', action='store_true', help='не пользоваться кэшем')
    ap.add_argument('--all-pages', action='store_true', help='распознавать и страницы с текстовым слоем')
    ap.add_argument('--dpi', type=int, default=None, help='разрешение рендера (по умолчанию родное у скана)')
    ap.add_argument('--quality', choices=('fast', 'accurate'), default=None,
                    help='режим чтения движка rapid: fast (быстро) или accurate (точнее); по умолчанию тот, что '
                         'задан в пакете')
    ap.add_argument('--engines', action='store_true', help='показать доступные движки и выйти')
    a = ap.parse_args(argv)
    if a.engines:
        for name, info in engines().items():
            state = 'доступен' if info['available'] else 'НЕТ: ' + info['reason']
            print(f'{name:10} {state}\n{"":10} {info["about"]}')
        return 0
    if not a.source:
        ap.print_help()
        return 2
    if os.path.isdir(a.source):
        files = sorted(glob.glob(os.path.join(a.source, '**', '*.pdf'), recursive=True))
    else:
        files = [a.source]
    if not files:
        print('PDF не найдены:', a.source, file=sys.stderr)
        return 1
    import pymupdf
    code = 0
    for path in files:
        out_dir = a.out or os.path.join(os.path.dirname(os.path.abspath(path)), 'ocr_out')
        os.makedirs(out_dir, exist_ok=True)
        cache = None if a.no_cache else (a.cache or os.path.join(out_dir, '.ocr_cache'))
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            with pymupdf.open(path) as d:
                n = len(d)
            pages = parse_pages(a.pages, n) if a.pages else None
            t0 = time.perf_counter()

            def progress(done, total, _name=os.path.basename(path)):
                print(f'\r{_name}: {done}/{total} стр.', end='', file=sys.stderr, flush=True)
            res = recognize_pdf(path, pages=pages, engine=a.engine, workers=a.workers, cache_dir=cache,
                                only_scans=not a.all_pages, progress=progress, dpi=a.dpi,
                                **({'quality': a.quality} if a.quality else {}))
            sec = time.perf_counter() - t0
            print(file=sys.stderr)
            with open(os.path.join(out_dir, stem + '.txt'), 'w', encoding='utf-8') as f:
                for no in sorted(res):
                    pt = res[no]
                    f.write(f'===== страница {no + 1} ({pt.source}' + (f', поворот {pt.rotation}' if pt.rotation else '')
                            + ') =====\n' + pt.text + '\n\n')
            if a.json:
                with open(os.path.join(out_dir, stem + '.json'), 'w', encoding='utf-8') as f:
                    json.dump({'file': os.path.basename(path), 'pages': [res[no].to_json() for no in sorted(res)]}, f,
                              ensure_ascii=False)
            extra = ''
            if a.searchable:
                dst = os.path.join(out_dir, stem + '.pdf')
                st = make_searchable_pdf(path, dst, results=res)
                extra = f', оцифрованный PDF: {dst} (страниц со слоем {st["recognized"]})'
            scans = sum(1 for p in res.values() if p.source != 'layer')
            fresh = sum(1 for p in res.values() if p.source != 'layer' and not p.cached)
            print(f'{os.path.basename(path)}: страниц {len(res)}, сканов {scans} (распознано сейчас {fresh}, из кэша '
                  f'{scans - fresh}), {sec:.1f} с{extra}')
        except OcrError as e:
            print(f'{os.path.basename(path)}: ОШИБКА: {e}', file=sys.stderr)
            code = 1
    return code


if __name__ == '__main__':
    sys.exit(main())
