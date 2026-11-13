# ocrkit: распознавание текста на сканах PDF

Самостоятельный инструмент: превращает сканы в PDF (и картинки) в текст с координатами. Работает на компьютере
пользователя, без сети и без прав администратора. Сделан для программы PackDoc, но от неё не зависит: папку
`ocrkit` можно скопировать в другой продукт как есть.

© NORREXIM DMCC. Инструмент распространяется на условиях GNU Affero General Public License версии 3 (он использует
библиотеку PyMuPDF под той же лицензией): полный текст в файле `LICENSE`. Модели распознавания распространяются на
условиях Apache-2.0: происхождение и текст лицензии в `ocrkit/models/NOTICE.md` и `ocrkit/models/LICENSE-Apache-2.0.txt`.

Что лежит рядом:

| Путь | Что это |
|---|---|
| `ocrkit/` | пакет с моделями (`ocrkit/models`, около 18 МБ) |
| `requirements.txt` | зависимости |
| `tests/test_ocrkit.py` | проверка пакета на искусственных страницах: их рисует сам тест |
| `tools/ocr_bench.py` | замеры на своих документах: точность и скорость |
| `tools/ocr_examples.py` | примеры распознавания на выдуманных документах |
| `tools/ocr_charts.py` | графики по файлу чисел `docs/recognition/measurements.json` |
| `docs/recognition/` | что сравнивали, как мерили, что выбрано и почему: описание, графики, примеры |

В опубликованной папке на GitHub материалы из `docs/recognition` лежат уровнем выше кода; в архиве выпуска
`PackDoc_Recognition_Source.zip` всё лежит так, как описано здесь, и скрипты из `tools` находят свои файлы сами.
Если в папке на GitHub нет какого-то файла модели (`ocrkit/models/*.onnx`), возьмите его из архива выпуска:
в архиве модели есть всегда.

## Что умеет

- `recognize_pdf`: текст страниц PDF. Страницы с текстовым слоем не распознаются (берётся слой), сканы
  распознаются параллельно, результат по порядку страниц; ход выполнения, отмена, кэш на диске.
- Сам находит поворот страницы (0, 90, 180, 270 градусов) и небольшой наклон.
- `recognize_image`: то же для картинки (numpy, файл PNG или JPEG).
- `find_codes`: поиск заранее известных кодов (коды изделий, номера плавок) с допуском на типовые подмены
  знаков 0/O, 1/I/L, 5/S, 8/B, 2/Z и на одну настоящую ошибку.
- `make_searchable_pdf`: копия PDF с невидимым текстовым слоем поверх сканов (в файле начинают работать поиск
  и выделение текста). Картинки не пережимаются.
- Запуск из командной строки.
- Три движка на выбор: `rapid` (основной, модели PP-OCRv6 на ONNX Runtime), `windows` (встроенное
  распознавание Windows), `tesseract`. `engine='auto'` берёт первый доступный в этом порядке.

Язык: латиница, цифры, китайский. Кириллицу основной движок не знает (русский текст выходит похожими
латинскими буквами): для кодов и номеров это не мешает. Рукописный текст не читается.

## Установка

Нужен Python 3.12 (проверено на Windows 11).

```
pip install -r requirements.txt
```

Нужны только `pymupdf`, `numpy`, `onnxruntime`. Модели уже лежат в `ocrkit/models`, при работе ничего не
скачивается. Необязательные движки:

- `windows`: `pip install winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Globalization
  winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Foundation
  winrt-Windows.Foundation.Collections`; в Windows должен стоять язык распознавания (английский или любой
  другой с латиницей: Параметры, Время и язык, Язык).
- `tesseract`: `pip install tesserocr` (в колесе уже есть библиотека Tesseract 5, отдельная установка не
  нужна) и файл `eng.traineddata` из https://github.com/tesseract-ocr/tessdata_fast в папку
  `ocrkit/tessdata` либо в папку из переменной окружения `OCRKIT_TESSDATA`.

Проверка: `python -m ocrkit --engines` покажет, какие движки доступны и почему недоступны остальные.

## Вызов из кода

```python
from ocrkit import recognize_pdf, find_codes, make_searchable_pdf

pages = recognize_pdf(
    'сертификаты.pdf',
    pages=None,            # None = все; иначе номера страниц с нуля
    engine='auto',         # 'auto' | 'rapid' | 'windows' | 'tesseract'
    workers=None,          # сколько страниц распознавать одновременно; None = по числу ядер
    cache_dir='кэш',       # папка кэша; None = без кэша. С кэшем повторный вызов мгновенный
    only_scans=True,       # страницы с текстовым слоем не распознавать, вернуть их текст
    progress=lambda done, total: print(done, total),
    cancel=lambda: False,  # вернуть True, чтобы прервать: поднимется ocrkit.Cancelled (в .pages готовое)
    dpi=None,              # разрешение рендера; None = родное разрешение скана (150..300)
    clip=None,             # часть страницы: (x0, y0, x1, y1) в долях страницы «как на экране»
    plan=None,             # функция(dict): сколько страниц с текстом, из кэша, будет распознано, во сколько потоков
    memory_limit_mb=None,  # предел памяти на распознавание; None = по свободной памяти, 0 = не ограничивать
)
for no, page in pages.items():
    print(no, page.source, page.rotation)     # 'layer' или имя движка; на сколько градусов страницу повернули
    print(page.text)                          # строки сверху вниз через \n, ячейки одной строки через \t
    for line in page.lines:                   # куски текста с рамками
        print(line.text, line.bbox, line.conf)

hits = find_codes(pages[0], ['EXF0041207', 'H7204518'], max_errors=1)     # коды здесь выдуманы
for h in hits:
    print(h.code, h.found, h.kind, h.bbox)    # kind: exact | lookalike | fuzzy

make_searchable_pdf('сертификаты.pdf', 'сертификаты с текстом.pdf', cache_dir='кэш')
```

