
import cv2
import numpy as np


class VestDetector:
    """Определение сигнального жилета (жёлтого) по цвету в области торса человека.

    Гипотеза: сотрудники без жилетов, посторонние - в жёлтых жилетах.
    Анализируется средняя часть человека (торс), где обычно расположен жилет.
    """

    # Ярко-жёлтый сигнальный цвет в HSV (OpenCV: H 0-179, S/V 0-255)
    YELLOW_LOWER = np.array([20, 90, 120])
    YELLOW_UPPER = np.array([38, 255, 255])

    def __init__(self, min_yellow_ratio: float = 0.2):
        self.min_yellow_ratio = min_yellow_ratio

    def _yellow_ratio(self, frame: np.ndarray, bbox: list) -> float:
        """Доля жёлтых пикселей в области торса (15-70% высоты человека)."""
        x1, y1, x2, y2 = bbox
        height = y2 - y1
        if height <= 0:
            return 0.0
        torso_y1 = y1 + int(height * 0.15)
        torso_y2 = y1 + int(height * 0.70)
        if torso_y1 >= torso_y2:
            return 0.0
        roi = frame[torso_y1:torso_y2, x1:x2]
        if roi.size == 0:
            return 0.0
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.YELLOW_LOWER, self.YELLOW_UPPER)
        return float(cv2.countNonZero(mask)) / (roi.shape[0] * roi.shape[1])

    def is_vest(self, frame: np.ndarray, bbox: list) -> bool:
        return self._yellow_ratio(frame, bbox) >= self.min_yellow_ratio

    def flag_persons(self, frame: np.ndarray, persons: list[dict]) -> list[dict]:
        """Возвращает людей, помеченных флагом vest=True/False."""
        flagged = []
        for person in persons:
            person = dict(person)
            person["vest"] = self.is_vest(frame, person["bbox"])
            flagged.append(person)
        return flagged