"""Движок «rapid»: модели PaddleOCR (PP-OCR) в формате ONNX на onnxruntime. Процессор, без сети.

Модели взяты из проекта RapidOCR (те же файлы .onnx), но обвязка своя и лёгкая: нужны только numpy и onnxruntime
(без OpenCV, Shapely, pyclipper). Три шага:
1. det: сеть DB даёт карту «здесь текст»; по ней строятся прямоугольники строк (связные области, разбор в numpy);
2. поворот: по форме областей видно, лежит ли страница боком; перевёрнута ли она, решает маленькая сеть cls
   по нескольким длинным строкам; небольшой наклон берётся из наклона длинных строк и учитывается при вырезании;
3. rec: каждая строка вырезается, приводится к высоте 48 пикселей и читается сетью (CTC).
4. режим «точнее» (quality='accurate'): строки с цифрами читает ещё и вторая модель (PP-OCRv4, у неё другие
   ошибки), два чтения сливаются по знакам (merge_readings); в обоих режимах к тексту строки применяются правки
   по смыслу поля (fixes.py).
"""
from __future__ import annotations

import difflib
import math
import os
import re

import numpy as np

ABOUT = 'модели PaddleOCR (PP-OCR) на onnxruntime: латиница, цифры, китайский; не зависит от Windows'

MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models')
DET_MODEL = 'PP-OCRv6_det_tiny.onnx'
REC_MODEL = 'PP-OCRv6_rec_tiny.onnx'
CLS_MODEL = 'ch_ppocr_mobile_v2.0_cls_infer.onnx'
REC2_MODEL = 'ch_PP-OCRv4_rec_infer.onnx'      # вторая модель чтения для режима «точнее»

# Режимы чтения: 'fast' = одна модель чтения; 'accurate' = строки с цифрами читает и вторая модель, чтения
# сливаются по знакам. Замеры в docs/LOGIC_ocr.md (раздел о точности)
QUALITIES = ('fast', 'accurate')
QUALITY = 'accurate'
# Слияние двух чтений строки: знак второй модели берётся, если первая не увереннее её больше чем на MERGE_MARGIN;
# знак первой модели, которого у второй нет, убирается, если его вероятность ниже MERGE_DROP
MERGE_MARGIN = 0.3
MERGE_DROP = 0.7
# Какие слова строки перечитывает вторая модель: слово с цифрами, в нём не меньше REREAD_MIN_ALNUM букв и цифр,
# цифр не меньше REREAD_MIN_DIGITS и не меньше доли REREAD_DIGIT_SHARE (так отсекаются русские слова, прочитанные
# латиницей с «цифрами» 3, 4, 6), и основная модель хотя бы в одном знаке слова уверена меньше REREAD_CONF
REREAD_MIN_ALNUM = 5
REREAD_MIN_DIGITS = 2
REREAD_DIGIT_SHARE = 0.0
REREAD_CONF = 0.98
REREAD_PAD = 0.75       # поля вырезки слова слева и справа от крайних знаков, в высотах строки
REC2_BATCH = 1          # вторая модель читает слова по одному: пачками у неё выходит втрое медленнее (замер)
_WORD = re.compile(r'\S+')

CLS_VOTES = 12      # сколько длинных строк голосуют, перевёрнута ли страница
# Наклон строк (градусы), замеры в docs/LOGIC_ocr.md: меньше SKEW_MIN считается нулевым (строки вырезаются по
# осям картинки); от SKEW_MIN до SKEW_DESKEW строки вырезаются вдоль наклона сдвигом столбцов (дёшево и так же
# точно); от SKEW_DESKEW страница поворачивается и строки ищутся заново (точнее, но ещё один проход поиска)
SKEW_MIN = 0.1
SKEW_DESKEW = 0.6
SKEW_AGREE = 0.5    # строки «согласны», если их наклон отличается от общего не больше чем на столько градусов
ARENA = False       # арена памяти onnxruntime: при входах разного размера с ней память растёт

_DET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_DET_STD = np.array([0.229, 0.224, 0.225], np.float32)


def models_dir() -> str:
    return os.environ.get('OCRKIT_MODELS') or MODELS_DIR


def _model_path(name: str) -> str:
    return name if os.path.isabs(name) else os.path.join(models_dir(), name)


def available():
    try:
        import onnxruntime  # noqa: F401
    except Exception as e:
        return False, f'нет библиотеки onnxruntime ({type(e).__name__}: {e})'
    miss = [n for n in (DET_MODEL, REC_MODEL, CLS_MODEL) if not os.path.isfile(_model_path(n))]
    if miss:
        return False, f'нет файлов моделей в {models_dir()}: {", ".join(miss)}'
    return True, ''


