"""Примеры распознавания на выдуманных документах: python tools/ocr_examples.py [папка]

Рисует страницу в духе сертификата качества с вымышленными названиями, номерами и кодами, делает из неё «сканы»
(чистый, с цветной печатью поверх текста, боком, с наклоном, бледный со сжатием, полоса бланка), распознаёт пакетом
packdoc.ocrkit и кладёт в папку (по умолчанию docs/recognition/examples): уменьшенную картинку страницы, прочитанный
текст, найденные коды и время. Данных заказчика здесь нет: всё, что на страницах, придумано в этом файле.
Нужны PyMuPDF и numpy (те же, что у пакета)."""
import json
import os
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import pymupdf  # noqa: E402

try:
    from packdoc import ocrkit  # noqa: E402
except ImportError:                 # опубликованная копия: пакет ocrkit лежит рядом
    import ocrkit  # noqa: E402

DPI = 200
RED = (0.80, 0.10, 0.12)
BLUE = (0.10, 0.20, 0.70)

# Вымышленные данные документа
MAKER = 'EXAMPLE STEEL WORKS CO., LTD.'
ADDRESS = 'No. 1 Sample Road, Demo Industrial Park'
CERT_NO = 'EX-QC-250731'
PO_NO = 'PO-EX-004512'
SPEC_NO = 'EXS-0907-R2'
ROWS = [
    ('1', 'EXF0041207', 'FLANGE WN RF ASME B16.5 CL150', 'DN50 SCH40', 'H7204518', '24'),
    ('2', 'EXF0041215', 'FLANGE WN RF ASME B16.5 CL300', 'DN80 SCH40', 'H7204518', '12'),
    ('3', 'EXF0041388', 'FLANGE BL RF ASME B16.5 CL150', 'DN100', 'H7204631', '8'),
    ('4', 'EXT0090166', 'TEE EQUAL ASME B16.9 BW', 'DN50 SCH80', 'K5510827', '16'),
    ('5', 'EXT0090174', 'TEE REDUCING ASME B16.9 BW', 'DN80X50 SCH40', 'K5510827', '6'),
    ('6', 'EXB0100652', 'ELBOW 90 LR ASME B16.9 BW', 'DN150 SCH40', 'K5510902', '10'),
    ('7', 'EXB0100660', 'ELBOW 45 LR ASME B16.9 BW', 'DN150 SCH40', 'K5510902', '4'),
    ('8', 'EXR0207731', 'REDUCER CONC ASME B16.9 BW', 'DN100X80 SCH40', 'M3308145', '9'),
]
CHEM = [('H7204518', '0.18', '0.24', '1.05', '0.012', '0.008'),
        ('H7204631', '0.19', '0.22', '0.98', '0.014', '0.006'),
        ('K5510827', '0.17', '0.26', '1.12', '0.011', '0.009'),
        ('K5510902', '0.20', '0.21', '1.01', '0.013', '0.007'),
        ('M3308145', '0.16', '0.25', '1.08', '0.010', '0.005')]
CODES = [r[1] for r in ROWS] + sorted({r[4] for r in ROWS}) + [CERT_NO, PO_NO, SPEC_NO]


def cyr_font():
    """Шрифт с кириллицей для одной русской строки (если в системе есть Arial)."""
    p = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'arial.ttf')
    return p if os.path.isfile(p) else None


