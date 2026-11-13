"""Распознавание PDF: страницы с текстовым слоем берутся как есть, сканы распознаются параллельно.

Схема работы (обоснование замерами: docs/LOGIC_ocr.md):
- вызывающий поток рендерит страницы по одной (PyMuPDF не рассчитан на несколько потоков) и раздаёт их рабочим;
- рабочие потоки распознают, у каждого свой движок с одним вычислительным потоком: так ядра заняты страницами,
  а не борьбой потоков внутри одной страницы;
- одновременно в памяти не больше 2 x workers отрендеренных страниц («скользящее окно», а не жёсткие пачки:
  освободившийся рабочий сразу берёт следующую страницу);
- результат собирается по номерам страниц, порядок выдачи от порядка готовности не зависит;
- число рабочих ограничено и ядрами, и свободной памятью: при нехватке памяти работа идёт в один поток.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait

from . import engines as _engines
from . import prep
from .cache import Cache
from .types import Cancelled, Line, OcrError, PageText

ALGO = 4        # номер версии обработки: входит в ключ кэша, растёт при изменении подготовки страницы
MAX_WORKERS = 6
# Память (замеры: docs/LOGIC_ocr.md): сколько занимает один работник в пике и сколько оставлять системе.
# Замер для rapid на странице A4 200 dpi: первый работник 520 МБ сверх 60 МБ самого Python, каждый следующий
# около 350 МБ (пик приходится на поиск строк: картинка 2000 x 1400 в числах с плавающей точкой и слои сети).
# Работников берётся столько, чтобы они заняли не больше MEMORY_SHARE свободной памяти после вычета запаса.
MEMORY_PER_WORKER_MB = {'rapid': 450, 'windows': 150, 'tesseract': 250}
MEMORY_RESERVE_MB = 700
MEMORY_SHARE = 0.6
KEEP_WARM = 1       # сколько загруженных движков держать между вызовами (загрузка моделей занимает доли секунды)


def available_memory_mb():
    """Свободная оперативная память в мегабайтах (сколько можно занять без ухода в файл подкачки) либо None,
    если узнать не удалось. Без сторонних библиотек: Windows через GlobalMemoryStatusEx, Linux через
    /proc/meminfo."""
    try:
        if os.name == 'nt':
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
            st = Status()
            st.dwLength = ctypes.sizeof(Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullAvailPhys / 1048576.0
            return None
        with open('/proc/meminfo', encoding='ascii') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def workers_by_memory(engine: str = 'rapid', available_mb: float | None = None,
                      memory_limit_mb: float | None = None) -> int:
    """Сколько работников помещается в память. available_mb: свободная память (None = спросить у системы);
    memory_limit_mb: свой предел памяти на распознавание (None = доля MEMORY_SHARE от свободной за вычетом запаса
    MEMORY_RESERVE_MB; переменная окружения OCRKIT_MEMORY_MB задаёт предел для всей программы).
    Меньше одного не бывает: при нехватке памяти работа идёт в один поток, а не падает."""
    per = MEMORY_PER_WORKER_MB.get(engine, 350)
    if memory_limit_mb is None and os.environ.get('OCRKIT_MEMORY_MB'):
        try:
            memory_limit_mb = float(os.environ['OCRKIT_MEMORY_MB'])
        except ValueError:
            memory_limit_mb = None
    if available_mb is None:
        available_mb = available_memory_mb()
    budget = None
    if available_mb is not None:
        budget = max(0.0, available_mb - MEMORY_RESERVE_MB) * MEMORY_SHARE
    if memory_limit_mb is not None:
        budget = memory_limit_mb if budget is None else min(budget, memory_limit_mb)
    if budget is None:
        return MAX_WORKERS
    return max(1, int(budget // per))


def default_workers(engine: str = 'rapid', available_mb: float | None = None,
                    memory_limit_mb: float | None = None) -> int:
    """Сколько страниц распознавать одновременно: число физических ядер, не больше 6 и не больше, чем помещается
    в свободную память (workers_by_memory). Замеры: docs/LOGIC_ocr.md."""
    return max(1, min(workers_by_cpu(), workers_by_memory(engine, available_mb, memory_limit_mb)))


def physical_cores() -> int:
    """Число физических ядер. В Windows спрашивается у системы; иначе половина логических (если их больше
    четырёх) либо сами логические."""
    n = os.cpu_count() or 2
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes

            class Info(ctypes.Structure):
                _fields_ = [('ProcessorMask', ctypes.c_size_t), ('Relationship', wintypes.DWORD),
                            ('Reserved', ctypes.c_ulonglong * 2)]
            size = wintypes.DWORD(0)
            fn = ctypes.windll.kernel32.GetLogicalProcessorInformation
            fn(None, ctypes.byref(size))
            count = size.value // ctypes.sizeof(Info)
            if count:
                buf = (Info * count)()
                if fn(buf, ctypes.byref(size)):
                    cores = sum(1 for x in buf if x.Relationship == 0)      # 0 = RelationProcessorCore
                    if cores:
                        return cores
    except Exception:
        pass
    return n // 2 if n > 4 else n


def workers_by_cpu() -> int:
    """Число работников по процессору: по одному на физическое ядро, не больше MAX_WORKERS (по замеру седьмой
    и восьмой работник на шести ядрах скорости не добавляют)."""
    return max(1, min(physical_cores(), MAX_WORKERS))


# ------------------------------------------------------------------------------------------- движки про запас

class _EnginePool:
    """Готовые движки одного вида: создаются по мере надобности, между вызовами не выбрасываются (модели
    грузятся один раз)."""

    def __init__(self, name: str, options: dict):
        self.name, self.options = name, options
        self._free = queue.LifoQueue()
        self._lock = threading.Lock()
        self.count = 0

    def acquire(self):
        try:
            return self._free.get_nowait()
        except queue.Empty:
            pass
        eng = _engines.create(self.name, threads=1, **self.options)
        with self._lock:
            self.count += 1
        return eng

    def release(self, eng):
        self._free.put(eng)

    def trim(self, keep: int = 1):
        """Оставить про запас не больше keep свободных движков: остальные отдают память сразу."""
        while self._free.qsize() > keep:
            try:
                self._free.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self.count -= 1


_POOLS: dict = {}
_POOLS_LOCK = threading.Lock()


def _pool(name: str, options: dict) -> _EnginePool:
    key = (name, json.dumps(options, sort_keys=True, default=str))
    with _POOLS_LOCK:
        p = _POOLS.get(key)
        if p is None:
            p = _POOLS[key] = _EnginePool(name, options)
        return p


def release_engines() -> None:
    """Освободить память: выбросить загруженные движки (и остановить рабочие процессы, если они запускались).
    Следующий вызов распознавания загрузит модели заново."""
    global _PROC_POOL, _PROC_KEY
    with _POOLS_LOCK:
        _POOLS.clear()
    if _PROC_POOL is not None:
        _PROC_POOL.shutdown(wait=False, cancel_futures=True)
        _PROC_POOL, _PROC_KEY = None, None


# -------------------------------------------------------------------------------------------- сборка страницы

def _page_text(pno, items, deg, img_w, img_h, zoom, frame, source, seconds, skew=0.0) -> PageText:
    """Собрать страницу: порядок чтения считается в картинке, где текст стоит прямо; координаты строк
    переводятся обратно на страницу PDF. frame = prep.page_frame(page)."""
    origin, derot, page_w, page_h, page_rot = frame
    order = prep.reading_order(items, skew)
    lines, rows = [], []
    for row in order:
        parts = []
        for i in row:
            quad, text, conf = items[i]
            text = ' '.join(text.split())       # внутри куска нет табуляций и переводов строк: ими делится text
            bbox, q = prep.to_page(quad, deg, img_w, img_h, zoom, origin, derot)
            lines.append(Line(text, bbox, conf, q))
            parts.append(text)
        rows.append('\t'.join(parts))
    return PageText(page=pno, text='\n'.join(rows), lines=lines, source=source, rotation=deg, skew=skew,
                    width=page_w, height=page_h, seconds=seconds, page_rotation=page_rot)


def layer_page(page, pno: int, clip=None) -> PageText:
    """Страница с текстовым слоем: строки слоя с их прямоугольниками, без распознавания. clip: вырезка в долях
    видимой страницы: берутся строки, середина которых попадает в неё."""
    items = []
    area = None
    if clip:
        area = prep.clip_rect(page, clip) * page.derotation_matrix
        area.normalize()
    d = page.get_text('dict', clip=area) if area is not None else page.get_text('dict')
    for block in d.get('blocks', []):
        for ln in block.get('lines', []):
            text = ''.join(s['text'] for s in ln.get('spans', [])).strip()
            if not text:
                continue
            x0, y0, x1, y1 = ln['bbox']
            if area is not None and not area.contains(((x0 + x1) / 2, (y0 + y1) / 2)):
                continue
            items.append(([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], text, 1.0))
    _o, _d, pw, ph, prot = prep.page_frame(page)
    return _page_text(pno, items, 0, 0, 0, 1.0, ((0.0, 0.0), None, pw, ph, prot), 'layer', 0.0)


def _read_image(eng, name, pno, img, zoom, frame) -> PageText:
    t0 = time.perf_counter()
    items, deg, skew = eng.read(img)
    h, w = img.shape[:2]
    return _page_text(pno, items, deg, w, h, zoom, frame, name, time.perf_counter() - t0, skew)


def recognize_oriented(engine, img):
    """Распознать картинку готовым движком (объект из engines.create) с подбором поворота.
    Возвращает (строки [(quad, text, conf)] в картинке, повёрнутой так, что текст стоит прямо; градусы поворота).
    Оставлено для вызывающих, которые сами рендерят часть страницы; новым проще recognize_pdf(..., clip=...)."""
    items, deg, _skew = engine.read(img)
    return items, deg


def recognize_image(img, engine: str = 'auto', rotate: bool = True, **options) -> PageText:
    """Распознать одну картинку: numpy (H x W x 3 RGB либо H x W серая), путь к файлу картинки или PIL.Image.
    Координаты строк в пикселях исходной картинки."""
    import numpy as np
    name = _engines.pick(engine)
    if isinstance(img, (str, os.PathLike)):
        import pymupdf
        try:
            pix = pymupdf.Pixmap(os.fspath(img))
            if pix.alpha or pix.n != 3:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix, 0) if pix.n - pix.alpha != 3 else pymupdf.Pixmap(pix, 0)
        except Exception as e:
            raise OcrError(f'Не удалось прочитать картинку {os.path.basename(os.fspath(img))}: {e}') from e
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
    if not isinstance(img, np.ndarray):
        img = np.asarray(img.convert('RGB'))
    if img.ndim == 2:
        img = np.stack([img] * 3, axis=-1)
    img = np.ascontiguousarray(img[:, :, :3])
    pool = _pool(name, options)
    eng = pool.acquire()
    try:
        t0 = time.perf_counter()
        items, deg, skew = eng.read(img, fix_rotation=rotate)
    finally:
        pool.release(eng)
    h, w = img.shape[:2]
    return _page_text(0, items, deg, w, h, 1.0, ((0.0, 0.0), None, w, h, 0), name, time.perf_counter() - t0, skew)


# -------------------------------------------------------------------------------------------------- PDF целиком

def recognize_pdf(path, pages=None, engine: str = 'auto', workers: int | None = None, cache_dir: str | None = None,
                  only_scans: bool = True, progress=None, cancel=None, dpi: int | None = None,
                  mode: str = 'threads', clip=None, plan=None, memory_limit_mb: float | None = None,
                  **options) -> dict:
    """Текст страниц PDF: {номер страницы с нуля: PageText}.

    pages: None = все страницы, иначе номера с нуля; engine: 'auto' или имя движка (см. engines());
    workers: сколько страниц распознавать одновременно (None = по числу ядер); cache_dir: папка кэша (None = без
    кэша); only_scans: страницы с текстовым слоем не распознавать, вернуть их текст как есть; progress(done, total):
    вызывается в потоке вызывающего по мере готовности страниц; cancel() -> bool: вернуть True, чтобы прервать
    (тогда поднимается Cancelled, в его поле pages уже готовые страницы; готовое остаётся в кэше);
    dpi: разрешение рендера (None = родное разрешение скана в пределах 150..300);
    mode: 'threads' (по умолчанию) или 'processes' (отдельные процессы; в собранной программе нужен
    multiprocessing.freeze_support());
    clip: распознать только часть страницы: (x0, y0, x1, y1) в долях страницы в её положении на экране, например
    (0, 0.15, 1, 0.52) = полоса от 15 до 52 % высоты; координаты строк остаются координатами страницы; вырезка
    входит в ключ кэша; поворот определяется уже по вырезке;
    plan(сведения): вызывается один раз перед распознаванием со словарём {'pages': всего страниц в заказе,
    'layer': с текстовым слоем, 'cached': из кэша, 'recognize': будет распознано сейчас, 'workers': во сколько
    потоков};
    memory_limit_mb: сколько памяти можно занять распознаванием (None = по свободной памяти компьютера с
    запасом; 0 = не ограничивать). Число работников не превышает того, что помещается в память, даже если
    workers задано больше: при нехватке работа идёт в один поток, а не уходит в файл подкачки;
    options: настройки движка (например lang='ru' для windows).
    """
    import pymupdf
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise OcrError(f'Файл не найден: {path}')
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise OcrError(f'Не удалось открыть PDF {os.path.basename(path)}: {e}') from e
    try:
        if doc.needs_pass:
            raise OcrError(f'PDF {os.path.basename(path)} закрыт паролем')
        n = len(doc)
        todo = list(range(n)) if pages is None else [int(p) for p in pages]
        bad = [p for p in todo if not 0 <= p < n]
        if bad:
            raise OcrError(f'В файле {os.path.basename(path)} {n} стр., нет страниц с номерами {bad}')
        if clip is not None:
            try:
                clip = tuple(round(min(1.0, max(0.0, float(v))), 4) for v in clip)
                ok = len(clip) == 4 and clip[2] > clip[0] and clip[3] > clip[1]
            except (TypeError, ValueError):
                ok = False
            if not ok:
                raise OcrError(f'Неверная вырезка страницы clip={clip!r}: нужны четыре доли (x0, y0, x1, y1), '
                               'x1 больше x0 и y1 больше y0')
            if clip == (0.0, 0.0, 1.0, 1.0):
                clip = None
        total = len(set(todo))
        out: dict = {}
        done = 0

        def step():
            nonlocal done
            done += 1
            if progress:
                progress(done, total)

        scans = []
        for pno in dict.fromkeys(todo):
            page = doc[pno]
            if only_scans and prep.has_text_layer(page):
                out[pno] = layer_page(page, pno, clip)
            else:
                scans.append(pno)
        n_layer = len(out)
        name, cache = None, None
        if scans:
            name = _engines.pick(engine)
            if cache_dir:
                # режим чтения уже есть в подписи движка: вызов без режима и вызов с режимом по умолчанию
                # должны попадать в один кэш
                key = {'algo': ALGO, 'sig': _engines.signature(name, **options), 'dpi': dpi or 0,
                       'opt': {k: v for k, v in options.items() if k != 'quality'}}
                if clip:
                    key['clip'] = list(clip)
                cache = Cache(cache_dir, path, name, key)
                left = []
                for pno in scans:
                    pt = cache.get(pno)
                    if pt is None:
                        left.append(pno)
                    else:
                        out[pno] = pt
                scans = left
        nw = 0
        if scans:
            nw = workers or workers_by_cpu()
            if memory_limit_mb != 0:        # память важнее просьбы о числе потоков; 0 = не ограничивать
                nw = min(nw, workers_by_memory(name, memory_limit_mb=memory_limit_mb))
            nw = max(1, min(nw, len(scans)))
        if plan:
            plan({'pages': total, 'layer': n_layer, 'cached': len(out) - n_layer, 'recognize': len(scans),
                  'workers': nw})
        for _ in range(len(out)):
            step()
        if scans:
            if mode == 'processes' and nw > 1:
                _run_processes(path, scans, name, nw, cache, out, step, cancel, dpi, options, clip)
            else:
                _run_threads(doc, scans, name, nw, cache, out, step, cancel, dpi, options, clip)
        return {p: out[p] for p in dict.fromkeys(todo) if p in out}
    finally:
        doc.close()


def _drain(pending, out, cache, step, block):
    """Забрать готовые задания. Возвращает оставшиеся."""
    if not pending:
        return pending
    ready, rest = wait(pending, timeout=None if block else 0, return_when=FIRST_COMPLETED)
    for f in ready:
        pt = f.result()
        if isinstance(pt, dict):
            sec = pt.get('seconds', 0.0)
            pt = PageText.from_json(pt)
            pt.seconds = sec
        out[pt.page] = pt
        if cache:
            cache.put(pt)
        step()
    return rest


def _feed(scans, nw, cancel, submit, out, cache, step):
    """Общий цикл раздачи страниц: не больше 2 x nw заданий в работе, отмена между страницами."""
    pending = set()
    cancelled = False
    try:
        for pno in scans:
            while len(pending) >= 2 * nw and not cancelled:
                pending = _drain(pending, out, cache, step, block=True)
                cancelled = bool(cancel and cancel())
            if cancelled or (cancel and cancel()):
                cancelled = True
                break
            pending.add(submit(pno))
            pending = _drain(pending, out, cache, step, block=False)
        while pending and not cancelled:
            pending = _drain(pending, out, cache, step, block=True)
            if pending and cancel and cancel():
                cancelled = True
    finally:
        if cancelled:
            for f in pending:
                f.cancel()
            running = {f for f in pending if not f.cancelled()}
            if running:
                wait(running)       # начатые страницы дорабатывают (и попадают в кэш), новые не начинаются
                for f in running:
                    if f.exception() is None:
                        _drain({f}, out, cache, lambda: None, block=False)
    if cancelled:
        e = Cancelled('Распознавание прервано')
        e.pages = dict(out)
        raise e


def _run_threads(doc, scans, name, nw, cache, out, step, cancel, dpi, options, clip=None):
    pool = _pool(name, options)

    def job(pno, img, zoom, frame):
        eng = pool.acquire()
        try:
            return _read_image(eng, name, pno, img, zoom, frame)
        finally:
            pool.release(eng)

    try:
        with ThreadPoolExecutor(max_workers=nw, thread_name_prefix='ocrkit') as ex:
            def submit(pno):
                page = doc[pno]
                img, zoom = prep.render(page, dpi, clip)
                return ex.submit(job, pno, img, zoom, prep.page_frame(page, clip))
            _feed(scans, nw, cancel, submit, out, cache, step)
    finally:
        pool.trim(KEEP_WARM)        # между вызовами остаётся один загруженный движок, остальная память свободна


# ---------------------------------------------------------------------------------------------- режим процессов

_PROC_POOL = None
_PROC_KEY = None
_W = {}


def _proc_init(name, options):
    _W['name'] = name
    _W['eng'] = _engines.create(name, threads=1, **options)
    _W['docs'] = {}


def _proc_job(path, pno, dpi, clip=None):
    import pymupdf
    doc = _W['docs'].get(path)
    if doc is None:
        for d in _W['docs'].values():
            d.close()
        _W['docs'].clear()
        doc = _W['docs'][path] = pymupdf.open(path)
    page = doc[pno]
    img, zoom = prep.render(page, dpi, clip)
    pt = _read_image(_W['eng'], _W['name'], pno, img, zoom, prep.page_frame(page, clip))
    return pt.to_json()


def _run_processes(path, scans, name, nw, cache, out, step, cancel, dpi, options, clip=None):
    global _PROC_POOL, _PROC_KEY
    key = (name, json.dumps(options, sort_keys=True, default=str), nw)
    if _PROC_POOL is None or _PROC_KEY != key:
        if _PROC_POOL is not None:
            _PROC_POOL.shutdown(wait=False, cancel_futures=True)
        _PROC_POOL = ProcessPoolExecutor(max_workers=nw, initializer=_proc_init, initargs=(name, options))
        _PROC_KEY = key
    ex = _PROC_POOL
    _feed(scans, nw, cancel, lambda pno: ex.submit(_proc_job, path, pno, dpi, clip), out, cache, step)
