"""
SORT — Simple Online and Realtime Tracking (Bewley et al., 2016).

Mỗi track là một Kalman filter với trạng thái [cx, cy, s, r, vcx, vcy, vs]
(tâm box, diện tích s, tỉ lệ khung r cố định, và vận tốc của cx/cy/s).
Mỗi frame: dự đoán vị trí mọi track, ghép detection mới với track bằng
Hungarian algorithm trên ma trận IoU, cập nhật track được ghép, tạo track
mới cho detection chưa ghép, và xoá track mất dấu quá `max_age` frame.

Toạ độ box dùng hệ chuẩn hoá [0..1] (x1, y1, x2, y2) để không phụ thuộc
độ phân giải ảnh detect hay ảnh video.
"""

import numpy as np
from filterpy.kalman import KalmanFilter
from scipy.optimize import linear_sum_assignment


def iou_matrix(a, b):
    """IoU giữa mọi box của a (N,4) và b (M,4), dạng (x1,y1,x2,y2)."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)), dtype=np.float32)
    a = a[:, None, :]
    b = b[None, :, :]
    ix1 = np.maximum(a[..., 0], b[..., 0])
    iy1 = np.maximum(a[..., 1], b[..., 1])
    ix2 = np.minimum(a[..., 2], b[..., 2])
    iy2 = np.minimum(a[..., 3], b[..., 3])
    inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
    area_a = (a[..., 2] - a[..., 0]) * (a[..., 3] - a[..., 1])
    area_b = (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
    union = area_a + area_b - inter
    return np.where(union > 0, inter / union, 0.0).astype(np.float32)


def _box_to_z(box):
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    return np.array([x1 + w / 2, y1 + h / 2, w * h, w / max(h, 1e-9)]).reshape(4, 1)


def _x_to_box(x):
    s, r = max(x[2], 1e-12), max(x[3], 1e-9)
    w = np.sqrt(s * r)
    h = s / w
    cx, cy = x[0], x[1]
    return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], dtype=np.float32)


class Track:
    _next_id = 1

    def __init__(self, box, score, cls):
        kf = KalmanFilter(dim_x=7, dim_z=4)
        kf.F = np.eye(7)
        kf.F[0, 4] = kf.F[1, 5] = kf.F[2, 6] = 1.0       # vị trí += vận tốc
        kf.H = np.zeros((4, 7))
        kf.H[0, 0] = kf.H[1, 1] = kf.H[2, 2] = kf.H[3, 3] = 1.0
        kf.R[2:, 2:] *= 10.0                               # đo diện tích/tỉ lệ kém tin cậy hơn
        kf.P[4:, 4:] *= 1000.0                             # vận tốc ban đầu chưa biết
        kf.P *= 10.0
        kf.Q[-1, -1] *= 0.01
        kf.Q[4:, 4:] *= 0.01
        kf.x[:4] = _box_to_z(box)
        self.kf = kf
        self.id = Track._next_id
        Track._next_id += 1
        self.cls = cls
        self.score = score
        self.hits = 1               # số lần được ghép với detection
        self.age = 0                # số frame đã sống
        self.time_since_update = 0  # số frame liên tiếp không được ghép

    def predict(self):
        if self.kf.x[2] + self.kf.x[6] <= 0:              # tránh diện tích âm
            self.kf.x[6] = 0.0
        self.kf.predict()
        self.age += 1
        self.time_since_update += 1
        return self.box

    def update(self, box, score, cls):
        self.kf.update(_box_to_z(box))
        self.hits += 1
        self.time_since_update = 0
        self.score = score
        self.cls = cls

    @property
    def box(self):
        return _x_to_box(self.kf.x[:, 0])


class Sort:
    def __init__(self, max_age=30, min_hits=3, iou_threshold=0.25):
        self.max_age = max_age          # frame được phép mất dấu trước khi xoá track
        self.min_hits = min_hits        # số lần ghép tối thiểu trước khi coi là track thật
        self.iou_threshold = iou_threshold
        self.tracks = []
        self.frame_count = 0

    def update(self, dets):
        """dets: list (box[x1,y1,x2,y2] chuẩn hoá, score, cls). Trả về các track đã xác nhận."""
        self.frame_count += 1
        predicted = np.array([t.predict() for t in self.tracks], dtype=np.float32).reshape(-1, 4)
        det_boxes = np.array([d[0] for d in dets], dtype=np.float32).reshape(-1, 4)

        matched_t, matched_d = set(), set()
        if len(predicted) and len(det_boxes):
            iou = iou_matrix(det_boxes, predicted)
            rows, cols = linear_sum_assignment(-iou)
            for d, t in zip(rows, cols):
                if iou[d, t] >= self.iou_threshold:
                    self.tracks[t].update(dets[d][0], dets[d][1], dets[d][2])
                    matched_t.add(t)
                    matched_d.add(d)

        for d, det in enumerate(dets):
            if d not in matched_d:
                self.tracks.append(Track(det[0], det[1], det[2]))

        self.tracks = [t for t in self.tracks if t.time_since_update <= self.max_age]

        # Track "xác nhận": đủ số lần ghép (hoặc đang ở các frame đầu tiên của chương trình)
        return [t for t in self.tracks
                if t.hits >= self.min_hits or self.frame_count <= self.min_hits]
