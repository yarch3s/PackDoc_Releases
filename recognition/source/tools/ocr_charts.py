"""Графики замеров распознавания: python tools/ocr_charts.py [файл чисел] [папка]

Числа берутся из docs/recognition/measurements.json (итоги замеров без данных заказчика), картинки PNG шириной
1600 точек кладутся в docs/recognition/charts. Нужен matplotlib (только для построения графиков, в программу он
не входит).

Оформление одно на все графики: белый фон, выбранный способ синим, остальные серым; числа подписаны у столбцов
и точек текстом основного цвета; сетка и оси тонкие и бледные; у столбцов закруглён только конец с данными."""
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
import matplotlib  # noqa: E402

matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import PathPatch  # noqa: E402
from matplotlib.path import Path  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DPI = 160
WIDTH = 10.0            # дюймов: 1600 точек
SURFACE = '#ffffff'
ACCENT = '#2a78d6'      # выбранный способ, основной ряд
LIGHT = '#86b6ef'       # второй ряд того же цвета (светлее)
GRAY = '#b0aea6'        # остальные способы
INK = '#0b0b0b'
INK2 = '#52514e'
MUTED = '#898781'
GRID = '#e1e0d9'
AXIS = '#c3c2b7'
BAR_PX = 30             # толщина столбца в точках картинки
R_PX = 6                # закругление конца с данными


def setup():
    plt.rcParams.update({
        'font.family': ['Segoe UI', 'DejaVu Sans'], 'font.size': 13, 'figure.facecolor': SURFACE,
        'axes.facecolor': SURFACE, 'axes.edgecolor': AXIS, 'axes.linewidth': 0.8, 'text.color': INK,
        'axes.labelcolor': INK2, 'xtick.color': MUTED, 'ytick.color': INK, 'xtick.labelsize': 11.5,
        'ytick.labelsize': 13, 'savefig.facecolor': SURFACE,
    })


def figure(height, title, subtitle=''):
    fig = plt.figure(figsize=(WIDTH, height), dpi=DPI)
    fig.text(0.03, 1 - 0.30 / height, title, fontsize=18, fontweight='bold', va='top', ha='left', color=INK)
    if subtitle:
        fig.text(0.03, 1 - 0.68 / height, subtitle, fontsize=12.5, va='top', ha='left', color=INK2)
    return fig


def note(fig, text):
    fig.text(0.03, 0.02, text, fontsize=11, va='bottom', ha='left', color=MUTED)


def clean(ax, grid='x'):
    for side in ('top', 'right', 'left' if grid == 'x' else 'right'):
        ax.spines[side].set_visible(False)
    ax.spines['bottom' if grid == 'y' else 'left'].set_visible(grid == 'y')
    ax.spines['bottom'].set_color(AXIS)
    ax.tick_params(length=0)
    ax.set_axisbelow(True)
    ax.grid(axis=grid, color=GRID, linewidth=0.8)


def px(ax):
    """Сколько единиц данных в одной точке картинки по x и по y."""
    (x0, y0), (x1, y1) = ax.transData.transform([(0, 0), (1, 1)])
    return 1.0 / abs(x1 - x0), 1.0 / abs(y1 - y0)


def hbar(ax, y, value, color, x0=0.0):
    """Горизонтальный столбец от x0 до value: у основания прямой, у конца с данными закруглён."""
    ux, uy = px(ax)
    h = BAR_PX * uy
    rx = min(R_PX * ux, max(0.0, value - x0))
    ry = R_PX * uy
    a, b = y - h / 2, y + h / 2
    verts = [(x0, a), (value - rx, a), (value, a), (value, a + ry), (value, b - ry), (value, b), (value - rx, b),
             (x0, b), (x0, a)]
    codes = [Path.MOVETO, Path.LINETO, Path.CURVE3, Path.CURVE3, Path.LINETO, Path.CURVE3, Path.CURVE3,
             Path.LINETO, Path.CLOSEPOLY]
    ax.add_patch(PathPatch(Path(verts, codes), facecolor=color, edgecolor='none'))


def vbar(ax, x, value, color):
    """Вертикальный столбец от нуля до value с закруглённым верхом."""
    ux, uy = px(ax)
    w = BAR_PX * 1.5 * ux
    rx = R_PX * ux
    ry = min(R_PX * uy, value)
    a, b = x - w / 2, x + w / 2
    verts = [(a, 0), (a, value - ry), (a, value), (a + rx, value), (b - rx, value), (b, value), (b, value - ry),
             (b, 0), (a, 0)]
    codes = [Path.MOVETO, Path.LINETO, Path.CURVE3, Path.CURVE3, Path.LINETO, Path.CURVE3, Path.CURVE3,
             Path.LINETO, Path.CLOSEPOLY]
    ax.add_patch(PathPatch(Path(verts, codes), facecolor=color, edgecolor='none'))


