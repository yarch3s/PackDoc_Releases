"""Движки распознавания: общий вид и выбор по имени.

Движок получает картинку страницы (numpy, высота x ширина x 3, RGB) и умеет:
    read(img, fix_rotation=True) -> (строки, поворот, наклон)
строки = [(четыре угла [[x, y] * 4] по ходу текста, текст, уверенность 0..1)] в координатах картинки, повёрнутой на
«поворот» градусов по часовой стрелке (0, 90, 180, 270); наклон в градусах для сведения (углы уже настоящие).
Всё остальное (рендер, кэш, параллельность, перевод координат на страницу) делает общий код пакета.
"""
from __future__ import annotations

import re

from ..types import EngineUnavailable

# порядок автовыбора: первый доступный. Обоснование порядка: docs/LOGIC_ocr.md (замеры точности на кодах)
ORDER = ('rapid', 'windows', 'tesseract')


def _load(name: str):
    if name == 'rapid':
        from . import rapid
        return rapid
    if name == 'windows':
        from . import windows
        return windows
    if name == 'tesseract':
        from . import tesseract
        return tesseract
    raise EngineUnavailable(f'Неизвестный движок распознавания: {name!r}. Есть: {", ".join(ORDER)}.')


def engines() -> dict:
    """Какие движки есть на этой машине: {имя: {'available': bool, 'reason': почему нет, 'about': описание}}."""
    out = {}
    for name in ORDER:
        mod = _load(name)
        try:
            ok, reason = mod.available()
        except Exception as e:      # проверка доступности не должна ронять вызывающего
            ok, reason = False, f'{type(e).__name__}: {e}'
        out[name] = {'available': bool(ok), 'reason': '' if ok else reason, 'about': mod.ABOUT}
    return out


def pick(engine: str = 'auto') -> str:
    """Имя движка по просьбе: 'auto' = первый доступный по порядку ORDER; иначе проверка, что названный доступен."""
    if engine in (None, '', 'auto'):
        info = engines()
        for name in ORDER:
            if info[name]['available']:
                return name
        why = '; '.join(f'{n}: {info[n]["reason"]}' for n in ORDER)
        raise EngineUnavailable('Нет ни одного доступного движка распознавания. ' + why)
    if engine not in ORDER:
        raise EngineUnavailable(f'Неизвестный движок распознавания: {engine!r}. Есть: {", ".join(ORDER)}.')
    ok, reason = _load(engine).available()
    if not ok:
        raise EngineUnavailable(f'Движок {engine!r} недоступен: {reason}')
    return engine


def signature(name: str, **options) -> str:
    """Подпись движка для ключа кэша: имя и то, от чего зависит результат (файлы моделей, язык). Меняется модель
    или язык: меняется подпись, и старый кэш не подхватывается."""
    mod = _load(name)
    fn = getattr(mod, 'signature', None)
    from ..types import OcrError
    try:
        return fn(**options) if fn else name
    except OcrError:
        raise                       # неверная настройка (например, неизвестный режим чтения): сказать сразу
    except Exception:
        return name


def create(name: str, **options):
    """Готовый к работе движок (объект с методом read)."""
    return _load(name).Engine(**options)


# ------------------------------------------------------- общий поиск поворота для движков без своего определения

_COMMON = set('''the of and to in no for is by with from this that are be as at on or it date test certificate
material standard type name size code item result heat number quantity page batch steel grade spec order product
customer inspection according description contract invoice total weight length и в на по с для от не дата номер
результат стандарт заказ сталь марка плавка партия кол наименование'''.split())
_WORD = re.compile(r'[A-Za-zА-Яа-яЁё]{2,}|\d{2,}')


def _quality(items):
    """(число обычных слов, число букв и цифр в словах с учётом уверенности): чем больше, тем вернее поворот."""
    hits = 0
    alnum = 0.0
    for _q, text, conf in items:
        for w in _WORD.findall(text):
            alnum += len(w) * conf
            if w.lower() in _COMMON:
                hits += 1
    return hits, alnum


def _sideways(items) -> bool:
    """Похоже ли, что текст лежит боком: длинные куски выше, чем шире, либо текста почти нет."""
    tall = wide = 0
    for quad, text, _c in items:
        if len(text) < 4:
            continue
        xs = [p[0] for p in quad]
        ys = [p[1] for p in quad]
        if (max(ys) - min(ys)) > 1.3 * (max(xs) - min(xs)):
            tall += 1
        else:
            wide += 1
    return tall > wide or (tall + wide) < 5


def generic_read(engine, img, fix_rotation: bool = True):
    """Распознать с подбором поворота пробами: для движков, которые сами поворот страницы не определяют.
    Прямая страница стоит одного прохода; лежащая боком или перевёрнутая: двух-трёх."""
    from ..prep import rotate
    items = engine.recognize(img)
    skew = float(getattr(engine, 'skew', 0.0) or 0.0)
    if not fix_rotation:
        return items, 0, skew
    q0 = _quality(items)
    if _sideways(items):
        tries = (90, 270)
    elif q0[0] >= 3:
        return items, 0, skew
    else:
        tries = (180,)
    best = (q0, items, 0, skew)
    for deg in tries:
        got = engine.recognize(rotate(img, deg))
        q = _quality(got)
        if q > best[0]:
            best = (q, got, deg, float(getattr(engine, 'skew', 0.0) or 0.0))
    return best[1], best[2], best[3]