def _quality(quality: str | None, rec2_model: str | None = None) -> str:
    """Действующий режим: «точнее» возможен, только если файл второй модели на месте."""
    q = quality or QUALITY
    if q not in QUALITIES:
        from ..types import OcrError
        raise OcrError(f'Неизвестный режим чтения {q!r}: есть {", ".join(QUALITIES)}')
    if q == 'accurate' and not os.path.isfile(_model_path(rec2_model or REC2_MODEL)):
        return 'fast'
    return q


def signature(det_model: str | None = None, rec_model: str | None = None, cls_model: str | None = None,
              quality: str | None = None, rec2_model: str | None = None, **_) -> str:
    """Имена и размеры файлов моделей и режим чтения: другая модель или другой режим = другой ключ кэша."""
    parts = ['rapid']
    names = [det_model or DET_MODEL, rec_model or REC_MODEL, cls_model or CLS_MODEL]
    q = _quality(quality, rec2_model)
    if q == 'accurate':
        names.append(rec2_model or REC2_MODEL)
    for name in names:
        p = _model_path(name)
        size = os.path.getsize(p) if os.path.isfile(p) else 0
        parts.append(f'{os.path.basename(p)}:{size}')
    parts.append(q)
    return '|'.join(parts)


def merge_readings(ta: str, pa, tb: str, pb, inner_only: bool = False) -> str:
    """Слияние двух чтений одной строки по знакам. ta, pa: текст и вероятности знаков основной модели; tb, pb:
    то же у второй модели. Пробелы всегда берутся у основной модели (вторая их в латинице часто не ставит).
    Там, где чтения расходятся знак в знак, берётся знак второй модели, если основная не увереннее её больше чем
    на MERGE_MARGIN. Знаки, которые есть только у второй модели, добавляются. Знак, который есть только у
    основной, остаётся, если она в нём уверена (от MERGE_DROP) либо это первый или последний знак слова строки
    (у края вырезки вторая модель теряет знак чаще). inner_only: не добавлять знаки второй модели с краёв
    (для слова, вырезанного из строки: с краю в вырезку может попасть кусок соседнего знака)."""
    if ta == tb or not tb.strip():
        return ta
    alnum = [i for i, c in enumerate(ta) if c.isalnum()]
    edge = {alnum[0], alnum[-1]} if alnum else set()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op == 'equal':
            out.append(ta[i1:i2])
        elif op == 'replace' and i2 - i1 == j2 - j1:
            for k in range(i2 - i1):
                out.append(tb[j1 + k] if pb[j1 + k] > pa[i1 + k] - MERGE_MARGIN else ta[i1 + k])
        elif op == 'insert':
            if not (inner_only and (i1 == 0 or i1 == len(ta))):
                out.append(''.join(c for c in tb[j1:j2] if not c.isspace()))
        elif op == 'delete':
            for i in range(i1, i2):
                if ta[i].isspace() or i in edge or pa[i] >= MERGE_DROP:
                    out.append(ta[i])
        else:                                   # замена куска на кусок другой длины: остаётся основа
            out.append(ta[i1:i2])
    return ''.join(out)


def _session(path: str, threads: int):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, int(threads))
    so.inter_op_num_threads = 1
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.log_severity_level = 3
    so.enable_cpu_mem_arena = ARENA
    so.add_session_config_entry('session.intra_op.allow_spinning', '0')     # не крутить ядра вхолостую
    return ort.InferenceSession(path, so, providers=['CPUExecutionProvider'])


# ---------------------------------------------------------------------------------------------- картинки (numpy)

