"""Движок «windows»: встроенное распознавание Windows 10/11 (Windows.Media.Ocr).

Ничего не добавляет к установщику, кроме маленьких пакетов winrt-*; работает быстро. Зависит от языков
распознавания, установленных у пользователя (Параметры -> Время и язык -> Язык): нужен английский (en-US) либо
другой язык с латиницей. Уверенность движок не сообщает: у всех строк ставится 0.9.
"""
from __future__ import annotations

import math

import numpy as np

from ..prep import to_gray

ABOUT = 'встроенное распознавание Windows (Windows.Media.Ocr): быстро, зависит от языков Windows у пользователя'

PREFER = ('en-US', 'en-GB', 'en', 'ru', 'ru-RU')    # порядок выбора языка, если язык не задан


def _langs():
    from winrt.windows.media.ocr import OcrEngine
    return [x.language_tag for x in OcrEngine.available_recognizer_languages]


def available():
    import sys
    if sys.platform != 'win32':
        return False, 'только для Windows'
    try:
        from winrt.windows.globalization import Language  # noqa: F401
        from winrt.windows.graphics.imaging import SoftwareBitmap  # noqa: F401
        from winrt.windows.media.ocr import OcrEngine  # noqa: F401
        from winrt.windows.storage.streams import DataWriter  # noqa: F401
    except Exception as e:
        return False, f'нет пакетов winrt (winrt-Windows.Media.Ocr и соседних): {type(e).__name__}: {e}'
    try:
        langs = _langs()
    except Exception as e:
        return False, f'Windows не отдаёт список языков распознавания: {type(e).__name__}: {e}'
    if not langs:
        return False, 'в Windows не установлено ни одного языка распознавания'
    return True, ''


def pick_lang(lang: str | None = None) -> str:
    langs = _langs()
    if lang:
        for x in langs:
            if x.lower() == lang.lower() or x.lower().split('-')[0] == lang.lower():
                return x
        from ..types import EngineUnavailable
        raise EngineUnavailable(f'В Windows нет языка распознавания {lang!r}. Установлены: {", ".join(langs)}')
    for want in PREFER:
        for x in langs:
            if x.lower() == want.lower():
                return x
    return langs[0]


def signature(lang: str | None = None, **_) -> str:
    try:
        return 'windows|' + pick_lang(lang)
    except Exception:
        return 'windows|' + str(lang or '')


class Engine:
    name = 'windows'

    def __init__(self, lang: str | None = None, drop_color: int = 1, **_):
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        from ..types import EngineUnavailable
        ok, why = available()
        if not ok:
            raise EngineUnavailable(f'Движок windows недоступен: {why}')
        self.lang = pick_lang(lang)
        self._eng = OcrEngine.try_create_from_language(Language(self.lang))
        if self._eng is None:
            raise EngineUnavailable(f'Windows не создала распознаватель для языка {self.lang}')
        self._max = int(OcrEngine.max_image_dimension)
        self.drop_color = int(drop_color)
        self.timing = {'rec': 0.0}

    def _run(self, img):
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter
        h, w = img.shape[:2]
        g = to_gray(img, self.drop_color)
        k = 1
        while max(h, w) // k > self._max:       # предел стороны картинки у движка (10000)
            k += 1
        if k > 1:
            g = g[::k, ::k]
            h, w = g.shape
        wr = DataWriter()
        wr.write_bytes(np.ascontiguousarray(g).tobytes())
        bmp = SoftwareBitmap.create_copy_from_buffer(wr.detach_buffer(), BitmapPixelFormat.GRAY8, w, h)
        res = self._eng.recognize_async(bmp).get()
        return res, k, w, h

    def recognize(self, img):
        import time
        t0 = time.perf_counter()
        res, k, w, h = self._run(img)
        ang = float(res.text_angle or 0.0)
        th = math.radians(ang)
        cos, sin = math.cos(th), math.sin(th)
        cx, cy = w / 2.0, h / 2.0
        out = []
        for ln in res.lines:
            ws = list(ln.words)
            if not ws:
                continue
            x0 = min(x.bounding_rect.x for x in ws)
            y0 = min(x.bounding_rect.y for x in ws)
            x1 = max(x.bounding_rect.x + x.bounding_rect.width for x in ws)
            y1 = max(x.bounding_rect.y + x.bounding_rect.height for x in ws)
            quad = []
            for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
                if ang:     # рамки даны в выровненной картинке: вернуть в исходную поворотом вокруг центра
                    dx, dy = px - cx, py - cy
                    px, py = cx + dx * cos - dy * sin, cy + dx * sin + dy * cos
                quad.append([px * k, py * k])
            out.append((quad, ln.text, 0.9))
        self.skew = ang
        self.timing['rec'] += time.perf_counter() - t0
        return out

    def read(self, img, fix_rotation: bool = True):
        from . import generic_read
        return generic_read(self, img, fix_rotation)
