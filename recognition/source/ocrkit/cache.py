"""Кэш результатов распознавания на диске.

Ключ страницы = содержимое файла (SHA-1) + номер страницы + имя движка + параметры: номер версии обработки
(core.ALGO), подпись движка (имена и размеры файлов моделей либо язык), разрешение, вырезка, настройки движка.
Сменились модели, версия обработки или настройки: ключ другой, старые тексты не подхватятся. Один и тот же файл под другим именем или в другой папке попадает в тот же кэш; изменённый файл
в старый кэш не попадает.

Раскладка: <папка кэша>/<первые 2 знака ключа файла>/<ключ файла>/<движок>-<ключ параметров>/p<страница>.json
В файле страницы: {"v": версия формата, "page": номер, "source": движок, "rotation": градусы, "skew": наклон,
"width", "height": размер страницы в точках, "page_rotation": свойство /Rotate, "seconds": время распознавания,
"rows": [число кусков в каждой зрительной строке], "lines": [{"t": текст, "b": [x0, y0, x1, y1], "c": уверенность,
"q": [[x, y] * 4]}]}.
Запись идёт через временный файл и переименование: оборванная запись не оставляет битого файла.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from .types import PageText

FORMAT = 2


def file_key(path: str) -> str:
    """SHA-1 содержимого файла (40 МБ считаются около 0.05 с)."""
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def params_key(params: dict) -> str:
    return hashlib.sha1(json.dumps(params, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:12]


class Cache:
    """Кэш одного файла для одного движка и набора параметров."""

    def __init__(self, cache_dir: str, path: str, engine: str, params: dict):
        fk = file_key(path)
        self.dir = os.path.join(cache_dir, fk[:2], fk, f'{engine}-{params_key(params)}')

    def _file(self, page: int) -> str:
        return os.path.join(self.dir, f'p{page:05d}.json')

    def get(self, page: int):
        fn = self._file(page)
        try:
            with open(fn, encoding='utf-8') as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None
        if d.get('v') != FORMAT or d.get('page') != page:
            return None
        try:
            os.utime(fn)        # отметка «недавно нужен» для чистки по возрасту
        except OSError:
            pass
        pt = PageText.from_json(d)
        pt.cached = True
        return pt

    def put(self, pt: PageText) -> None:
        try:
            os.makedirs(self.dir, exist_ok=True)
            fn = self._file(pt.page)
            tmp = f'{fn}.{os.getpid()}.tmp'
            d = pt.to_json()
            d['v'] = FORMAT
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False, separators=(',', ':'))
            os.replace(tmp, fn)
        except OSError:
            pass                # кэш не обязателен: нет места или прав, работаем без него


def clean_cache(cache_dir: str, max_age_days: float = 30.0, max_mb: float | None = None) -> dict:
    """Чистка кэша: убрать страницы, к которым не обращались дольше max_age_days; если задан max_mb, убирать самые
    давние, пока кэш не уложится в этот объём. Возвращает {'removed': файлов, 'left': файлов, 'mb': объём после}."""
    files = []
    for root, _dirs, names in os.walk(cache_dir):
        for n in names:
            fn = os.path.join(root, n)
            try:
                st = os.stat(fn)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, fn))
    files.sort()
    now = time.time()
    total = sum(s for _m, s, _f in files)
    removed = 0
    keep = []
    for m, s, fn in files:
        old = (now - m) > max_age_days * 86400
        big = max_mb is not None and total > max_mb * 1e6
        if old or big or fn.endswith('.tmp') and now - m > 3600:
            try:
                os.remove(fn)
                removed += 1
                total -= s
                continue
            except OSError:
                pass
        keep.append(fn)
    for root, _dirs, _names in os.walk(cache_dir, topdown=False):   # пустые папки
        if root != cache_dir:
            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except OSError:
                pass
    return {'removed': removed, 'left': len(keep), 'mb': round(total / 1e6, 2)}
