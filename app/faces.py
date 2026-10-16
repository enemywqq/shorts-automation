"""Лица в клипах для автовыбора раскладки.

Запускается python из venv генератора, потому что там уже есть OpenCV; в оркестратор
этот модуль не импортируется. Аргументы: модель YuNet, видео, JSON
{"spans": [[start, end], ...], "step": секунды, "score": порог уверенности, "ffmpeg": путь}.
Необязательные "crops": рамка без чёрных полос w:h:x:y (или null) на каждый клип и "scene": порог
scene detection ffmpeg для слежения.
В stdout JSON {"size": [ширина, высота], "clips": [...]}: на каждый клип список
замеров [t, [[cx, cy, w, h, motion], ...]], t от начала клипа, рамки лиц в долях кадра
(после обрезки полос). motion: средняя разница яркости (0..255) в рамке лица с прошлым
замером, у первого замера клипа -1. Живое лицо в вебке меняется, аватар или картинка в
оверлее почти нет. С "scene" ещё "cuts": на каждый клип время склеек от его начала.
"""
import json
import re
import subprocess
import sys
import tempfile

import cv2
import numpy as np

DETECT_WIDTH = 640


def ffmpeg_cmd(opts: dict, video: str, start: float, end: float, crop: "str | None", w: int, h: int) -> list[str]:
    """Кадры отрезка раз в step секунд, уже уменьшенные до w x h, одним проходом ffmpeg с быстрой
    перемоткой: перемотка OpenCV на каждый замер декодировала от ключевого кадра каждый раз.
    Со "scene" в том же проходе вторая ветка ищет склейки по всем кадрам."""
    pick = f"crop={crop}," if crop else ""
    cmd = [opts["ffmpeg"], "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", video]
    raw = ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    if "scene" not in opts:
        return cmd + ["-v", "error", "-vf", f"{pick}fps=1/{opts['step']},scale={w}:{h}", *raw]
    # metadata=print пишет время выбранных кадров в stderr на уровне info. Первый кадр select
    # пропускает всегда: без единого кадра выход null падает, а в клипе может не быть склеек.
    graph = (f"[0:v]{pick}split[a][b];[a]fps=1/{opts['step']},scale={w}:{h}[f];"
             f"[b]scale=320:-2,select='gt(scene,{opts['scene']})+eq(n,0)',metadata=print[s]")
    return cmd + ["-v", "info", "-filter_complex", graph, "-map", "[f]", *raw, "-map", "[s]", "-f", "null", "-"]


def frames(cmd: list[str], w: int, h: int, err):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
    size = w * h * 3
    while len(buf := proc.stdout.read(size)) == size:
        yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    if proc.wait() != 0:
        err.seek(0)
        sys.exit(f"ffmpeg не отдал кадры: {' '.join(cmd)}\n{err.read().decode(errors='replace')[-2000:]}")


def main() -> None:
    model, video, opts = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        sys.exit(f"не открылось видео: {video}")
    src_w, src_h = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    cap.release()
    result, cuts = [], []
    crops = opts.get("crops") or [None] * len(opts["spans"])
    for (start, end), crop in zip(opts["spans"], crops):
        cw, ch = map(int, crop.split(":")[:2]) if crop else (src_w, src_h)
        w, h = DETECT_WIDTH, round(ch * DETECT_WIDTH / cw)
        detector = cv2.FaceDetectorYN.create(model, "", (w, h), score_threshold=opts["score"])
        samples, prev = [], None
        with tempfile.TemporaryFile() as err:
            for i, small in enumerate(frames(ffmpeg_cmd(opts, video, start, end, crop, w, h), w, h, err)):
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
                samples.append([round(i * opts["step"], 2), boxes])
                prev = gray
            err.seek(0)
            cuts.append([float(t) for t in re.findall(rb"pts_time:(\d+(?:\.\d+)?)", err.read()) if float(t) > 0])
        result.append(samples)
    print(json.dumps({"size": [src_w, src_h], "clips": result, **({"cuts": cuts} if "scene" in opts else {})}))


if __name__ == "__main__":
    main()
