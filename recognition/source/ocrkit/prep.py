"""Подготовка страницы: рендер PDF в картинку, поворот, перевод координат из картинки обратно на страницу."""
from __future__ import annotations

import numpy as np

DPI_DEFAULT = 200           # разрешение рендера, если у страницы нет своей картинки-скана
DPI_MIN, DPI_MAX = 150, 300
TEXT_LAYER_MIN = 40         # знаков в текстовом слое, с которых страница считается страницей с текстом


def has_text_layer(page) -> bool:
    """Есть ли у страницы настоящий текстовый слой (а не пара служебных знаков поверх скана)."""
    return len(page.get_text().strip()) >= TEXT_LAYER_MIN


def scan_dpi(page) -> int:
    """Разрешение рендера страницы: родное разрешение скана (чтобы не пересчитывать картинку лишний раз),
    в пределах DPI_MIN..DPI_MAX. Скан = картинка, занимающая почти всю страницу."""
    best = 0
    try:
        for info in page.get_image_info():
            x0, y0, x1, y1 = info['bbox']
            if (x1 - x0) * (y1 - y0) < 0.5 * page.rect.width * page.rect.height:
                continue
            # картинка может лежать на странице боком: сравниваем длинные стороны
            px = max(info['width'], info['height'])
            pt = max(x1 - x0, y1 - y0)
            best = max(best, round(px / pt * 72))
    except Exception:
        best = 0
    if not best:
        return DPI_DEFAULT
    return int(min(max(best, DPI_MIN), DPI_MAX))


def clip_rect(page, clip):
    """Вырезка в долях видимой страницы (x0, y0, x1, y1) -> прямоугольник в точках видимой страницы."""
    import pymupdf
    r = page.rect
    x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in clip)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f'пустая вырезка страницы: {clip}')
    return pymupdf.Rect(r.x0 + x0 * r.width, r.y0 + y0 * r.height, r.x0 + x1 * r.width, r.y0 + y1 * r.height)


def render(page, dpi: int | None = None, clip=None):
    """Картинка страницы (RGB, numpy) и масштаб: пикселей на точку страницы. clip: вырезка в долях видимой
    страницы (x0, y0, x1, y1) либо None."""
    dpi = dpi or scan_dpi(page)
    zoom = dpi / 72.0
    import pymupdf
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB, alpha=False,
                          clip=clip_rect(page, clip) if clip else None)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
    return img, zoom


def to_gray(img, drop_color: int = 1):
    """Серая картинка для движков, которым цвет не нужен. drop_color=1: серое = самый яркий из трёх каналов:
    цветные чернила (красная или синяя печать поверх текста) становятся светлыми, чёрный текст остаётся чёрным.
    drop_color=0: обычная яркость."""
    if img.ndim == 2:
        return img
    if img.shape[2] == 1:
        return img[:, :, 0]
    if drop_color:
        return img.max(axis=2)
    g = (img[:, :, 0].astype(np.uint16) * 77 + img[:, :, 1].astype(np.uint16) * 150
         + img[:, :, 2].astype(np.uint16) * 29) >> 8
    return g.astype(np.uint8)


def rotate(img, degrees: int):
    """Поворот картинки по часовой стрелке на 0, 90, 180 или 270 градусов."""
    k = (degrees // 90) % 4
    return img if k == 0 else np.ascontiguousarray(np.rot90(img, k=-k))


def unrotate_point(x: float, y: float, degrees: int, w: int, h: int):
    """Точка повёрнутой картинки -> точка исходной (w, h: размер исходной картинки в пикселях)."""
    k = (degrees // 90) % 4
    if k == 0:
        return x, y
    if k == 1:          # исходная (x, y) -> (h - y, x)
        return y, h - x
    if k == 2:
        return w - x, h - y
    return w - y, x     # k == 3: исходная (x, y) -> (y, w - x)


def to_page(quad, degrees: int, w: int, h: int, zoom: float, origin=(0.0, 0.0), derot=None):
    """Углы строки из повёрнутой картинки в координаты страницы PDF. Возвращает (bbox, quad).

    Цепочка: картинка, повёрнутая для распознавания -> исходная картинка рендера -> страница «как на экране»
    (деление на zoom) -> система координат страницы без её свойства /Rotate (derot = шесть чисел матрицы
    page.derotation_matrix). Последний шаг нужен, чтобы координаты совпадали с тем, что PyMuPDF отдаёт в
    page.get_text() и page.search_for() и что он ждёт в page.insert_text()."""
    pts = []
    for x, y in quad:
        ox, oy = unrotate_point(x, y, degrees, w, h)
        px, py = origin[0] + ox / zoom, origin[1] + oy / zoom
        if derot is not None:
            a, b, c, d, e, f = derot
            px, py = a * px + c * py + e, b * px + d * py + f
        pts.append((round(float(px), 2), round(float(py), 2)))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys)), tuple(pts)


def page_frame(page, clip=None):
    """Сведения о странице для перевода координат: (начало картинки на видимой странице, матрица derot или None,
    ширина и высота страницы без учёта /Rotate, значение /Rotate). clip: вырезка в долях видимой страницы."""
    rot = page.rotation
    r = page.rect
    derot = tuple(page.derotation_matrix) if rot else None
    w, h = (r.height, r.width) if rot in (90, 270) else (r.width, r.height)
    if clip:
        c = clip_rect(page, clip)
        # рендер вырезки начинается с целого пикселя: начало берём то же, что у картинки
        return (c.x0, c.y0), derot, w, h, rot
    return (r.x0, r.y0), derot, w, h, rot


def reading_order(items, skew: float = 0.0):
    """Порядок чтения: строки сверху вниз, внутри зрительной строки слева направо.

    items: [(quad, text, conf)] в картинке, где текст стоит прямо (с точностью до наклона skew в градусах).
    Возвращает [[индексы кусков одной зрительной строки]]. Куски считаются одной зрительной строкой, если их
    середины по высоте близки (ближе половины высоты меньшего куска).
    """
    import math
    th = math.radians(skew)
    cos, sin = math.cos(th), math.sin(th)
    boxes = []
    for i, (quad, _t, _c) in enumerate(items):
        # выровненные координаты: поворот на -skew
        us = [x * cos + y * sin for x, y in quad]
        vs = [-x * sin + y * cos for x, y in quad]
        boxes.append((i, min(us), (min(vs) + max(vs)) / 2.0, max(vs) - min(vs)))
    boxes.sort(key=lambda b: b[2])
    rows = []
    for b in boxes:
        if rows:
            last = rows[-1]
            cy = sum(x[2] for x in last) / len(last)
            hh = sum(x[3] for x in last) / len(last)
            if abs(b[2] - cy) < 0.5 * min(hh, b[3] or hh):
                last.append(b)
                continue
        rows.append([b])
    return [[b[0] for b in sorted(r, key=lambda x: x[1])] for r in rows]
