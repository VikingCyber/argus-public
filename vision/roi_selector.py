import argparse
from pathlib import Path

import cv2
import numpy as np


class RoiSelector:
    def __init__(self, image: np.ndarray):
        self.image = image
        self.height, self.width = image.shape[:2]
        self.scale = min(1.0, 1200 / self.width, 700 / self.height)
        self.view = cv2.resize(image, None, fx=self.scale, fy=self.scale) if self.scale < 1 else image.copy()
        self.rois = []
        self.start = None
        self.current = None

    def _point_to_image(self, point):
        return (round(point[0] / self.scale), round(point[1] / self.scale))

    def _draw(self):
        canvas = self.view.copy()
        for x1, y1, x2, y2 in self.rois:
            a = (round(x1 * self.scale), round(y1 * self.scale))
            b = (round(x2 * self.scale), round(y2 * self.scale))
            cv2.rectangle(canvas, a, b, (0, 0, 255), 2)
        if self.current:
            a, b = self.current
            cv2.rectangle(canvas, a, b, (0, 255, 255), 2)
        cv2.putText(canvas, "Drag: exclude zone | s: save | u: undo | c: clear | q: quit",
                    (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        return canvas

    def mouse(self, event, x, y, flags, _):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.start = (x, y)
            self.current = (self.start, self.start)
        elif event == cv2.EVENT_MOUSEMOVE and self.start:
            self.current = (self.start, (x, y))
        elif event == cv2.EVENT_LBUTTONUP and self.start:
            end = (x, y)
            x1, y1 = self._point_to_image((min(self.start[0], end[0]), min(self.start[1], end[1])))
            x2, y2 = self._point_to_image((max(self.start[0], end[0]), max(self.start[1], end[1])))
            if x2 > x1 and y2 > y1:
                self.rois.append((x1, y1, x2, y2))
            self.start = None
            self.current = None

    def run(self):
        window = "Argus ROI selector"
        cv2.namedWindow(window)
        cv2.setMouseCallback(window, self.mouse)
        while True:
            cv2.imshow(window, self._draw())
            key = cv2.waitKey(30) & 0xFF
            if key == ord("s"):
                print("\nВставь в unread_plate и collect в config.yaml:")
                for roi in self.rois:
                    normalized = [round(roi[0] / self.width, 4),
                                  round(roi[1] / self.height, 4),
                                  round(roi[2] / self.width, 4),
                                  round(roi[3] / self.height, 4)]
                    print(f"  - {normalized}")
            elif key == ord("u") and self.rois:
                self.rois.pop()
            elif key == ord("c"):
                self.rois.clear()
            elif key in (ord("q"), 27):
                break
        cv2.destroyAllWindows()


def main():
    parser = argparse.ArgumentParser(description="Разметка зон, исключаемых из детекции")
    parser.add_argument("image", nargs="?", default=str(Path(__file__).resolve().parent.parent / "images/easy.jpg"))
    args = parser.parse_args()
    image = cv2.imdecode(np.fromfile(args.image, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f"Не удалось открыть: {args.image}")
    RoiSelector(image).run()


if __name__ == "__main__":
    main()
