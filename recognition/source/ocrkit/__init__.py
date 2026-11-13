"""ocrkit: оцифровка и распознавание PDF-сканов (текст с координатами), локально и без сети.

Пакет самодостаточен: ничего не берёт из остального продукта, его можно скопировать в другой проект как есть
(нужны PyMuPDF, numpy и onnxruntime; подробности в README переносимой копии).

    from packdoc.ocrkit import recognize_pdf, find_codes     # в другом продукте: from ocrkit import ...
    pages = recognize_pdf('скан.pdf', cache_dir='кэш')
    for no, page in pages.items():
        print(no, page.source, page.rotation, page.text[:80])
        for hit in find_codes(page, ['EXF0041207']):
            print(hit.code, hit.kind, hit.bbox)
"""
from .cache import clean_cache
from .codes import CodeHit, find_codes, normalize_code, same_code, skeleton
from .core import (available_memory_mb, default_workers, recognize_image, recognize_pdf, release_engines,
                   workers_by_memory)
from .engines import engines
from .engines import signature as engine_signature
from .searchable import add_text_layer, make_searchable_pdf
from .types import Cancelled, EngineUnavailable, Line, OcrError, PageText

__version__ = '1.1.0'

__all__ = ['recognize_pdf', 'recognize_image', 'engines', 'engine_signature', 'PageText', 'Line', 'make_searchable_pdf',
           'add_text_layer', 'find_codes', 'normalize_code', 'same_code', 'skeleton', 'CodeHit', 'clean_cache',
           'default_workers', 'workers_by_memory', 'available_memory_mb', 'release_engines', 'OcrError',
           'EngineUnavailable', 'Cancelled', '__version__']