Координаты (`Line.bbox`, `Line.quad`, `CodeHit.bbox`): точки PDF от левого верхнего угла, в той же системе,
что `page.get_text()` и `page.search_for()` в PyMuPDF (свойство страницы /Rotate не учитывается). Их можно
прямо передавать в `page.insert_text`, `page.draw_rect`, `page.add_highlight_annot`.

Режимы чтения движка `rapid`. `recognize_pdf(..., quality='accurate')` («точнее», по умолчанию): слова с цифрами
перечитывает вторая модель (PP-OCRv4), два чтения сливаются по знакам. `quality='fast'` («быстро»): одна модель.
Числа по обоим режимам: `docs/recognition/README.md`. Если файла второй модели в `ocrkit/models` нет, режим
«точнее» работает как «быстро». У каждого режима свой кэш.

Память. Число работников выбирается не только по ядрам, но и по свободной памяти: при нехватке распознавание
идёт в один поток (а не падает и не уходит в файл подкачки). Свой предел: `recognize_pdf(..., memory_limit_mb=1500)`
либо переменная окружения `OCRKIT_MEMORY_MB`; `memory_limit_mb=0` снимает ограничение. Сколько получится на этой
машине: `ocrkit.default_workers()`. Освободить загруженные движки: `ocrkit.release_engines()`.

Свой цикл (например, распознавать собранные из кусков картинки) строится на готовом движке:
`from ocrkit.engines import create, pick` и затем `eng = create(pick('auto'), threads=1)`;
`eng.read(img)` возвращает строки, поворот и наклон, `eng.recognize(img)` только строки без поиска поворота.
Имя `ocrkit.engines` в самом пакете занято функцией `engines()` (список доступных движков), поэтому
подпакет движков импортируется только так: `from ocrkit.engines import ...`.

## Командная строка

```
python -m ocrkit <pdf или папка> [--out папка] [--engine auto|rapid|windows|tesseract] [--workers N]
       [--searchable] [--json] [--pages 1-5,8] [--cache папка | --no-cache] [--all-pages] [--dpi N]
python -m ocrkit --engines
```

По умолчанию рядом с PDF создаётся папка `ocr_out`: `<имя>.txt` (текст по страницам); с `--json` ещё
`<имя>.json` (строки с координатами); с `--searchable` ещё `<имя>.pdf` с невидимым текстовым слоем. Номера
страниц в командной строке считаются с единицы.

## Как встроить в сборку PyInstaller

1. Скопировать папку `ocrkit` в свой пакет (или рядом) и импортировать её.
2. Положить модели данными рядом с пакетом, сохранив путь: `--add-data "путь/ocrkit/models;ocrkit/models"`.
3. Не исключать `numpy` (нужен onnxruntime). Исключить лишнее, что стоит на машине разработчика и чего в
   сборке быть не должно: `--exclude-module cv2 --exclude-module tesserocr --exclude-module onnxruntime.tools
   --exclude-module onnxruntime.transformers --exclude-module onnxruntime.quantization`.
4. Для запасного движка Windows добавить `--hidden-import winrt.windows.media.ocr` и соседние модули
   (`winrt.windows.globalization`, `winrt.windows.graphics.imaging`, `winrt.windows.storage.streams`,
   `winrt.windows.foundation`, `winrt.windows.foundation.collections`).
5. Проверить из собранной программы: распознать страницу при запрещённой сети.

Если модели лежат не рядом с пакетом, папку задаёт переменная окружения `OCRKIT_MODELS`.

## Кэш

Ключ страницы: SHA-1 содержимого файла + номер страницы + движок + подпись движка (имена и размеры файлов
моделей либо язык: `ocrkit.engine_signature(имя)`) + параметры (разрешение, вырезка, настройки движка) + номер
версии обработки (`ocrkit.core.ALGO`). Тот же файл под другим именем попадает в тот же кэш; изменённый файл,
другая модель или новая версия обработки дают другой ключ, старые тексты не подхватываются.
Формат файла описан в `ocrkit/cache.py`. Чистка: `ocrkit.clean_cache(папка, max_age_days=30, max_mb=None)`.

## Проверка и замеры

```
python tests/test_ocrkit.py                 # искусственные страницы: документы для проверки не нужны
python tools/ocr_examples.py <папка>        # примеры на выдуманных документах
python tools/ocr_bench.py engines
python tools/ocr_bench.py truth <папка с PDF> --engine rapid      # точность на страницах с текстовым слоем
python tools/ocr_bench.py speed <папка с PDF> --workers 1,2,4,6   # скорость на сканах
python tools/ocr_charts.py                  # графики (нужен matplotlib)
```

## Ограничения

- Кириллица основным движком не читается (нужна: взять модель `eslav_PP-OCRv5_rec_mobile.onnx` из набора
  RapidOCR и передать её как `rec_model`, либо движок `windows` с русским языком).
- Рукописный текст (подписи, даты от руки) не читается ни одним из движков.
- Текст, целиком закрытый печатью, не восстановить; цветную (красную, синюю) печать поверх чёрного текста
  движок убирает сам.
- Очень мелкий текст (ниже 6 пунктов при 200 dpi) читается с ошибками: помогает скан в 300 dpi.