def resize(img, nh: int, nw: int):
    """Билинейное изменение размера (H x W x C) -> float32. При сильном уменьшении сначала усреднение блоками."""
    h, w = img.shape[:2]
    if (h, w) == (nh, nw):
        return img.astype(np.float32)
    ky, kx = max(1, int(h / nh)), max(1, int(w / nw))
    if ky >= 2 or kx >= 2:
        hh, ww = (h // ky) * ky, (w // kx) * kx
        img = img[:hh, :ww].reshape(hh // ky, ky, ww // kx, kx, -1).mean(axis=(1, 3), dtype=np.float32)
        h, w = img.shape[:2]
    ys = (np.arange(nh, dtype=np.float32) + 0.5) * (h / nh) - 0.5
    xs = (np.arange(nw, dtype=np.float32) + 0.5) * (w / nw) - 0.5
    y0 = np.floor(ys).astype(np.intp)
    x0 = np.floor(xs).astype(np.intp)
    wy = (ys - y0)[:, None, None]
    wx = (xs - x0)[None, :, None]
    y1 = np.clip(y0 + 1, 0, h - 1)
    x1 = np.clip(x0 + 1, 0, w - 1)
    y0 = np.clip(y0, 0, h - 1)
    x0 = np.clip(x0, 0, w - 1)
    rows = img[y0].astype(np.float32) * (1 - wy) + img[y1].astype(np.float32) * wy
    return rows[:, x0] * (1 - wx) + rows[:, x1] * wx


def sample_quad(img, cx, cy, bw, bh, cos, sin, out_h: int, flip: bool = False, stretch: float = 1.0):
    """Вырезать повёрнутый прямоугольник (центр, ширина вдоль строки, высота, направление строки) и привести к
    высоте out_h. Билинейная выборка; при уменьшении больше чем в 1.5 раза выборка гуще с усреднением."""
    h, w = img.shape[:2]
    scale = bh / out_h
    out_w = max(8, int(round(bw / scale * stretch)))
    ss = max(1, int(math.ceil(scale / 1.5)))
    nu, nv = out_w * ss, out_h * ss
    u = ((np.arange(nu, dtype=np.float32) + 0.5) / nu - 0.5) * bw
    v = ((np.arange(nv, dtype=np.float32) + 0.5) / nv - 0.5) * bh
    if flip:
        u, v = -u, -v
    X = cx + u[None, :] * cos - v[:, None] * sin
    Y = cy + u[None, :] * sin + v[:, None] * cos
    x0 = np.floor(X).astype(np.intp)
    y0 = np.floor(Y).astype(np.intp)
    fx = (X - x0)[:, :, None]
    fy = (Y - y0)[:, :, None]
    x1 = np.clip(x0 + 1, 0, w - 1)
    y1 = np.clip(y0 + 1, 0, h - 1)
    x0 = np.clip(x0, 0, w - 1)
    y0 = np.clip(y0, 0, h - 1)
    a = img[y0, x0].astype(np.float32)
    b = img[y0, x1].astype(np.float32)
    c = img[y1, x0].astype(np.float32)
    d = img[y1, x1].astype(np.float32)
    out = (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy
    if ss > 1:
        out = out.reshape(out_h, ss, out_w, ss, -1).mean(axis=(1, 3))
    return out


def rotate_small(img, degrees: float, fill: float = 255.0):
    """Выровнять наклонённую страницу: строки, шедшие под углом degrees (по часовой на экране), становятся
    горизонтальными. Размер картинки тот же, поворот вокруг центра, билинейная выборка, поля белые.
    Точка результата p' берётся из исходной точки c + R(угол) * (p' - c)."""
    h, w = img.shape[:2]
    th = math.radians(degrees)
    cos, sin = math.cos(th), math.sin(th)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    xs = np.arange(w, dtype=np.float32) - cx
    ys = np.arange(h, dtype=np.float32) - cy
    X = cx + xs[None, :] * cos - ys[:, None] * sin
    Y = cy + xs[None, :] * sin + ys[:, None] * cos
    x0 = np.floor(X).astype(np.intp)
    y0 = np.floor(Y).astype(np.intp)
    fx = (X - x0)[:, :, None]
    fy = (Y - y0)[:, :, None]
    out_of = ((x0 < 0) | (y0 < 0) | (x0 >= w - 1) | (y0 >= h - 1))
    x0 = np.clip(x0, 0, w - 2)
    y0 = np.clip(y0, 0, h - 2)
    a = img[y0, x0].astype(np.float32)
    b = img[y0, x0 + 1].astype(np.float32)
    c = img[y0 + 1, x0].astype(np.float32)
    d = img[y0 + 1, x0 + 1].astype(np.float32)
    out = (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy
    out[out_of] = fill
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def crop_box(img, cx, cy, bw, bh, out_h: int, stretch: float = 1.0):
    """Вырезать прямоугольник по осям картинки и привести к высоте out_h (быстрый путь для страниц без наклона)."""
    h, w = img.shape[:2]
    x0 = max(0, int(round(cx - bw / 2)))
    x1 = min(w, int(round(cx + bw / 2)))
    y0 = max(0, int(round(cy - bh / 2)))
    y1 = min(h, int(round(cy + bh / 2)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return np.full((out_h, 8, img.shape[2]), 255.0, np.float32)
    sub = img[y0:y1, x0:x1]
    out_w = max(8, int(round(sub.shape[1] * out_h / sub.shape[0] * stretch)))
    return resize(sub, out_h, out_w)


def crop_sheared(img, cx, cy, bw, bh, th, out_h: int, stretch: float = 1.0):
    """Строка с небольшим наклоном: срез по осям, где каждый столбец сдвинут по высоте вслед за строкой.
    Дешевле поворота: одна выборка в родном разрешении, затем обычное приведение к высоте out_h."""
    h, w = img.shape[:2]
    x0 = max(0, int(round(cx - bw / 2)))
    x1 = min(w, int(round(cx + bw / 2)))
    hh = max(2, int(round(bh)))
    if x1 - x0 < 2:
        return np.full((out_h, 8, img.shape[2]), 255.0, np.float32)
    xs = np.arange(x0, x1)
    top = np.rint(cy - bh / 2 + (xs - cx) * math.tan(th)).astype(np.intp)
    rows = np.clip(top[None, :] + np.arange(hh, dtype=np.intp)[:, None], 0, h - 1)
    sub = img[rows, xs[None, :]]
    out_w = max(8, int(round(sub.shape[1] * out_h / sub.shape[0] * stretch)))
    return resize(sub, out_h, out_w)


def crop_line(img, box, out_h: int, stretch: float = 1.0):
    """Вырезка строки высотой out_h; stretch > 1 растягивает её по ширине (узким знакам вроде III просторнее)."""
    cx, cy, bw, bh, _s, th = (float(v) for v in box)
    if th == 0.0:
        return crop_box(img, cx, cy, bw, bh, out_h, stretch)
    if abs(th) < 0.14:      # до 8 градусов
        return crop_sheared(img, cx, cy, bw, bh, th, out_h, stretch)
    return sample_quad(img, cx, cy, bw, bh, math.cos(th), math.sin(th), out_h, stretch=stretch)


def lies_sideways(prob, thresh: float = 0.3) -> bool:
    """Лежит ли текст боком, по карте текста: у прямых строк горизонтальные отрезки длинные и их мало, вертикальные
    короткие и их много; у лежащих боком наоборот."""
    m = prob > thresh
    nh = np.count_nonzero(m[:, 1:] & ~m[:, :-1]) + np.count_nonzero(m[:, 0])
    nv = np.count_nonzero(m[1:, :] & ~m[:-1, :]) + np.count_nonzero(m[0, :])
    return nh > 2 * nv and nh > 200


# ------------------------------------------------------------------------------------- разбор карты текста (DB)

def _components(mask):
    """Связные области двоичной карты через отрезки строк. Возвращает (метка отрезка, строка, начало, конец, число
    областей); конец не включается."""
    h, w = mask.shape
    pad = np.zeros((h, w + 2), np.int8)
    pad[:, 1:-1] = mask
    d = np.diff(pad, axis=1)
    starts = d == 1
    ry, rs = np.nonzero(starts)
    re = np.nonzero(d == -1)[1]
    n = len(ry)
    if n == 0:
        return np.zeros(0, np.intp), ry, rs, re, 0
    rid = (np.cumsum(starts.ravel()) - 1).reshape(h, w + 1)[:, :w]
    both = mask[:-1] & mask[1:]
    pairs = np.unique(rid[:-1][both].astype(np.int64) * n + rid[1:][both])
    parent = list(range(n))
    for p in pairs.tolist():
        a, b = divmod(p, n)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        while parent[b] != b:
            parent[b] = parent[parent[b]]
            b = parent[b]
        if a != b:
            parent[b] = a
    root = np.empty(n, np.intp)
    for i in range(n):
        j = i
        while parent[j] != j:
            j = parent[j]
        root[i] = j
    _, lab = np.unique(root, return_inverse=True)
    return lab, ry, rs, re, int(lab.max()) + 1


def db_boxes(prob, thresh=0.3, box_thresh=0.5, unclip=1.6, dilate=True, angle=None, skew_min=None):
    """Прямоугольники строк по карте вероятности текста.

    Возвращает (boxes, stats): boxes = массив N x 6 (cx, cy, ширина вдоль строки, высота, оценка, угол в радианах)
    в пикселях карты; stats = {'angle': общий наклон строк в градусах, 'wide': суммарная длина лежачих областей,
    'tall': суммарная длина стоячих областей}. angle=None: наклон определить по длинным областям; иначе взять
    заданный (в градусах).
    """
    mask = prob > thresh
    if dilate:
        m = mask.copy()
        m[:, 1:] |= mask[:, :-1]
        m[1:, :] |= mask[:-1, :]
        m[1:, 1:] |= mask[:-1, :-1]
        mask = m
    lab, ry, rs, re, k = _components(mask)
    stats = {'angle': 0.0, 'wide': 0.0, 'tall': 0.0}
    if k == 0:
        return np.zeros((0, 6), np.float32), stats
    cnt = (re - rs).astype(np.float64)
    ryf = ry.astype(np.float64)
    a = rs.astype(np.float64)
    b = re.astype(np.float64)
    sx = (a + b - 1) * cnt / 2
    sxx = ((b - 1) * b * (2 * b - 1) - (a - 1) * a * (2 * a - 1)) / 6
    N = np.bincount(lab, cnt, k)
    mx = np.bincount(lab, sx, k) / N
    my = np.bincount(lab, ryf * cnt, k) / N
    vxx = np.bincount(lab, sxx, k) / N - mx * mx
    vyy = np.bincount(lab, ryf * ryf * cnt, k) / N - my * my
    vxy = np.bincount(lab, ryf * sx, k) / N - mx * my
    x0 = np.full(k, 1e9)
    x1 = np.full(k, -1e9)
    y0 = np.full(k, 1e9)
    y1 = np.full(k, -1e9)
    np.minimum.at(x0, lab, a)
    np.maximum.at(x1, lab, b)
    np.minimum.at(y0, lab, ryf)
    np.maximum.at(y1, lab, ryf + 1)
    bw, bh = x1 - x0, y1 - y0
    wide = (bw >= 4 * bh) & (bw >= 40)
    tall = (bh >= 4 * bw) & (bh >= 40)
    stats['wide'] = float(bw[wide].sum())
    stats['tall'] = float(bh[tall].sum())
    if angle is None:
        angle = 0.0
        if wide.any():
            ang = np.degrees(np.arctan2(vxy[wide], np.maximum(vxx[wide], 1e-9)))
            wgt = bw[wide]
            order = np.argsort(ang)
            cum = np.cumsum(wgt[order])
            angle = float(ang[order][np.searchsorted(cum, cum[-1] / 2)])     # взвешенная медиана
            # наклон страницы есть, только если строки согласны: у настоящего наклона почти все длинные строки
            # идут под одним углом. На странице без обычных строк (круговая диаграмма, чертёж) углы вразнобой:
            # такой «наклон» не учитывается
            agree = float(wgt[np.abs(ang - angle) <= SKEW_AGREE].sum() / max(wgt.sum(), 1e-9))
            stats['agree'] = agree
            if agree < 0.5:
                angle = 0.0
    stats['angle'] = float(angle)
    th = math.radians(angle)
    if abs(angle) < (SKEW_MIN if skew_min is None else skew_min):
        th = 0.0
    cos, sin = math.cos(th), math.sin(th)
    if th == 0.0:
        u0, u1, v0, v1 = x0, x1, y0, y1
    else:
        # проекции концов отрезков на оси строки: крайние точки области лежат на концах её отрезков
        u0 = np.full(k, 1e9)
        u1 = np.full(k, -1e9)
        v0 = np.full(k, 1e9)
        v1 = np.full(k, -1e9)
        for xx in (a, b):
            for yy in (ryf, ryf + 1):
                u = xx * cos + yy * sin
                v = -xx * sin + yy * cos
                np.minimum.at(u0, lab, u)
                np.maximum.at(u1, lab, u)
                np.minimum.at(v0, lab, v)
                np.maximum.at(v1, lab, v)
    w_ = u1 - u0
    h_ = v1 - v0
    keep = np.minimum(w_, h_) >= 3
    # оценка области: средняя вероятность по её пикселям (сумма по отрезкам через накопленные суммы строк)
    rc = np.zeros((prob.shape[0], prob.shape[1] + 1), np.float64)
    np.cumsum(prob, axis=1, dtype=np.float64, out=rc[:, 1:])
    score = np.bincount(lab, rc[ry, re] - rc[ry, rs], k) / N
    keep &= score >= box_thresh
    dist = w_ * h_ * unclip / np.maximum(2 * (w_ + h_), 1e-9)
    cu = (u0 + u1) / 2
    cv = (v0 + v1) / 2
    cx = cu * cos - cv * sin
    cy = cu * sin + cv * cos
    out = np.stack([cx, cy, w_ + 2 * dist, h_ + 2 * dist, score, np.full(k, th)], axis=1)[keep]
    out = out[np.minimum(out[:, 2], out[:, 3]) >= 5]
    return out.astype(np.float32), stats


def _quad(cx, cy, bw, bh, th):
    cos, sin = math.cos(th), math.sin(th)
    pts = []
    for su, sv in ((-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)):
        u, v = su * bw, sv * bh
        pts.append([cx + u * cos - v * sin, cy + u * sin + v * cos])
    return pts


# -------------------------------------------------------------------------------------------------------- движок

class Engine:
    name = 'rapid'

    def __init__(self, threads: int = 1, det_model: str | None = None, rec_model: str | None = None,
                 cls_model: str | None = None, det_side: int = 2000, rec_batch: int = 1, min_conf: float = 0.5,
                 box_thresh: float = 0.5, unclip: float = 1.6, rec_h: int = 48, det_norm: str = 'imagenet',
                 drop_color: int = 1, stretch: float = 1.0, redetect: int = 1, skew_min: float = SKEW_MIN,
                 skew_deskew: float = SKEW_DESKEW, quality: str | None = None, rec2_model: str | None = None,
                 fixes: int = 1, reread_conf: float = REREAD_CONF, reread_min_alnum: int = REREAD_MIN_ALNUM,
                 reread_min_digits: int = REREAD_MIN_DIGITS, reread_digit_share: float = REREAD_DIGIT_SHARE, **_):
        from ..types import EngineUnavailable
        ok, why = available() if not (det_model and rec_model) else (True, '')
        if not ok:
            raise EngineUnavailable(f'Движок rapid недоступен: {why}')
        self.threads = threads
        self.det = _session(_model_path(det_model or DET_MODEL), threads)
        self.rec = _session(_model_path(rec_model or REC_MODEL), threads)
        cp = _model_path(cls_model or CLS_MODEL)
        self.cls = _session(cp, threads) if os.path.isfile(cp) else None
        self.det_in = self.det.get_inputs()[0].name
        self.rec_in = self.rec.get_inputs()[0].name
        chars = self.rec.get_modelmeta().custom_metadata_map.get('character', '')
        if not chars:
            raise EngineUnavailable('В модели распознавания нет словаря знаков (метаданные character)')
        self.chars = [''] + chars.splitlines() + [' ']
        self.quality = _quality(quality, rec2_model)
        self.fixes = int(fixes)
        self.reread_conf = float(reread_conf)
        self.reread_min_alnum = int(reread_min_alnum)
        self.reread_min_digits = int(reread_min_digits)
        self.reread_digit_share = float(reread_digit_share)
        self.rec2 = None
        if self.quality == 'accurate':
            net = _session(_model_path(rec2_model or REC2_MODEL), threads)
            chars2 = net.get_modelmeta().custom_metadata_map.get('character', '')
            if chars2:
                self.rec2 = (net, net.get_inputs()[0].name, [''] + chars2.splitlines() + [' '])
            else:
                self.quality = 'fast'
        if self.cls is not None:
            ci = self.cls.get_inputs()[0]
            self.cls_in = ci.name
            hw = [d for d in ci.shape[2:] if isinstance(d, int)]
            self.cls_hw = (hw[0], hw[1]) if len(hw) == 2 else (48, 192)
        self.det_side = det_side
        self.rec_batch = max(1, rec_batch)
        self.min_conf = min_conf
        self.box_thresh = box_thresh
        self.unclip = unclip
        self.rec_h = rec_h
        self.det_norm = det_norm
        self.drop_color = int(drop_color)
        self.stretch = float(stretch)
        self.redetect = int(redetect)
        self.skew_min = float(skew_min)
        self.skew_deskew = float(skew_deskew)
        self.timing = {'det': 0.0, 'cls': 0.0, 'rec': 0.0, 'rec2': 0.0, 'crop': 0.0, 'post': 0.0}

    # -- det
    def _prob(self, img):
        h, w = img.shape[:2]
        scale = min(1.0, self.det_side / max(h, w))
        nh, nw = max(32, int(round(h * scale))), max(32, int(round(w * scale)))
        small = resize(img, nh, nw)
        ph, pw = -(-nh // 32) * 32, -(-nw // 32) * 32
        x = np.full((ph, pw, 3), 255.0, np.float32)
        x[:nh, :nw] = small
        if self.det_norm == 'imagenet':
            x = (x[:, :, ::-1] / 255.0 - _DET_MEAN) / _DET_STD      # сети обучены на BGR
        else:
            x = (x[:, :, ::-1] / 255.0 - 0.5) / 0.5
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])
        prob = self.det.run(None, {self.det_in: x})[0][0, 0]
        return prob[:nh, :nw], scale

    # -- rec
    def _read(self, crops, detail: bool = False, rec=None, batch: int | None = None):
        """crops: список картинок высотой rec_h (float32, RGB 0..255). Возвращает [(текст, уверенность)].
        detail=True: ещё вероятности знаков текста и места знаков по ширине вырезки (по одному на знак).
        rec: (сеть, имя входа, словарь) другой модели чтения; по умолчанию основная."""
        net, net_in, chars = rec or (self.rec, self.rec_in, self.chars)
        out = [None] * len(crops)
        order = sorted(range(len(crops)), key=lambda i: crops[i].shape[1])
        step = max(1, batch or self.rec_batch)
        for s in range(0, len(order), step):
            idx = order[s:s + step]
            wmax = max(crops[i].shape[1] for i in idx)
            x = np.zeros((len(idx), 3, self.rec_h, wmax), np.float32)
            for j, i in enumerate(idx):
                c = crops[i]
                x[j, :, :, :c.shape[1]] = ((c[:, :, ::-1] / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)
            pred = net.run(None, {net_in: x})[0]
            ids = pred.argmax(axis=2)
            mx = pred.max(axis=2)
            for j, i in enumerate(idx):
                seq = ids[j]
                keep = seq != 0
                keep[1:] &= seq[1:] != seq[:-1]
                text = ''.join(chars[t] for t in seq[keep])
                conf = float(mx[j][keep].mean()) if keep.any() else 0.0
                if not detail:
                    out[i] = (text, conf)
                    continue
                # вероятности и места идут по знакам словаря; знак словаря почти всегда один знак текста
                scale = wmax / float(pred.shape[1])
                probs, xs = [], []
                for t, v, k in zip(np.nonzero(keep)[0], mx[j][keep], seq[keep]):
                    for _c in chars[k]:
                        probs.append(float(v))
                        xs.append((float(t) + 0.5) * scale)
                out[i] = (text, conf, probs, xs)
        return out

    def _reread(self, word: str, probs) -> bool:
        """Нужно ли слово перечитать второй моделью (правило у констант REREAD_*)."""
        al = [c for c in word if c.isalnum()]
        dg = sum(c.isdigit() for c in al)
        if dg < max(1, self.reread_min_digits) or len(al) < self.reread_min_alnum:
            return False
        if dg < self.reread_digit_share * len(al):
            return False
        return min(probs, default=1.0) < self.reread_conf

    def _second_reading(self, crops, texts):
        """Режим «точнее»: слова-коды перечитывает вторая модель. Слово вырезается из готовой вырезки строки по
        местам знаков (так вторая модель читает только нужное и почти не добавляет времени), чтения сливаются."""
        pad = REREAD_PAD * self.rec_h
        jobs = []
        for i, (text, _conf, probs, xs) in enumerate(texts):
            w = crops[i].shape[1]
            for m in _WORD.finditer(text):
                a, b = m.span()
                if b > len(xs) or not self._reread(text[a:b], probs[a:b]):
                    continue
                x0 = int(max(0.0, xs[a] - pad))
                x1 = int(min(float(w), xs[b - 1] + pad))
                if x1 - x0 >= 8:
                    jobs.append((i, a, b, np.ascontiguousarray(crops[i][:, x0:x1])))
        if not jobs:
            return texts
        second = self._read([j[3] for j in jobs], detail=True, rec=self.rec2, batch=REC2_BATCH)
        by_line = {}
        for (i, a, b, _c), (tb, _cb, pb, _xb) in zip(jobs, second):
            by_line.setdefault(i, []).append((a, b, tb, pb))
        texts = list(texts)
        for i, reps in by_line.items():
            text, conf, probs, xs = texts[i]
            for a, b, tb, pb in sorted(reps, reverse=True):         # справа налево: места слева не сдвигаются
                keep = [k for k, c in enumerate(tb) if not c.isspace()]
                new = merge_readings(text[a:b], probs[a:b], ''.join(tb[k] for k in keep), [pb[k] for k in keep],
                                     inner_only=True)
                text = text[:a] + new + text[b:]
            texts[i] = (text, conf, probs, xs)
        return texts

    # -- cls
    def _upside_down(self, img, boxes) -> bool:
        """Перевёрнута ли страница: голосование сети cls по нескольким длинным строкам."""
        if self.cls is None or len(boxes) == 0:
            return False
        ratio = boxes[:, 2] / np.maximum(boxes[:, 3], 1)
        cand = np.nonzero((ratio >= 3) & (boxes[:, 3] >= 10))[0]
        if len(cand) == 0:
            return False
        cand = cand[np.argsort(-boxes[cand, 2])][:CLS_VOTES]
        ch, cw = self.cls_hw
        x = np.zeros((len(cand), 3, ch, cw), np.float32)
        for j, i in enumerate(cand):
            b = boxes[i].copy()
            b[2] = min(b[2], b[3] * cw / ch)        # из длинной строки берётся середина: сеть ждёт кусок 4:1
            c = crop_line(img, b, ch)[:, :cw]
            x[j, :, :, :c.shape[1]] = ((c[:, :, ::-1] / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)
        pred = self.cls.run(None, {self.cls_in: x})[0]
        self.last_cls = (float(pred[:, 0].sum()), float(pred[:, 1].sum()), len(cand))
        return float(pred[:, 1].sum()) > float(pred[:, 0].sum())

    # -- страница целиком
    def read(self, img, fix_rotation: bool = True):
        """Распознать страницу. Возвращает (строки, поворот, наклон): строки = [(quad, text, conf)] в координатах
        картинки, повёрнутой на «поворот» градусов по часовой; наклон в градусах уже учтён в углах quad."""
        import time
        t0 = time.perf_counter()
        if self.drop_color and img.ndim == 3 and img.shape[2] == 3:
            # цветные чернила (красные и синие печати поверх текста) становятся светлыми, чёрный текст остаётся:
            # серое = самый яркий из трёх каналов. Дальше картинка одноканальная (и вырезки втрое дешевле)
            img = img.max(axis=2, keepdims=True)
        prob, scale = self._prob(img)
        t1 = time.perf_counter()
        rot = 0
        if fix_rotation and lies_sideways(prob):
            rot = 90
            img = np.ascontiguousarray(np.rot90(img, k=-1))
            prob = np.ascontiguousarray(np.rot90(prob, k=-1))
        boxes, st = db_boxes(prob, box_thresh=self.box_thresh, unclip=self.unclip, skew_min=self.skew_min)
        if len(boxes):
            boxes[:, :4] /= scale
        t2 = time.perf_counter()
        if fix_rotation and self._upside_down(img, boxes):
            rot = (rot + 180) % 360
            h, w = img.shape[:2]
            img = np.ascontiguousarray(img[::-1, ::-1])
            boxes[:, 0] = w - boxes[:, 0]
            boxes[:, 1] = h - boxes[:, 1]
        t3 = time.perf_counter()
        skew = float(st['angle']) if abs(st['angle']) >= self.skew_min else 0.0
        back = None
        deskew = abs(skew) >= self.skew_deskew
        if self.redetect and (rot != 0 or deskew) and len(boxes):
            # страница была повёрнута или заметно наклонена: первая карта текста снята «не с того бока» и чуть
            # хуже. Ставим страницу прямо и ищем строки заново (ещё один проход det только для таких страниц)
            if deskew:
                h, w = img.shape[:2]
                img = rotate_small(img, skew)
                back = (math.cos(math.radians(skew)), math.sin(math.radians(skew)), (w - 1) / 2.0, (h - 1) / 2.0)
            prob, scale = self._prob(img)
            boxes, _st2 = db_boxes(prob, box_thresh=self.box_thresh, unclip=self.unclip,
                                   angle=0.0 if deskew else None, skew_min=self.skew_min)
            if len(boxes):
                boxes[:, :4] /= scale
        t3b = time.perf_counter()
        crops = [crop_line(img, b, self.rec_h, self.stretch) for b in boxes]
        if getattr(self, 'keep_last', False):
            self.last = {'img': img, 'boxes': boxes, 'crops': crops, 'rot': rot, 'back': back}
        t4 = time.perf_counter()
        texts = self._read(crops, detail=True) if crops else []
        t5 = time.perf_counter()
        if self.rec2 is not None and texts:
            texts = self._second_reading(crops, texts)
        if self.fixes:
            from ..fixes import fix_line
            texts = [(fix_line(t[0]),) + tuple(t[1:]) for t in texts]
        t6 = time.perf_counter()
        items = []
        for (cx, cy, bw, bh, _s, th), (text, conf, _p, _x) in zip(boxes, texts):
            text = text.strip()
            if text and conf >= self.min_conf:
                quad = _quad(float(cx), float(cy), float(bw), float(bh), float(th))
                if back is not None:        # из выровненной картинки обратно в наклонённую
                    cos, sin, ox, oy = back
                    quad = [[ox + (x - ox) * cos - (y - oy) * sin, oy + (x - ox) * sin + (y - oy) * cos]
                            for x, y in quad]
                items.append((quad, text, conf))
        for k, v in (('det', t1 - t0 + t3b - t3), ('post', t2 - t1), ('cls', t3 - t2), ('crop', t4 - t3b),
                     ('rec', t5 - t4), ('rec2', t6 - t5)):
            self.timing[k] += v
        return items, rot, float(st['angle'])

    def recognize(self, img):
        """Без поиска поворота: строки [(quad, text, conf)] в координатах данной картинки."""
        return self.read(img, fix_rotation=False)[0]
