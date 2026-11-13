# Модели распознавания: происхождение и лицензии

В этой папке лежат файлы моделей, которыми пакет `ocrkit` читает сканы. Модели работают только на компьютере
пользователя: при распознавании ничего не скачивается и никуда не отправляется. Файлы моделей не изменялись.

| Файл | Что делает | Откуда | Лицензия |
|---|---|---|---|
| `PP-OCRv6_det_tiny.onnx` | ищет строки текста на странице (PP-OCRv6, tiny) | проект PaddleOCR, выпуск 3.7.0; файл в формате ONNX взят из набора моделей RapidOCR 3.10.0 | Apache-2.0 |
| `PP-OCRv6_rec_tiny.onnx` | читает знаки в найденной строке (PP-OCRv6, tiny) | то же | Apache-2.0 |
| `ch_ppocr_mobile_v2.0_cls_infer.onnx` | определяет, не перевёрнута ли строка (PP-OCR mobile v2.0) | то же | Apache-2.0 |
| `ch_PP-OCRv4_rec_infer.onnx` | вторая модель чтения для режима «точнее»: перечитывает строки с цифрами (PP-OCRv4) | проект PaddleOCR; файл в формате ONNX взят из пакета RapidOCR 1.4.4 | Apache-2.0 |

Проекты:

- PaddleOCR (авторы моделей): https://github.com/PaddlePaddle/PaddleOCR , лицензия Apache-2.0
- RapidOCR (перевод моделей в формат ONNX): https://github.com/RapidAI/RapidOCR , лицензия Apache-2.0

Полный текст лицензии Apache-2.0 лежит рядом в файле `LICENSE-Apache-2.0.txt`.

Библиотеки, на которых модели исполняются (в сборку программы они входят как есть, их лицензии кладутся в папку
`licenses` сборки):

- ONNX Runtime: https://github.com/microsoft/onnxruntime , лицензия MIT
- NumPy: https://github.com/numpy/numpy , лицензия BSD-3-Clause
- PyWinRT (запасной движок Windows.Media.Ocr): https://github.com/pywinrt/pywinrt , лицензия MIT