def draw(stamp: bool = False):
    """Страница A4 с вымышленным сертификатом. stamp: красная и синяя печати поверх текста."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)

    def text(x, y, s, size=9, bold=False, color=(0, 0, 0)):
        page.insert_text((x, y), s, fontsize=size, fontname='hebo' if bold else 'helv', color=color)

    text(150, 60, MAKER, 16, True)
    text(190, 76, ADDRESS, 8)
    text(175, 104, 'MILL TEST CERTIFICATE', 15, True)
    font = cyr_font()
    if font:
        page.insert_text((182, 120), 'СЕРТИФИКАТ КАЧЕСТВА (вымышленный образец)', fontsize=9, fontname='cyr',
                         fontfile=font)
    text(200, 136, 'ACCORDING TO EN 10204/3.1', 9)
    y = 164
    for a, b in (('Certificate No.:', CERT_NO), ('Purchase Order:', PO_NO), ('Specification:', SPEC_NO),
                 ('Customer:', 'SAMPLE PIPE TRADING LLC'), ('Date of issue:', '2025.07.31')):
        text(50, y, a, 9, True)
        text(150, y, b, 9)
        y += 15
    text(330, 164, 'Material: ASTM A105 / ASTM A234 WPB', 9)
    text(330, 179, 'Heat treatment: NORMALIZED 910 C', 9)
    text(330, 194, 'Standard: ASME B16.5-2020, ASME B16.9-2024', 9)
    # таблица позиций
    cols = [50, 78, 150, 330, 425, 495, 545]
    top = 250
    page.draw_rect(pymupdf.Rect(cols[0], top, cols[-1], top + 18 * (len(ROWS) + 1)), width=0.7)
    for x in cols[1:-1]:
        page.draw_line((x, top), (x, top + 18 * (len(ROWS) + 1)), width=0.5)
    for k, h in enumerate(('Line', 'Ident code', 'Description', 'Size', 'Heat No.', 'Qty')):
        text(cols[k] + 4, top + 12, h, 8, True)
    for r, row in enumerate(ROWS):
        yy = top + 18 * (r + 1)
        page.draw_line((cols[0], yy), (cols[-1], yy), width=0.5)
        for k, v in enumerate(row):
            text(cols[k] + 4, yy + 12, v, 8)
    # химический состав
    top2 = 440
    text(50, top2 - 8, 'CHEMICAL COMPOSITION, %', 9, True)
    c2 = [50, 150, 215, 280, 345, 410, 475]
    page.draw_rect(pymupdf.Rect(c2[0], top2, c2[-1], top2 + 16 * (len(CHEM) + 1)), width=0.7)
    for x in c2[1:-1]:
        page.draw_line((x, top2), (x, top2 + 16 * (len(CHEM) + 1)), width=0.5)
    for k, h in enumerate(('Heat No.', 'C', 'Si', 'Mn', 'P', 'S')):
        text(c2[k] + 4, top2 + 11, h, 8, True)
    for r, row in enumerate(CHEM):
        yy = top2 + 16 * (r + 1)
        page.draw_line((c2[0], yy), (c2[-1], yy), width=0.5)
        for k, v in enumerate(row):
            text(c2[k] + 4, yy + 11, v, 8)
    text(50, 570, 'We hereby certify that the material described above has been manufactured, tested and', 8)
    text(50, 582, 'inspected in accordance with the specification and the purchase order requirements.', 8)
    text(50, 612, 'Quality department: ____________________', 9)
    text(330, 612, 'Inspector: ____________________', 9)
    if stamp:
        # красная круглая печать поверх таблицы позиций и синяя прямоугольная поверх состава
        c = pymupdf.Point(400, 330)
        for rad, wd in ((62, 2.2), (57, 0.8), (38, 0.8)):
            page.draw_circle(c, rad, color=RED, width=wd, stroke_opacity=0.8)
        for s, dy, size in (('EXAMPLE STEEL WORKS', -8, 8.5), ('QUALITY CONTROL', 6, 8.5), ('PASSED', 22, 11)):
            w = pymupdf.get_text_length(s, 'hebo', size)
            page.insert_text((c.x - w / 2, c.y + dy), s, fontsize=size, fontname='hebo', color=RED,
                             fill_opacity=0.8, morph=(c, pymupdf.Matrix(-12)))
        r = pymupdf.Rect(170, 462, 330, 505)
        page.draw_rect(r, color=BLUE, width=1.6, stroke_opacity=0.75)
        page.insert_text((182, 480), 'CHECKED 2025.07.31', fontsize=11, fontname='hebo', color=BLUE,
                         fill_opacity=0.75)
        page.insert_text((182, 496), 'INSPECTOR No. 07', fontsize=10, fontname='hebo', color=BLUE,
                         fill_opacity=0.75)
    return doc


def as_scan(page, skew: float = 0.0, pale: float = 0.0, jpeg: int = 75, rot90: int = 0, seed: int = 1):
    """Рендер страницы «как скан»: лёгкое размытие, шум, сжатие JPEG; наклон, бледность, поворот."""
    z = DPI / 72.0
    m = pymupdf.Matrix(z, z)
    if skew:
        m = m.prerotate(skew)
    pix = page.get_pixmap(matrix=m, colorspace=pymupdf.csRGB, alpha=False)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(np.float32)
    k = np.array([1, 2, 1], np.float32) / 4
    img[1:-1] = img[:-2] * k[0] + img[1:-1] * k[1] + img[2:] * k[2]
    img[:, 1:-1] = img[:, :-2] * k[0] + img[:, 1:-1] * k[1] + img[:, 2:] * k[2]
    if pale:
        img = 255.0 - (255.0 - img) * (1.0 - pale) - 12.0          # светло-серый текст на сероватом листе
    img += np.random.default_rng(seed).normal(0, 4.0, img.shape).astype(np.float32)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if rot90:
        img = np.ascontiguousarray(np.rot90(img, k=-rot90))
    pm = pymupdf.Pixmap(pymupdf.csRGB, img.shape[1], img.shape[0], img.tobytes(), False)
    return pm.tobytes('jpeg', jpg_quality=jpeg), img.shape[1], img.shape[0]


def scan_pdf(path, jpeg_bytes, w, h):
    doc = pymupdf.open()
    page = doc.new_page(width=w * 72.0 / DPI, height=h * 72.0 / DPI)
    page.insert_image(page.rect, stream=jpeg_bytes)
    doc.save(path)
    doc.close()


def small_picture(jpeg_bytes, out_path, width=1000, limit=300_000, clip=None):
    """Уменьшенная картинка страницы для документа: не шире width точек и не тяжелее limit байт.
    clip: показать только часть страницы (доли ширины и высоты), как её получает распознавание."""
    src = pymupdf.Pixmap(jpeg_bytes)
    if clip:
        img = np.frombuffer(src.samples, np.uint8).reshape(src.height, src.width, src.n)[:, :, :3]
        x0, y0 = int(clip[0] * src.width), int(clip[1] * src.height)
        x1, y1 = int(clip[2] * src.width), int(clip[3] * src.height)
        part = np.ascontiguousarray(img[y0:y1, x0:x1])
        src = pymupdf.Pixmap(pymupdf.csRGB, part.shape[1], part.shape[0], part.tobytes(), False)
    s = min(1.0, width / max(src.width, src.height))
    pm = pymupdf.Pixmap(src, max(1, int(src.width * s)), max(1, int(src.height * s)), None)
    for q in (72, 60, 50, 40):
        data = pm.tobytes('jpeg', jpg_quality=q)
        if len(data) <= limit:
            break
    with open(out_path, 'wb') as f:
        f.write(data)
    return len(data)


EXAMPLES = [
    ('01_clean', 'Чистый скан', dict(), None),
    ('02_stamp', 'Цветные печати поверх текста', dict(stamp=True), None),
    ('03_sideways', 'Страница лежит боком', dict(rot90=1), None),
    ('04_skew', 'Наклон 1.5 градуса', dict(skew=1.5), None),
    ('05_pale_jpeg', 'Бледный скан с сильным сжатием', dict(pale=0.55, jpeg=30), None),
    ('06_band', 'Только полоса бланка (часть страницы)', dict(), (0.0, 0.17, 1.0, 0.50)),
]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'docs', 'recognition', 'examples')
    os.makedirs(out, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='ocr_examples_')
    ocrkit.recognize_image(np.full((64, 256, 3), 255, np.uint8))        # загрузка моделей вне замера времени
    report = []
    for name, title, kw, clip in EXAMPLES:
        kw = dict(kw)
        doc = draw(stamp=kw.pop('stamp', False))
        data, w, h = as_scan(doc[0], **kw)
        doc.close()
        pdf = os.path.join(tmp, name + '.pdf')
        scan_pdf(pdf, data, w, h)
        t0 = time.perf_counter()
        res = ocrkit.recognize_pdf(pdf, engine='rapid', workers=1, cache_dir=None, clip=clip)
        sec = time.perf_counter() - t0
        pt = res[0]
        hits = ocrkit.find_codes(pt, CODES)
        want = CODES if clip is None else [c for c in CODES if c in [r[1] for r in ROWS] + [r[4] for r in ROWS]]
        found = {h.code: h for h in hits}
        exact = sorted(c for c in want if c in found and found[c].exact)
        fixed = sorted(c for c in want if c in found and not found[c].exact)
        lost = sorted(c for c in want if c not in found)
        size = small_picture(data, os.path.join(out, name + '.jpg'), clip=clip)
        with open(os.path.join(out, name + '.txt'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(pt.text + '\n')
        report.append({'name': name, 'title': title, 'seconds': round(sec, 2), 'lines': len(pt.lines),
                       'rotation': pt.rotation, 'skew': round(pt.skew, 2), 'codes_expected': len(want),
                       'codes_exact': len(exact), 'codes_fixed': fixed, 'codes_lost': lost,
                       'picture_bytes': size, 'clip': clip})
        print(f'{name}: {sec:.1f} с, строк {len(pt.lines)}, поворот {pt.rotation}, наклон {pt.skew:.2f}, кодов '
              f'{len(want)}: точно {len(exact)}, с поправкой {len(fixed)}, не найдено {len(lost)}; картинка '
              f'{size // 1024} КБ')
        os.remove(pdf)
    os.rmdir(tmp)
    with open(os.path.join(out, 'examples.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump({'codes': CODES, 'examples': report}, f, ensure_ascii=False, indent=1)
    write_readme(out, report)
    return 0


def write_readme(out, report):
    """Страница примеров: картинка, что прочитано, какие коды найдены, сколько заняло."""
    lines = ['# Примеры распознавания', '',
             'Все страницы ниже выдуманы: названия, номера и коды придуманы для примера и нарисованы скриптом',
             '`tools/ocr_examples.py`. Картинки уменьшены, распознавалась страница в 200 точек на дюйм.',
             'Время указано для ноутбука разработки (один поток), вместе с рендером страницы.', '',
             f'Известные коды, которые ищутся на странице: {len(CODES)} '
             '(коды изделий, номера плавок, номер сертификата, заказа и спецификации).', '']
    for r in report:
        text = open(os.path.join(out, r['name'] + '.txt'), encoding='utf-8').read().rstrip('\n').split('\n')
        lines += [f'## {r["title"]}', '', f'![{r["title"]}]({r["name"]}.jpg)', '']
        lines += [f'- время: {r["seconds"]} с; строк найдено: {r["lines"]}; поворот: {r["rotation"]} градусов; '
                  f'наклон: {r["skew"]} градуса',
                  f'- известных кодов на странице {r["codes_expected"]}: прочитано знак в знак '
                  f'{r["codes_exact"]}, найдено с поправкой похожих знаков {len(r["codes_fixed"])}, '
                  f'не найдено {len(r["codes_lost"])}']
        if r['codes_fixed']:
            lines.append('- найдены с поправкой: ' + ', '.join(r['codes_fixed']))
        if r['codes_lost']:
            lines.append('- не найдены: ' + ', '.join(r['codes_lost']))
        lines += ['', f'Прочитанный текст (полностью в файле `{r["name"]}.txt`, табуляция разделяет куски одной '
                  'зрительной строки):', '', '```text'] + text[:28] + (['...'] if len(text) > 28 else []) + ['```', '']
    with open(os.path.join(out, 'README.md'), 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines))


if __name__ == '__main__':
    sys.exit(main())
