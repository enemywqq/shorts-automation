"""Лица в клипах для автовыбора раскладки.

Запускается python из venv генератора, потому что там уже есть OpenCV; в оркестратор
этот модуль не импортируется. Аргументы: модель YuNet, видео, JSON
{"spans": [[start, end], ...], "step": секунды, "score": порог уверенности}.
В stdout JSON {"size": [ширина, высота], "clips": [...]}: на каждый клип список
замеров [t, [[cx, cy, w, h, motion], ...]], t от начала клипа, рамки лиц в долях кадра.
motion: средняя разница яркости (0..255) в рамке лица с прошлым замером, у первого
замера клипа -1. Живое лицо в вебке меняется, аватар или картинка в оверлее почти нет.
"""
import json
import sys

import cv2

DETECT_WIDTH = 640


def main() -> None:
    model, video, opts = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        sys.exit(f"не открылось видео: {video}")
    src_w, src_h = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    w, h = DETECT_WIDTH, round(src_h * DETECT_WIDTH / src_w)
    detector = cv2.FaceDetectorYN.create(model, "", (w, h), score_threshold=opts["score"])
    result = []
    for start, end in opts["spans"]:
        samples, t, prev = [], 0.0, None
        while start + t < end:
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + t) * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            small = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            _, faces = detector.detect(small)
            boxes = []
            for x, y, fw, fh in ([] if faces is None else (f[:4] for f in faces)):
                x0, y0 = max(int(x), 0), max(int(y), 0)
                x1, y1 = min(int(x + fw), w), min(int(y + fh), h)
                motion = -1.0
                if prev is not None and x1 > x0 and y1 > y0:
                    motion = float(cv2.absdiff(gray[y0:y1, x0:x1], prev[y0:y1, x0:x1]).mean())
                boxes.append([round(float(v), 4) for v in ((x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h, motion)])
            samples.append([round(t, 2), boxes])
            prev = gray
            t += opts["step"]
        result.append(samples)
    print(json.dumps({"size": [src_w, src_h], "clips": result}))


if __name__ == "__main__":
    main()