def save(fig, out, name):
    path = os.path.join(out, name)
    fig.savefig(path, dpi=DPI, metadata={'Software': None})
    plt.close(fig)
    print(name, os.path.getsize(path) // 1024, 'КБ')


def fmt(v):
    # 99,98 не должно превратиться в «100,0»: у самого верха шкалы показываем два знака после запятой
    text = f'{v:.2f}' if 99.95 <= v < 100 else f'{v:.1f}'
    return text.replace('.', ',') + ' %'


def accuracy_methods(d, out):
    ms = d['methods']
    n = len(ms)
    height = 1.75 + 0.62 * n + 0.75
    fig = figure(height, 'Точность чтения кодов и чисел: какой способ сколько читает',
                 'Доля слов, прочитанных знак в знак; в скобках с допуском одной ошибки в слове')
    top, bottom = 1 - 1.6 / height, 0.75 / height
    panels = (('A', d['sets']['A']['title'], 0.36, 0.27), ('B', d['sets']['B']['title'], 0.70, 0.27))
    for key, title, left, width in panels:
        ax = fig.add_axes([left, bottom, width, top - bottom])
        ax.set_xlim(0, 100)
        ax.set_ylim(n - 0.5, -0.5)
        clean(ax)
        ax.set_xticks([0, 25, 50, 75, 100])
        ax.set_xticklabels(['0', '25 %', '50 %', '75 %', '100 %'])
        ax.set_title(title, fontsize=12, color=INK2, loc='left', pad=8, linespacing=1.35)
        ax.set_yticks(range(n))
        ax.set_yticklabels([m['name'] for m in ms] if key == 'A' else [''] * n)
        for i, m in enumerate(ms):
            v = m.get(key + '_exact')
            if v is None:
                ax.text(2, i, 'не измерялось', va='center', ha='left', fontsize=12, color=MUTED)
                continue
            hbar(ax, i, v, ACCENT if m.get('chosen') else GRAY)
            one = m.get(key + '_one')
            ax.text(v - 2.5, i, fmt(v) + (f'  ({fmt(one)})' if one is not None else ''), va='center', ha='right',
                    fontsize=12.5, color='#ffffff' if m.get('chosen') else INK,
                    fontweight='bold' if m.get('chosen') else 'normal')
    note(fig, d['notes']['accuracy'])
    save(fig, out, 'accuracy_methods.png')


def speed_workers(d, out):
    s = d['speed']
    height = 4.9
    fig = figure(height, 'Скорость и память выбранного способа от числа работников',
                 s['subtitle'])
    top, bottom = 1 - 1.45 / height, 0.95 / height
    xs = list(range(len(s['workers'])))
    for left, key, title, unit, digits in ((0.07, 'pages_per_min', 'Страниц в минуту', '', 1),
                                           (0.56, 'peak_gb', 'Память в пике, ГБ', '', 1)):
        ax = fig.add_axes([left, bottom, 0.40, top - bottom])
        vals = s[key]
        ax.set_xlim(-0.6, len(xs) - 0.4)
        ax.set_ylim(0, max(vals) * 1.18)
        clean(ax, 'y')
        ax.set_xticks(xs)
        ax.set_xticklabels([str(w) for w in s['workers']])
        ax.tick_params(axis='x', colors=INK, labelsize=13)
        ax.tick_params(axis='y', colors=MUTED, labelsize=11.5)
        ax.yaxis.set_major_formatter(lambda v, _pos: f'{v:g}'.replace('.', ','))
        ax.set_title(title, fontsize=13, color=INK2, loc='left', pad=8)
        ax.set_xlabel('работников (потоков)', fontsize=12)
        for x, v in zip(xs, vals):
            vbar(ax, x, v, ACCENT)
            ax.text(x, v + max(vals) * 0.025, f'{v:.{digits}f}'.replace('.', ','), ha='center', va='bottom',
                    fontsize=12.5, color=INK)
    note(fig, d['notes']['speed'])
    save(fig, out, 'speed_workers.png')


def dots(ax, rows, lo, hi, legend):
    """Точки «точно» и «с одной ошибкой» на общей шкале, соединённые тонкой линией."""
    n = len(rows)
    ax.set_xlim(lo, hi)
    ax.set_ylim(n - 0.5, -0.5)
    clean(ax)
    ax.set_yticks(range(n))
    ax.set_yticklabels([r['name'] for r in rows])
    for i, r in enumerate(rows):
        a, b = r[legend[0][0]], r[legend[1][0]]
        ax.plot([a, b], [i, i], color=AXIS, linewidth=2, solid_capstyle='round', zorder=2)
        ax.scatter([a], [i], s=150, color=legend[0][2], edgecolor=SURFACE, linewidth=2, zorder=3)
        ax.scatter([b], [i], s=150, color=legend[1][2], edgecolor=SURFACE, linewidth=2, zorder=3)
        left, right = (a, b) if a <= b else (b, a)
        gap = (hi - lo) * 0.024
        ax.text(left - gap, i, fmt(left), ha='right', va='center', fontsize=12, color=INK)
        ax.text(right + gap, i, fmt(right), ha='left', va='center', fontsize=12, color=INK)
    handles = [plt.Line2D([], [], marker='o', linestyle='', markersize=10, color=c, label=lab)
               for _k, lab, c in legend]
    ax.legend(handles=handles, loc='lower left', bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, fontsize=12,
              handletextpad=0.3, columnspacing=1.6, borderaxespad=0.2)


def robustness(d, out):
    rows = d['robustness']
    height = 1.75 + 0.5 * len(rows) + 0.7
    fig = figure(height, 'Устойчивость выбранного способа к качеству скана',
                 'Одни и те же страницы с текстовым слоем, превращённые в скан разного качества')
    ax = fig.add_axes([0.36, 0.7 / height, 0.56, 1 - (1.75 + 0.7) / height])
    dots(ax, rows, 90, 100.6, (('exact', 'точно', ACCENT), ('one', 'с допуском одной ошибки', LIGHT)))
    ax.set_xticks([90, 92, 94, 96, 98, 100])
    ax.set_xticklabels([f'{v} %' for v in (90, 92, 94, 96, 98, 100)])
    note(fig, d['notes']['robustness'])
    save(fig, out, 'robustness.png')


def installer_weight(d, out):
    ms = [m for m in d['methods'] if m.get('installer_mb') is not None]
    n = len(ms)
    height = 1.3 + 0.62 * n + 0.75
    fig = figure(height, 'Сколько способ добавляет к установщику программы',
                 'Мегабайты после сжатия установщика')
    ax = fig.add_axes([0.36, 0.75 / height, 0.58, 1 - (1.3 + 0.75) / height])
    top = max(m['installer_mb'] for m in ms)
    ax.set_xlim(0, top * 1.25)
    ax.set_ylim(n - 0.5, -0.5)
    clean(ax)
    ax.set_yticks(range(n))
    ax.set_yticklabels([m['name'].replace(' *', '') for m in ms])
    for i, m in enumerate(ms):
        v = m['installer_mb']
        hbar(ax, i, v, ACCENT if m.get('chosen') else GRAY)
        kind = 'измерено' if m.get('installer_measured') else 'оценка'
        ax.text(v + top * 0.015, i, f'+{v:g} МБ'.replace('.', ',') + f' ({kind})', va='center', ha='left',
                fontsize=12.5, color=INK, fontweight='bold' if m.get('chosen') else 'normal')
    note(fig, d['notes']['installer'])
    save(fig, out, 'installer_weight.png')


def before_after(d, out):
    rows = d.get('before_after') or []
    if not rows:
        print('before_after: чисел пока нет, график не строится')
        return
    height = 1.75 + 0.62 * len(rows) + 0.7
    fig = figure(height, 'Точность до и после доработки чтения',
                 'Доля кодов и чисел, прочитанных знак в знак')
    ax = fig.add_axes([0.36, 0.7 / height, 0.56, 1 - (1.75 + 0.7) / height])
    lo = d.get('before_after_axis', [95, 100])[0]
    dots(ax, rows, lo, 100 + (100 - lo) * 0.12, (('before', 'было', LIGHT), ('after', 'стало', ACCENT)))
    ticks = [t for t in range(int(lo), 101)]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f'{t} %' for t in ticks])
    note(fig, d['notes']['before_after'])
    save(fig, out, 'before_after.png')


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'docs', 'recognition', 'measurements.json')
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, 'docs', 'recognition', 'charts')
    os.makedirs(out, exist_ok=True)
    with open(src, encoding='utf-8') as f:
        d = json.load(f)
    setup()
    accuracy_methods(d, out)
    speed_workers(d, out)
    robustness(d, out)
    installer_weight(d, out)
    before_after(d, out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
