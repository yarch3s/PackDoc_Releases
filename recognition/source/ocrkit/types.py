"""Типы результата распознавания: строка текста с прямоугольником и страница."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Line:
    """Строка (кусок текста одной рамкой: ячейка таблицы, строка абзаца).

    bbox: прямоугольник (x0, y0, x1, y1) в координатах страницы PDF, как их отдаёт PyMuPDF в page.get_text() и
    page.search_for() и ждёт в page.insert_text(): точки 1/72 дюйма от левого верхнего угла страницы; свойство
    страницы /Rotate при этом не учитывается (у страниц без /Rotate это просто «как на экране»);
    quad: четыре угла строки в тех же координатах по ходу текста (верх-лево, верх-право, низ-право, низ-лево):
    у страницы, лежащей боком, строка стоит вертикально, и углы показывают направление письма;
    conf: уверенность движка от 0 до 1 (у текстового слоя 1.0).
    """
    text: str
    bbox: tuple
    conf: float = 1.0
    quad: tuple = ()

    def to_json(self) -> dict:
        return {'t': self.text, 'b': [round(v, 2) for v in self.bbox], 'c': round(float(self.conf), 4),
                'q': [[round(x, 2), round(y, 2)] for x, y in self.quad]}

    @classmethod
    def from_json(cls, d: dict) -> 'Line':
        return cls(d['t'], tuple(d['b']), d.get('c', 1.0), tuple(tuple(p) for p in d.get('q') or ()))


@dataclass
class PageText:
    """Текст одной страницы.

    text: весь текст в порядке чтения: зрительные строки сверху вниз через перевод строки, куски одной зрительной
    строки (ячейки таблицы) слева направо через знак табуляции;
    lines: куски текста (Line) с прямоугольниками в том же порядке, что в text;
    source: 'layer' (текстовый слой PDF) либо имя движка распознавания;
    rotation: на сколько градусов по часовой стрелке видимую страницу повернули перед распознаванием, чтобы текст
    встал прямо (0, 90, 180, 270); skew: найденный небольшой наклон строк в градусах;
    width, height: размер страницы в точках (без учёта /Rotate); page_rotation: свойство /Rotate страницы;
    seconds: время распознавания страницы; cached: результат взят из кэша.
    """
    page: int
    text: str = ''
    lines: list = field(default_factory=list)
    source: str = ''
    rotation: int = 0
    skew: float = 0.0
    width: float = 0.0
    height: float = 0.0
    seconds: float = 0.0
    cached: bool = False
    page_rotation: int = 0

    def rows(self) -> list:
        """Зрительные строки: [[Line, ...], ...] (те же куски, что в lines, по строкам text)."""
        out, i = [], 0
        for raw in self.text.split('\n') if self.lines else []:
            n = raw.count('\t') + 1
            out.append(self.lines[i:i + n])
            i += n
        return out

    def to_json(self) -> dict:
        return {'page': self.page, 'source': self.source, 'rotation': self.rotation, 'skew': round(self.skew, 3),
                'width': round(self.width, 2), 'height': round(self.height, 2), 'seconds': round(self.seconds, 3),
                'page_rotation': self.page_rotation,
                'rows': [raw.count('\t') + 1 for raw in self.text.split('\n')] if self.lines else [],
                'lines': [ln.to_json() for ln in self.lines]}

    @classmethod
    def from_json(cls, d: dict) -> 'PageText':
        lines = [Line.from_json(x) for x in d.get('lines', [])]
        rows, i = [], 0
        for n in d.get('rows') or [1] * len(lines):
            rows.append('\t'.join(ln.text for ln in lines[i:i + n]))
            i += n
        return cls(page=d['page'], text='\n'.join(rows), lines=lines, source=d.get('source', ''),
                   rotation=d.get('rotation', 0), skew=d.get('skew', 0.0), width=d.get('width', 0.0),
                   height=d.get('height', 0.0), seconds=d.get('seconds', 0.0),
                   page_rotation=d.get('page_rotation', 0))


class OcrError(Exception):
    """Ошибка распознавания с понятным текстом для пользователя."""


class EngineUnavailable(OcrError):
    """Движок распознавания на этой машине недоступен (в тексте причина)."""


class Cancelled(OcrError):
    """Работа прервана по просьбе вызывающего (функция cancel вернула True). В поле pages готовые страницы."""
    pages: dict = {}
