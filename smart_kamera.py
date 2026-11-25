"""
SMART KAMERA PENJAGA BARANG (Windows 10) - versi 2

Langsung jalan tanpa tombol pengaktifan. Ada 3 tingkat peringatan:
  HIJAU   : SIAGA - AMAN
  KUNING  : ADA YANG MENDEKATI!       (tangan masuk ZONA PENDEKATAN)  + suara
  MERAH   : TANGAN MAU AMBIL BARANG!  (tangan masuk TEMPAT BARANG)    + suara
  MERAH   : BARANG HILANG! (xx%)      (isi kotak barang berubah)      + sirene + suara

Cara pakai:
  1. Arahkan kamera ke barang, tekan SPASI untuk membekukan gambar.
  2. Tarik mouse untuk membuat kotak di barang, lalu tekan ENTER.
  3. Jauhkan tangan 3 detik (kalibrasi), lalu penjagaan otomatis aktif.

Tombol:
  SPASI = reset setelah alarm (kalibrasi ulang)
  R     = pilih ulang barang
  + / - = naikkan / turunkan sensitivitas
  Q     = keluar
"""

import collections
import datetime
import os
import subprocess
import threading
import time

import cv2
import numpy as np

try:
    import winsound  # bawaan Windows
except ImportError:
    winsound = None

# ---------------------------------------------------------------- pengaturan
CAMERA_INDEX = 0            # ganti 1 kalau webcam bukan yang pertama
CALIBRATE_SECONDS = 3       # jauhkan tangan selama ini sebelum penjagaan aktif
OUTER_MARGIN = 0.6          # lebar ZONA PENDEKATAN (0.6 = 60% ukuran barang)
PIXEL_DIFF = 35             # selisih kecerahan piksel yang dianggap "berubah"
STOLEN_RATIO = 0.80         # % piksel BARANG yang hilang agar alarm berbunyi (0.80 = 80%)
ITEM_STAND_OUT = 40         # piksel dianggap bagian barang jika beda sebesar ini dari latar
CONFIRM_SECONDS = 0.7       # barang harus hilang segini lama (anti alarm palsu)
ALERT_COOLDOWN = 3.0        # jeda antar suara peringatan (detik)
RECORD_AFTER = 10           # detik rekaman setelah alarm
FPS_RECORD = 20
OUT_DIR = "rekaman"

TEXT_LEVEL1 = "ada orang yang mendekat"
TEXT_LEVEL2 = "tangan mau ambil barang"
TEXT_STOLEN = "barang telah dicuri"

# ------------------------------------------------------------ deteksi tangan
try:
    import mediapipe as mp
    _mp_hands = mp.solutions.hands
    _mp_draw = mp.solutions.drawing_utils
    _hands = _mp_hands.Hands(
        max_num_hands=2, min_detection_confidence=0.5, min_tracking_confidence=0.5
    )
    HAS_HANDS = True
except Exception:
    _hands = None
    HAS_HANDS = False

FINGERTIPS = (4, 8, 12, 16, 20)

GREEN = (0, 200, 0)
YELLOW = (0, 255, 255)
RED = (0, 0, 255)
GRAY = (170, 170, 170)


# ------------------------------------------------------------------- suara
class Voice:
    """Mengucapkan teks lewat suara Windows (tanpa file tambahan)."""

    def __init__(self):
        self._proc = None

    def busy(self):
        return self._proc is not None and self._proc.poll() is None

    def say(self, text):
        if self.busy():
            return False
        cmd = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Speak('{text}')"
        )
        try:
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", cmd],
                creationflags=0x08000000,  # tanpa jendela hitam
            )
        except Exception:
            self._proc = None
        return True


def beep_pattern(pattern):
    """pattern = [(frekuensi, durasi_ms), ...] dimainkan di thread terpisah."""
    if winsound is None:
        return

    def run():
        for freq, ms in pattern:
            winsound.Beep(freq, ms)

    threading.Thread(target=run, daemon=True).start()


