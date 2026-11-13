"""Оцифровка: копия PDF с невидимым текстовым слоем поверх сканов (в файле начинают работать поиск и выделение)."""
from __future__ import annotations

import math
import os

from .types import OcrError

_INVISIBLE = 3      # режим вывода текста PDF «не рисовать»: текст есть, на вид страницы не влияет


def _font_for(text: str) -> str:
    """Шрифт для строки: встроенная Helvetica для латиницы (ширины букв известны, слова встают на свои места);
    для кириллицы и иероглифов стандартный китайский шрифт PDF без встраивания (в нём есть и кириллица)."""
    return 'helv' if all(ord(c) < 256 for c in text) else 'china-s'


def _text_width(text: str, font: str, size: float) -> float:
    import pymupdf
    if font == 'helv':
        return pymupdf.get_text_length(text, fontname='helv', fontsize=size)
    return len(text) * size         # у шрифта без встраивания все знаки шириной в кегль


def add_text_layer(page, page_text) -> int:
    """Положить на страницу невидимый текст строк page_text (PageText). Возвращает число положенных строк."""
    import pymupdf
    rot = page.rotation
    if rot:
        page.set_rotation(0)            # строки заданы в координатах страницы без /Rotate: кладём в них же
    n = 0
    try:
        shape = page.new_shape()
        for ln in page_text.lines:
            text = ' '.join(ln.text.split())
            if not text:
                continue
            quad = ln.quad or ((ln.bbox[0], ln.bbox[1]), (ln.bbox[2], ln.bbox[1]), (ln.bbox[2], ln.bbox[3]),
                               (ln.bbox[0], ln.bbox[3]))
            tl, tr, _br, bl = (pymupdf.Point(p) for p in quad)
            dx, dy = tr.x - tl.x, tr.y - tl.y
            width = math.hypot(dx, dy)
            height = math.hypot(bl.x - tl.x, bl.y - tl.y)
            if width < 1 or height < 1:
                continue
            font = _font_for(text)
            size = max(2.0, height * 0.8)
            tw = _text_width(text, font, size)
            if tw <= 0:
                continue
            base = pymupdf.Point(bl.x + (tl.x - bl.x) * 0.22, bl.y + (tl.y - bl.y) * 0.22)   # базовая линия
            ang = math.degrees(math.atan2(dy, dx))
            m = pymupdf.Matrix(width / tw, 1) * pymupdf.Matrix(-ang)
            try:
                shape.insert_text(base, text, fontsize=size, fontname=font, render_mode=_INVISIBLE,
                                  morph=(base, m))
                n += 1
            except Exception:
                continue        # знак, которого нет в шрифте: строка пропускается, остальные остаются
        shape.commit(overlay=True)
    finally:
        if rot:
            page.set_rotation(rot)
    return n


def make_searchable_pdf(src, dst, pages=None, engine: str = 'auto', workers: int | None = None,
                        cache_dir: str | None = None, progress=None, cancel=None, results: dict | None = None,
                        **options) -> dict:
    """Сделать копию PDF с невидимым текстовым слоем поверх страниц-сканов.

    Страницы, где текст уже есть, не меняются. Картинки сканов не пережимаются: размер файла растёт только на
    текст. results: готовый результат recognize_pdf для этого файла (чтобы не распознавать второй раз).
    Возвращает {'pages': всего страниц, 'recognized': страниц с новым слоем, 'lines': строк текста, 'engine': имя}.
    """
    import pymupdf
    from .core import recognize_pdf
    src, dst = os.fspath(src), os.fspath(dst)
    if os.path.abspath(src) == os.path.abspath(dst):
        raise OcrError('Результат нельзя писать в тот же файл: укажите другое имя')
    if results is None:
        results = recognize_pdf(src, pages=pages, engine=engine, workers=workers, cache_dir=cache_dir,
                                only_scans=True, progress=progress, cancel=cancel, **options)
    doc = pymupdf.open(src)
    try:
        done = lines = 0
        name = ''
        for pno, pt in sorted(results.items()):
            if pt.source == 'layer' or not pt.lines or not 0 <= pno < len(doc):
                continue
            lines += add_text_layer(doc[pno], pt)
            done += 1
            name = pt.source
        os.makedirs(os.path.dirname(os.path.abspath(dst)) or '.', exist_ok=True)
        doc.save(dst, garbage=3, deflate=True)
        return {'pages': len(doc), 'recognized': done, 'lines': lines, 'engine': name}
    finally:
        doc.close()
