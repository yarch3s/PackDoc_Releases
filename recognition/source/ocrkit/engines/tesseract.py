"""Движок «tesseract»: Tesseract 5 через пакет tesserocr (библиотека внутри колеса, отдельная установка не нужна).

Нужны данные языков *.traineddata (eng обязательно): папка задаётся параметром tessdata, переменной окружения
OCRKIT_TESSDATA или TESSDATA_PREFIX, либо лежит рядом с пакетом в ocrkit/tessdata.
"""
from __future__ import annotations

import os

import numpy as np

from ..prep import to_gray

ABOUT = 'Tesseract 5 (пакет tesserocr + данные языков): классический движок, средняя скорость'

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tessdata_dir(explicit: str | None = None) -> str:
    for d in (explicit, os.environ.get('OCRKIT_TESSDATA'), os.path.join(_HERE, 'tessdata'),
              os.environ.get('TESSDATA_PREFIX')):
        if d and os.path.isfile(os.path.join(d, 'eng.traineddata')):
            return d
    return ''


def available():
    try:
        import tesserocr  # noqa: F401
    except Exception as e:
        return False, f'нет пакета tesserocr ({type(e).__name__}: {e})'
    if not tessdata_dir():
        return False, ('нет данных языков: положите eng.traineddata в папку ocrkit/tessdata или задайте '
                       'переменную OCRKIT_TESSDATA')
    return True, ''


def signature(lang: str = 'eng', psm: int = 11, tessdata: str | None = None, **_) -> str:
    d = tessdata_dir(tessdata)
    sizes = [os.path.getsize(os.path.join(d, x + '.traineddata')) if d and os.path.isfile(
        os.path.join(d, x + '.traineddata')) else 0 for x in lang.split('+')]
    return f'tesseract|{lang}|{psm}|' + ','.join(str(x) for x in sizes)


class Engine:
    name = 'tesseract'

    def __init__(self, lang: str = 'eng', tessdata: str | None = None, psm: int = 11, threads: int = 1,
                 drop_color: int = 1, **_):
        from ..types import EngineUnavailable
        os.environ.setdefault('OMP_THREAD_LIMIT', str(max(1, int(threads))))   # иначе каждый вызов берёт 4 ядра
        try:
            import tesserocr
        except Exception as e:
            raise EngineUnavailable(f'Движок tesseract недоступен: нет пакета tesserocr ({e})') from e
        d = tessdata_dir(tessdata)
        if not d:
            raise EngineUnavailable('Движок tesseract недоступен: нет данных языков (eng.traineddata)')
        miss = [x for x in lang.split('+') if not os.path.isfile(os.path.join(d, x + '.traineddata'))]
        if miss:
            raise EngineUnavailable(f'Движок tesseract: в {d} нет данных языков {", ".join(miss)}')
        self._t = tesserocr
        self._api = tesserocr.PyTessBaseAPI(path=d, lang=lang, psm=psm, oem=tesserocr.OEM.LSTM_ONLY)
        self.lang = lang
        self.drop_color = int(drop_color)
        self.timing = {'rec': 0.0}

    def recognize(self, img, dpi: int = 200):
        import time
        t0 = time.perf_counter()
        h, w = img.shape[:2]
        g = np.ascontiguousarray(to_gray(img, self.drop_color))
        api = self._api
        api.SetImageBytes(g.tobytes(), w, h, 1, w)
        api.SetSourceResolution(dpi)
        api.Recognize()
        out = []
        it = api.GetIterator()
        level = self._t.RIL.TEXTLINE
        if it is not None:
            while True:
                try:
                    text = (it.GetUTF8Text(level) or '').strip()
                except RuntimeError:
                    text = ''
                if text:
                    box = it.BoundingBox(level)
                    if box:
                        x0, y0, x1, y1 = box
                        conf = max(0.0, min(1.0, it.Confidence(level) / 100.0))
                        out.append(([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], ' '.join(text.split()), conf))
                if not it.Next(level):
                    break
        api.Clear()
        self.skew = 0.0
        self.timing['rec'] += time.perf_counter() - t0
        return out

    def read(self, img, fix_rotation: bool = True):
        from . import generic_read
        return generic_read(self, img, fix_rotation)