class Siren:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            if winsound is None:
                time.sleep(0.3)
                continue
            for freq in (900, 1500, 900, 1500):
                if self._stop.is_set():
                    return
                winsound.Beep(freq, 250)


# ----------------------------------------------------------------- bantuan
def expand_rect(rect, margin, frame_w, frame_h):
    x, y, w, h = rect
    mx, my = int(w * margin), int(h * margin)
    x0, y0 = max(0, x - mx), max(0, y - my)
    x1, y1 = min(frame_w, x + w + mx), min(frame_h, y + h + my)
    return x0, y0, x1 - x0, y1 - y0


def point_in_rect(p, rect):
    x, y, w, h = rect
    return x <= p[0] <= x + w and y <= p[1] <= y + h


def prepare(gray_crop, gain=1.0):
    """Haluskan gambar; 'gain' mengoreksi perubahan cahaya seluruh ruangan."""
    return cv2.GaussianBlur(gray_crop, (7, 7), 0).astype(np.float32) * gain


def build_item_mask(ref):
    """Tandai piksel yang merupakan BARANG (beda dari warna latar di tepi kotak).
    Hanya piksel ini yang dihitung, jadi bayangan/lengan di latar tidak memicu alarm.
    Mengembalikan (mask, kontras_rendah)."""
    h, w = ref.shape
    b = max(2, min(h, w) // 8)
    ring = np.concatenate(
        [ref[:b].ravel(), ref[-b:].ravel(), ref[:, :b].ravel(), ref[:, -b:].ravel()]
    )
    bg = float(np.median(ring))
    mask = (np.abs(ref - bg) > ITEM_STAND_OUT).astype(np.uint8)
    mask = cv2.erode(mask, np.ones((3, 3), np.uint8)).astype(bool)
    ratio = float(mask.mean())
    if ratio > 0.85:                 # kotak pas di barang: pakai seluruh kotak
        return np.ones_like(mask, dtype=bool), False
    if ratio < 0.05:                 # barang hampir sewarna dengan latar
        return np.ones_like(mask, dtype=bool), True
    return mask, False


def select_item(cap):
    """Bekukan gambar, lalu pengguna menarik kotak di sekitar barang."""
    print("Arahkan kamera ke barang, lalu tekan SPASI untuk membekukan gambar.")
    while True:
        ok, frame = cap.read()
        if not ok:
            return None, None
        view = frame.copy()
        cv2.putText(view, "Tekan SPASI untuk membekukan gambar (Q = keluar)", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, YELLOW, 2)
        cv2.imshow("Smart Kamera", view)
        key = cv2.waitKey(1) & 0xFF
        if key == ord(" "):
            break
        if key in (ord("q"), ord("Q"), 27):
            return None, None
    guide = frame.copy()
    cv2.putText(guide, "TARIK MOUSE untuk membuat kotak di barang,",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, YELLOW, 2)
    cv2.putText(guide, "lalu tekan ENTER (C = batal)",
                (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, YELLOW, 2)
    rect = cv2.selectROI("Smart Kamera", guide, showCrosshair=False)
    if rect[2] < 10 or rect[3] < 10:
        return None, None
    return frame, tuple(int(v) for v in rect)


# --------------------------------------------------------------------- main
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print("Kamera tidak bisa dibuka. Coba ubah CAMERA_INDEX di bagian atas file.")
        return

    frame0, item = select_item(cap)
    if item is None:
        cap.release()
        cv2.destroyAllWindows()
        return

    fh, fw = frame0.shape[:2]
    outer = expand_rect(item, OUTER_MARGIN, fw, fh)

    voice = Voice()
    siren = Siren()
    buffer = collections.deque(maxlen=FPS_RECORD * 3)  # ~3 detik sebelum alarm

    calibrating = True
    calib_start = time.time()
    reference, ref_mean = None, 128.0
    item_mask, low_contrast = None, False
    stolen = False
    change_since = None
    level, last_alert, last_stolen_voice = 0, 0.0, 0.0
    writer, record_until = None, 0.0
    sensitivity = STOLEN_RATIO
    prev_outer = None
    last_key, last_key_time = "", 0.0

    print("Jauhkan tangan 3 detik, penjagaan akan aktif otomatis.")
    if not HAS_HANDS:
        print("(MediaPipe tidak ditemukan: deteksi tangan diganti deteksi gerakan.)")

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        now = time.time()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        buffer.append(frame.copy())
        view = frame.copy()

        # ---------- deteksi tangan
        points, hand_outer, hand_inner = [], False, False
        if HAS_HANDS:
            res = _hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            if res.multi_hand_landmarks:
                for hl in res.multi_hand_landmarks:
                    _mp_draw.draw_landmarks(view, hl, _mp_hands.HAND_CONNECTIONS)
                    for i, lm in enumerate(hl.landmark):
                        p = (int(lm.x * fw), int(lm.y * fh))
                        points.append(p)
                        if i == 8:  # ujung jari telunjuk
                            cv2.circle(view, p, 8, YELLOW, -1)
            hand_outer = any(point_in_rect(p, outer) for p in points)
            hand_inner = any(point_in_rect(p, item) for p in points)
        else:
            ox, oy, ow, oh = outer
            cur = cv2.GaussianBlur(gray[oy:oy + oh, ox:ox + ow], (7, 7), 0)
            if prev_outer is not None and prev_outer.shape == cur.shape:
                moved = float((cv2.absdiff(cur, prev_outer) > 25).mean())
                hand_outer = moved > 0.02
                hand_inner = moved > 0.06
            prev_outer = cur

        # ---------- perubahan isi kotak barang
        x, y, w, h = item
        changed = 0.0
        # barang hampir sewarna dengan latar -> pakai batas lebih rendah
        thr = sensitivity * (0.4 if low_contrast else 1.0)
        if calibrating:
            if now - calib_start >= CALIBRATE_SECONDS:
                reference = prepare(gray[y:y + h, x:x + w])
                ref_mean = float(gray.mean())
                item_mask, low_contrast = build_item_mask(reference)
                if low_contrast:
                    print("Peringatan: barang kurang kontras dengan latar, deteksi kurang akurat.")
                calibrating = False
                change_since = None
        else:
            gain = ref_mean / max(float(gray.mean()), 1.0)
            current = prepare(gray[y:y + h, x:x + w], gain)
            gone = np.abs(current - reference) > PIXEL_DIFF
            changed = float(gone[item_mask].mean())   # % piksel BARANG yang sudah hilang
            if not stolen:
                if changed > thr:
                    if change_since is None:
                        change_since = now
                    if now - change_since >= CONFIRM_SECONDS:
                        stolen = True
                        siren.start()
                        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        cv2.imwrite(os.path.join(OUT_DIR, f"curi_{stamp}.jpg"), frame)
                        path = os.path.join(OUT_DIR, f"curi_{stamp}.mp4")
                        writer = cv2.VideoWriter(
                            path, cv2.VideoWriter_fourcc(*"mp4v"), FPS_RECORD, (fw, fh)
                        )
                        for f in buffer:
                            writer.write(f)
                        record_until = now + RECORD_AFTER
                        print(f"BARANG HILANG! Bukti disimpan di {path}")
                else:
                    change_since = None
            if writer is not None:
                writer.write(frame)
                if now >= record_until:
                    writer.release()
                    writer = None

        # ---------- tingkat peringatan
        prev_level = level
        if calibrating:
            level = 0
        elif stolen:
            level = 3
        elif hand_inner:
            level = 2
        elif hand_outer:
            level = 1
        else:
            level = 0

        if level > prev_level:
            last_alert = 0.0  # naik tingkat -> langsung beri tahu
        if level in (1, 2) and now - last_alert > ALERT_COOLDOWN:
            text = TEXT_LEVEL1 if level == 1 else TEXT_LEVEL2
            if voice.say(text):
                last_alert = now
                if level == 1:
                    beep_pattern([(1000, 120)])
                else:
                    beep_pattern([(1400, 150), (1400, 150)])
        if level == 3 and now - last_stolen_voice > 4.0:
            if voice.say(TEXT_STOLEN):
                last_stolen_voice = now

        # ---------- tampilan
        if calibrating:
            color = GRAY
            label = f"MENYIAPKAN... jauhkan tangan ({int(CALIBRATE_SECONDS - (now - calib_start)) + 1})"
        elif level == 0:
            color, label = GREEN, "SIAGA - AMAN"
        elif level == 1:
            color, label = YELLOW, "ADA YANG MENDEKATI!"
        elif level == 2:
            color, label = RED, "TANGAN MAU AMBIL BARANG!"
        else:
            color, label = RED, f"BARANG HILANG! ({changed * 100:.0f}%)"

        if level == 3 and int(now * 4) % 2 == 0:
            cv2.rectangle(view, (0, 0), (fw - 1, fh - 1), RED, 12)

        ox, oy, ow, oh = outer
        cv2.rectangle(view, (ox, oy), (ox + ow, oy + oh), color, 2)
        cv2.putText(view, "ZONA PENDEKATAN", (ox + 4, max(14, oy - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
        cv2.rectangle(view, (x, y), (x + w, y + h), color, 3)
        cv2.putText(view, "TEMPAT BARANG", (x + 4, max(14, y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)

        cv2.putText(view, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        cv2.putText(view, f"Barang hilang: {changed * 100:.0f}% (alarm di {thr * 100:.0f}%)", (10, 58),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        if HAS_HANDS:
            if points:
                cv2.putText(view, "MEDIAPIPE: TANGAN TERDETEKSI", (10, 84),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, GREEN, 2)
            else:
                cv2.putText(view, "MEDIAPIPE: tidak ada tangan", (10, 84),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, GRAY, 1)
        else:
            cv2.putText(view, "MODE GERAKAN (MediaPipe tidak terpasang)", (10, 84),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, GRAY, 1)

        # batang indikator perubahan (kiri bawah)
        bar_w = int(150 * min(1.0, changed))
        cv2.rectangle(view, (10, fh - 20), (160, fh - 8), GRAY, 1)
        cv2.rectangle(view, (10, fh - 20), (10 + bar_w, fh - 8), color, -1)
        cv2.putText(view, "SPASI=reset  R=pilih ulang  +/-=sensitivitas  Q=keluar",
                    (170, fh - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        if now - last_key_time < 1.5:
            cv2.putText(view, f"Tombol terbaca: {last_key}", (fw - 220, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, YELLOW, 2)
        cv2.imshow("Smart Kamera", view)

        # ---------- tombol (huruf besar/kecil sama saja)
        key = cv2.waitKey(1) & 0xFF
        if 65 <= key <= 90:
            key += 32
        if key != 255:
            last_key = chr(key) if 32 <= key < 127 else str(key)
            last_key_time = now
        if key in (ord("q"), 27):
            break
        elif key == ord(" "):
            siren.stop()
            stolen, calibrating, calib_start = False, True, now
            change_since, level = None, 0
        elif key == ord("r"):
            siren.stop()
            frame1, new_item = select_item(cap)
            if new_item is not None:
                item = new_item
                outer = expand_rect(item, OUTER_MARGIN, fw, fh)
                prev_outer = None
            stolen, calibrating, calib_start = False, True, time.time()
            change_since, level = None, 0
        elif key in (ord("+"), ord("=")):
            sensitivity = max(0.30, sensitivity - 0.05)   # lebih sensitif
        elif key in (ord("-"), ord("_")):
            sensitivity = min(0.98, sensitivity + 0.05)   # kurang sensitif

    siren.stop()
    if writer is not None:
        writer.release()
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()