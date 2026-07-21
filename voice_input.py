# -*- coding: utf-8 -*-
"""
Голосовой ввод на русском для любого окна Windows (включая Claude Code).

Как пользоваться:
  1. Запустите скрипт (или start_voice.bat / VoiceInputRU.exe). Он висит в фоне.
  2. Нажмите F9 — начнётся запись (мягкий восходящий звук).
  3. Говорите по-русски.
  4. Нажмите F9 ещё раз — запись остановится (нисходящий звук),
     текст распознается и вставится в активное окно (Ctrl+V).
     Текст также остаётся в буфере обмена — если вставка не сработала,
     нажмите Ctrl+V вручную.

Распознавание полностью локальное (faster-whisper), интернет нужен
только один раз — для скачивания модели.
"""

import os
import queue
import sys
import threading
import time

# Если запущены без консоли (windowed exe), sys.stdout/stderr == None и любой
# print() уронит программу. Перенаправляем вывод в лог-файл рядом с exe —
# и работа не падает, и диагностика сохраняется. При запуске из start_voice.bat
# консоль на месте (stdout не None), поведение не меняется.
if sys.stdout is None or sys.stderr is None:
    try:
        _base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else __file__)
        _log = open(os.path.join(_base, "voice_input.log"), "a", encoding="utf-8", buffering=1)
    except Exception:
        _log = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log

# ---------- Защита от второго экземпляра ----------
# Если запущены два экземпляра (например, вручную + из автозагрузки), каждый
# ловит F9 и каждый вставляет текст — получаются дубли. Мьютекс живёт, пока
# жив процесс, второй экземпляр молча выходит.
if sys.platform == "win32":
    import ctypes as _ctypes
    _kernel32 = _ctypes.windll.kernel32
    _kernel32.CreateMutexW(None, False, "Local\\VoiceInputRU_SingleInstance")
    if _kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("VoiceInputRU уже запущен — второй экземпляр не нужен, выхожу.")
        sys.exit(0)

import numpy as np
import sounddevice as sd
import keyboard
import pyperclip

# ---------- Возврат фокуса на нужное окно (Win32) ----------
# Запись останавливается в окне Claude, а распознавание идёт несколько секунд.
# За это время можно уйти в Телеграм/браузер — поэтому запоминаем окно в момент
# остановки и перед вставкой возвращаем фокус именно на него.
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    # Снимаем системную блокировку смены переднего окна: без этого Windows
    # игнорирует SetForegroundWindow от фонового процесса (просто мигает иконкой).
    try:
        SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
        SPIF_SENDCHANGE = 0x02
        _user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, 0, SPIF_SENDCHANGE)
    except Exception:
        pass

    _own_pid = _kernel32.GetCurrentProcessId()
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    # Виртуальные коды боковых модификаторов — их различает только низкоуровневый
    # хук по vkCode (библиотека keyboard путает левый/правый Ctrl).
    VK_SIDE_KEYS = {
        "right ctrl": 0xA3, "left ctrl": 0xA2,
        "right shift": 0xA1, "left shift": 0xA0,
        "right alt": 0xA5, "left alt": 0xA4,
    }

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    _LL_KBD_PROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )
    _user32.CallNextHookEx.restype = ctypes.c_ssize_t
    _user32.SetWindowsHookExW.restype = wintypes.HHOOK
    _hook_ref = None  # держим ссылку на callback, иначе GC его снесёт

    def get_active_window():
        return _user32.GetForegroundWindow()

    def _window_title(hwnd):
        length = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _window_pid(hwnd):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def find_target_window(substr):
        """Ищет видимое окно верхнего уровня, в заголовке которого есть substr
        (например «Claude»). Свою консоль пропускаем по PID — иначе путь к папке
        «Улучшения claude» сам совпадёт с подстрокой. Возвращает hwnd или None."""
        if not substr:
            return None
        substr_low = substr.lower()
        matches = []

        def _cb(hwnd, lparam):
            if not _user32.IsWindowVisible(hwnd):
                return True
            if _window_pid(hwnd) == _own_pid:  # наше собственное окно (консоль)
                return True
            title = _window_title(hwnd)
            if title and substr_low in title.lower():
                matches.append(hwnd)
            return True

        _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
        if not matches:
            return None
        # предпочитаем не свёрнутое окно
        for hwnd in matches:
            if not _user32.IsIconic(hwnd):
                return hwnd
        return matches[0]

    def _alt_nudge():
        """Короткое «нажатие» Alt через keybd_event. Windows разрешает
        SetForegroundWindow только процессу, который недавно генерировал ввод —
        этим нажатием мы удовлетворяем условие."""
        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x02
        try:
            _user32.keybd_event(VK_MENU, 0, 0, 0)
            _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            pass

    def focus_window(hwnd):
        """Возвращает фокус на hwnd. SetForegroundWindow на Windows капризен —
        комбинируем снятие блокировки, Alt-nudge, AttachThreadInput и ретраи."""
        if not hwnd or not _user32.IsWindow(hwnd):
            print("focus_window: окно уже не существует")
            return False
        if _user32.GetForegroundWindow() == hwnd:
            return True
        SW_RESTORE = 9
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW_RESTORE)
        cur_thread = _kernel32.GetCurrentThreadId()
        for attempt in range(3):
            fg = _user32.GetForegroundWindow()
            fg_thread = _user32.GetWindowThreadProcessId(fg, None)
            tgt_thread = _user32.GetWindowThreadProcessId(hwnd, None)
            attached = []
            for t in (fg_thread, tgt_thread):
                if t and t != cur_thread:
                    _user32.AttachThreadInput(cur_thread, t, True)
                    attached.append(t)
            _alt_nudge()
            _user32.BringWindowToTop(hwnd)
            _user32.ShowWindow(hwnd, SW_RESTORE)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetActiveWindow(hwnd)
            _user32.SetFocus(hwnd)
            for t in attached:
                _user32.AttachThreadInput(cur_thread, t, False)
            if _user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.08)
        ok = _user32.GetForegroundWindow() == hwnd
        if not ok:
            print(f"focus_window: не удалось (сейчас впереди '{_window_title(_user32.GetForegroundWindow())}')")
        return ok
else:
    def get_active_window():
        return None

    def _window_title(hwnd):
        return ""

    def find_target_window(substr):
        return None

    def focus_window(hwnd):
        return False

# Настройки можно менять через переменные окружения, без правки кода/пересборки exe:
#   VOICE_MODEL  = tiny / base / small / medium / large-v3-turbo
#       (по умолчанию large-v3-turbo — лучшее качество для русского;
#        на слабой машине поставь VOICE_MODEL=small)
#   VOICE_DEVICE = auto / cuda / cpu (по умолчанию auto: GPU, если есть, иначе CPU).
#       Для GPU нужны cuBLAS/cuDNN: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
#   VOICE_BEAM   = ширина beam search (по умолчанию 1 — greedy, самый быстрый)
#   VOICE_CHUNK_SEC = длина куска (сек), распознаваемого фоном прямо во время записи;
#       к остановке почти всё уже распознано, ждать остаётся только хвост.
#       0 — отключить (распознавать всё целиком после остановки). По умолчанию 12.
#   VOICE_HOTKEY = горячая клавиша (по умолчанию f9)
#   VOICE_VOLUME = громкость звуковых сигналов 0.0–1.0 (по умолчанию 0.2)
#   VOICE_TARGET_WINDOW = подстрока заголовка окна, куда всегда вставлять текст
#       (по умолчанию «Claude»). Пусто → вставлять в то окно, что активно при остановке.
#   VOICE_MUTE_DISCORD = 1/0 — глушить микрофон в Discord на время записи (по умолчанию 1).
#       Требует: в Discord (Настройки → Голос и видео → Горячие клавиши) перевязать
#       «Переключить микрофон» с F9 на VOICE_DISCORD_MUTE_KEY — иначе F9 достаётся
#       только одной программе (RegisterHotKey резервирует клавишу эксклюзивно).
#   VOICE_DISCORD_MUTE_KEY = клавиша для Discord-мьюта (по умолчанию scroll lock)
MODEL_SIZE = os.environ.get("VOICE_MODEL", "large-v3-turbo")
TARGET_WINDOW = os.environ.get("VOICE_TARGET_WINDOW", "Claude")
LANGUAGE = os.environ.get("VOICE_LANG", "ru")
HOTKEY = os.environ.get("VOICE_HOTKEY", "f9")
SOUND_VOLUME = float(os.environ.get("VOICE_VOLUME", "0.2"))
MUTE_DISCORD = os.environ.get("VOICE_MUTE_DISCORD", "1").strip().lower() not in ("0", "false", "no")
DISCORD_MUTE_KEY = os.environ.get("VOICE_DISCORD_MUTE_KEY", "f6")
SAMPLE_RATE = 16000

recording = False
audio_queue = queue.Queue()


# ---------- Мягкие звуковые сигналы (синус с плавным затуханием, без писка) ----------

def _make_chime(notes, note_len=0.10, note_gap=0.07, volume=SOUND_VOLUME):
    """Собирает короткую мелодию из затухающих «колокольчиков»."""
    tail = 0.35  # хвост затухания последней ноты
    total = note_gap * (len(notes) - 1) + note_len + tail
    buf = np.zeros(int(SAMPLE_RATE * total), dtype=np.float32)
    for i, freq in enumerate(notes):
        dur = note_len + tail
        t = np.linspace(0, dur, int(SAMPLE_RATE * dur), False)
        # основной тон + лёгкая вторая гармоника — звучит как мягкий колокольчик
        wave = np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(4 * np.pi * freq * t)
        attack = np.clip(t / 0.012, 0, 1)          # плавное нарастание 12 мс
        decay = np.exp(-t / (note_len * 1.6))      # естественное затухание
        wave = (wave * attack * decay).astype(np.float32)
        start = int(SAMPLE_RATE * note_gap * i)
        buf[start:start + len(wave)] += wave[: len(buf) - start]
    peak = np.max(np.abs(buf))
    if peak > 0:
        buf = buf / peak * volume
    return buf

# Старт и стоп сделаны контрастными (регистр + длина + число нот),
# зеркальные мелодии из одних нот на слух не различались.
SND_START = _make_chime([1046.5], note_len=0.07)             # C6: один короткий высокий «динь»
SND_STOP = _make_chime([392.0, 261.63], note_len=0.16)       # G4 -> C4: два долгих низких
SND_DONE = _make_chime([659.25, 1046.5], 0.09)     # E5 -> C6: текст вставлен
SND_EMPTY = _make_chime([311.13, 233.08], 0.12)    # мягкий низкий: ничего не распознано


def play(sound):
    try:
        sd.play(sound, SAMPLE_RATE)
    except Exception:
        pass  # звук — не критичен, работу не прерываем


# ---------- Запись и распознавание ----------

print(f"Загрузка модели Whisper '{MODEL_SIZE}' (при первом запуске скачивается)...")

def _add_nvidia_dll_dirs():
    """cuBLAS/cuDNN, поставленные через pip (nvidia-cublas-cu12, nvidia-cudnn-cu12),
    лежат в site-packages/nvidia/*/bin — ctranslate2 ищет их через PATH."""
    try:
        import nvidia  # noqa: F401 — пакет-неймспейс, есть только если стоят cu12-колёса
    except ImportError:
        return
    import nvidia as _nv
    for root in _nv.__path__:
        for sub in os.listdir(root):
            bin_dir = os.path.join(root, sub, "bin")
            if os.path.isdir(bin_dir):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                try:
                    os.add_dll_directory(bin_dir)
                except (AttributeError, OSError):
                    pass

from faster_whisper import WhisperModel
# cpu_threads=0 → ctranslate2 сам берёт число физических ядер; задаём явно все ядра
# для ускорения тяжёлой large-v3-turbo на CPU.
_cpu_threads = int(os.environ.get("VOICE_CPU_THREADS", str(os.cpu_count() or 0)))

def _load_model():
    """GPU (float16), если есть CUDA; иначе CPU (int8). VOICE_DEVICE=cpu — принудительно CPU.
    Ошибка всплывает только на первом прогоне, поэтому пробный transcribe обязателен —
    заодно прогревает модель, и первая реальная фраза не ждёт инициализации."""
    want = os.environ.get("VOICE_DEVICE", "auto").strip().lower()
    if want != "cpu":
        try:
            _add_nvidia_dll_dirs()
            m = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")
            list(m.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), language=LANGUAGE)[0])
            return m, "cuda"
        except Exception as e:
            if want == "cuda":
                raise
            print(f"CUDA недоступна ({type(e).__name__}), работаю на CPU.")
    m = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=_cpu_threads)
    list(m.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), language=LANGUAGE)[0])
    return m, "cpu"

model, _device = _load_model()
# распознавание — по одному прогону за раз: две тяжёлые транскрипции внахлёст
# конкурируют за CPU и тормозят обе
transcribe_lock = threading.Lock()
print(f"Модель загружена ({'GPU' if _device == 'cuda' else f'CPU, потоков={_cpu_threads}'}).")
print(f"Нажмите {HOTKEY.upper()} — начать запись, {HOTKEY.upper()} ещё раз — распознать и вставить.")
print("Если текст не вставился сам — он уже в буфере обмена, нажмите Ctrl+V.")
print("Выход: Ctrl+C в этом окне.")


def audio_callback(indata, frame_count, time_info, status):
    if recording:
        audio_queue.put(indata.copy())


def paste_text(text, target_hwnd=None):
    pyperclip.copy(text)
    time.sleep(0.15)  # даём буферу обмена обновиться
    # возвращаем фокус на окно, в котором остановили запись (Claude),
    # даже если сейчас активно другое окно (Телеграм, браузер и т.д.)
    if target_hwnd:
        if focus_window(target_hwnd):
            time.sleep(0.12)  # даём окну реально активироваться перед вставкой
        else:
            print("Не удалось вернуть фокус на исходное окно — текст в буфере обмена, вставьте Ctrl+V вручную.")
            play(SND_EMPTY)
            return
    # отпускаем модификаторы, если что-то зажато — иначе Ctrl+V превращается в другую комбинацию
    for key in ("ctrl", "shift", "alt", "windows"):
        try:
            keyboard.release(key)
        except Exception:
            pass
    time.sleep(0.05)
    keyboard.send("ctrl+v")


# Типовые галлюцинации Whisper на хвостовой тишине (обучен на субтитрах —
# на паузе «дописывает» их служебные фразы). Сравнение без пунктуации/регистра.
_HALLUCINATIONS = {
    "спасибо", "спасибо за просмотр", "спасибо за внимание", "благодарю за внимание",
    "продолжение следует", "до встречи", "до новых встреч", "пока",
    "субтитры сделал dimatorzok", "субтитры делал dimatorzok", "субтитры создавал dimatorzok",
    "редактор субтитров ас корректор ав", "titry",
}
_NORM_RE = None  # компилируется при первом вызове, чтобы не тащить re наверх


def _is_hallucination(seg) -> bool:
    """Отсекает сегмент-галлюцинацию: типовая фраза из чёрного списка либо
    сегмент, в котором модель сама почти уверена, что речи не было."""
    global _NORM_RE
    if _NORM_RE is None:
        import re
        _NORM_RE = re.compile(r"[^\wа-яё ]+", re.IGNORECASE)
    norm = _NORM_RE.sub("", seg.text.lower()).strip()
    if norm in _HALLUCINATIONS:
        return True
    return seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.8


def _transcribe_text(audio: np.ndarray) -> str:
    """Распознаёт кусок аудио, отбрасывая сегменты-галлюцинации. Возвращает текст ('' — пусто)."""
    with transcribe_lock:  # не даём двум прогонам конкурировать за CPU/GPU
        segments, _ = model.transcribe(
            audio,
            language=LANGUAGE,
            vad_filter=True,
            # VAD режет тишину до/после речи — именно на ней рождаются «спасибо за просмотр»
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 250},
            # greedy вместо beam=5: для диктовки разницы нет, скорость выше
            beam_size=int(os.environ.get("VOICE_BEAM", "1")),
            # не подмешивать уже распознанный текст в контекст — источник зацикливаний
            condition_on_previous_text=False,
        )
        parts = []
        for seg in segments:
            if _is_hallucination(seg):
                print(f"  (отброшен сегмент-галлюцинация: {seg.text.strip()!r})")
                continue
            parts.append(seg.text.strip())
    return " ".join(parts).strip()


# ---------- Чанковая транскрипция: распознаём фоном прямо во время записи ----------
# Энкодер Whisper — узкое место на CPU (~0.7 c на 1 c аудио для large-v3-turbo).
# Поэтому длинный монолог режем на куски по паузам и распознаём их, пока запись idет:
# к остановке готов почти весь текст, ждать остаётся только хвост.

CHUNK_SEC = float(os.environ.get("VOICE_CHUNK_SEC", "12"))
_MIN_CHUNK_SEC = 4.0  # раньше этой границы паузу не ищем — куски короче бьют по качеству


class _RecordingSession:
    """Одна запись: копит аудио из audio_queue, фоном распознаёт готовые куски."""

    def __init__(self):
        self.parts: list[str] = []
        self.buf = np.zeros(0, dtype=np.float32)
        self.total_samples = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _split_point(self) -> int:
        """Индекс разреза первых CHUNK_SEC секунд буфера: последняя пауза
        (тихие 250 мс), чтобы не резать слово посередине. Нет паузы — режем как есть."""
        end = int(CHUNK_SEC * SAMPLE_RATE)
        start = int(_MIN_CHUNK_SEC * SAMPLE_RATE)
        seg = np.abs(self.buf[:end])
        win = int(0.25 * SAMPLE_RATE)
        # скользящее среднее |x| окном 250 мс через кумулятивную сумму — O(n)
        c = np.cumsum(seg, dtype=np.float64)
        rolling = (c[win:] - c[:-win]) / win
        thr = max(1e-4, 0.15 * float(seg.mean()))
        quiet = np.where(rolling[start:] < thr)[0]
        if len(quiet):
            return start + int(quiet[-1]) + win // 2
        return end

    def _append_queue_item(self, data) -> None:
        piece = data.flatten().astype(np.float32)
        self.buf = np.concatenate([self.buf, piece])
        self.total_samples += len(piece)

    def _run(self):
        while True:
            try:
                self._append_queue_item(audio_queue.get(timeout=0.2))
            except queue.Empty:
                if not recording:
                    break  # запись остановлена и очередь пуста — дальше ничего не придёт
                continue
            if CHUNK_SEC > 0 and recording and len(self.buf) >= CHUNK_SEC * SAMPLE_RATE:
                cut = self._split_point()
                chunk, self.buf = self.buf[:cut], self.buf[cut:]
                text = _transcribe_text(chunk)
                if text:
                    self.parts.append(text)
                    print(f"  (фоном распознано: …{text[-60:]})")
        if len(self.buf):
            text = _transcribe_text(self.buf)
            if text:
                self.parts.append(text)

    def finish_and_paste(self, target_hwnd=None):
        """Вызывается после остановки записи: дожидается хвоста и вставляет текст."""
        t0 = time.monotonic()
        self.thread.join()
        if self.total_samples < SAMPLE_RATE // 2:  # меньше полсекунды — игнорируем
            print("Слишком короткая запись, пропускаю.")
            play(SND_EMPTY)
            return
        text = " ".join(self.parts).strip()
        if not text:
            print("Речь не распознана.")
            play(SND_EMPTY)
            return
        print(f"Распознано (ожидание {time.monotonic() - t0:.1f} с): {text}")
        paste_text(text, target_hwnd)
        play(SND_DONE)


# (vk, scancode) для клавиш-переключателей — им обязателен флаг KEYEVENTF_EXTENDEDKEY,
# который библиотека keyboard для этих клавиш не выставляет (проверено: без него
# keybd_event уходит, но физическое состояние переключателя не меняется, и Discord
# такое нажатие не видит). Поэтому шлём их напрямую через user32, в обход keyboard.
_TOGGLE_VK = {
    "scroll lock": (0x91, 0x46),
    "num lock": (0x90, 0x45),
    "pause": (0x13, 0x45),
}


def toggle_discord_mute():
    """Шлёт DISCORD_MUTE_KEY, чтобы Discord (перевязанный на эту клавишу в своих
    настройках) переключил мьют синхронно со стартом/стопом записи. F9 при этом
    RegisterHotKey держит эксклюзивно только за собой — Discord его больше не видит,
    поэтому мьют развязан на отдельную клавишу."""
    if not MUTE_DISCORD:
        return
    key = DISCORD_MUTE_KEY.strip().lower()
    try:
        if sys.platform == "win32" and key in _TOGGLE_VK:
            vk, scan = _TOGGLE_VK[key]
            KEYEVENTF_EXTENDEDKEY = 0x0001
            KEYEVENTF_KEYUP = 0x0002
            _user32.keybd_event(vk, scan, KEYEVENTF_EXTENDEDKEY, 0)
            _user32.keybd_event(vk, scan, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        else:
            keyboard.send(DISCORD_MUTE_KEY)
    except Exception as e:
        print(f"Не удалось переключить мьют в Discord: {e}")


_last_toggle = 0.0  # антидребезг: повторный триггер не должен дёргать старт/стоп дважды
_toggle_lock = threading.Lock()


def toggle():
    global recording, _last_toggle
    if not _toggle_lock.acquire(blocking=False):
        return  # предыдущий toggle ещё выполняется — пропускаем
    try:
        now = time.monotonic()
        if now - _last_toggle < 0.4:
            return  # слишком быстрый повторный триггер — игнорируем
        _last_toggle = now
        _toggle_body()
    finally:
        _toggle_lock.release()


_session = None  # текущая _RecordingSession


def _toggle_body():
    global recording, _session
    if not recording:
        while not audio_queue.empty():
            audio_queue.get_nowait()
        recording = True
        _session = _RecordingSession()
        toggle_discord_mute()
        play(SND_START)
        print(f"Запись... ({HOTKEY} — остановить)")
    else:
        recording = False
        toggle_discord_mute()
        # Ищем окно Claude по заголовку и всегда шлём текст туда — независимо от
        # того, где сейчас курсор. Если не нашли (Claude закрыт) — откат на активное окно.
        target_hwnd = find_target_window(TARGET_WINDOW) or get_active_window()
        title = _window_title(target_hwnd)
        print(f"Целевое окно: {title or '<без заголовка>'} (hwnd={target_hwnd})")
        play(SND_STOP)
        print("Распознаю...")
        sess, _session = _session, None
        threading.Thread(
            target=sess.finish_and_paste, args=(target_hwnd,), daemon=True
        ).start()


def _parse_hotkey(spec):
    """Преобразует строку вроде 'f9' или 'ctrl+alt+v' в (модификаторы, vk-код)
    для RegisterHotKey. Возвращает None, если клавишу не удалось разобрать."""
    MODS = {"alt": 1, "ctrl": 2, "control": 2, "shift": 4, "win": 8, "windows": 8}
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        if p not in MODS:
            return None
        mods |= MODS[p]
    key = parts[-1]
    vk = None
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit():
        n = int(key[1:])
        if 1 <= n <= 24:
            vk = 0x70 + (n - 1)
    if vk is None:
        return None
    return mods, vk


def _fallback_keyboard_loop():
    print(f"Горячая клавиша '{HOTKEY}' — через keyboard-хук.")
    print("Если не срабатывает поверх окна, запущенного от администратора — запустите этот .exe тоже от имени администратора.")
    keyboard.add_hotkey(HOTKEY, toggle)
    keyboard.wait()


def _run_lowlevel_hook(target_vk):
    """Свой WH_KEYBOARD_LL-хук: срабатывает строго на нужный vkCode (различает
    левый/правый Ctrl) по фронту «отпущено→нажато». toggle запускаем в отдельном
    потоке, чтобы не задерживать обработку клавиатуры системой."""
    global _hook_ref
    WH_KEYBOARD_LL = 13
    WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
    WM_KEYUP, WM_SYSKEYUP = 0x0101, 0x0105
    pressed = {"down": False}

    def _proc(nCode, wParam, lParam):
        if nCode == 0:
            vk = _KBDLLHOOKSTRUCT.from_address(lParam).vkCode
            if vk == target_vk:
                if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    if not pressed["down"]:
                        pressed["down"] = True
                        threading.Thread(target=toggle, daemon=True).start()
                elif wParam in (WM_KEYUP, WM_SYSKEYUP):
                    pressed["down"] = False
        return _user32.CallNextHookEx(None, nCode, wParam, lParam)

    _hook_ref = _LL_KBD_PROC(_proc)  # держим ссылку от GC
    hmod = _kernel32.GetModuleHandleW(None)
    handle = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_ref, hmod, 0)
    if not handle:
        print("Не удалось поставить низкоуровневый хук, откат на keyboard.")
        _fallback_keyboard_loop()
        return
    print(f"Горячая клавиша '{HOTKEY}' — низкоуровневый хук (различает левый/правый).")
    msg = wintypes.MSG()
    try:
        while True:
            ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
    finally:
        _user32.UnhookWindowsHookEx(handle)


def run_hotkey_loop():
    """Системная горячая клавиша через RegisterHotKey — срабатывает поверх любого
    окна (в т.ч. браузера и окон с повышенными правами), в отличие от низкоуровневого
    хука keyboard, который такие окна пропускает."""
    if sys.platform == "win32" and HOTKEY.strip().lower() in VK_SIDE_KEYS:
        # боковой модификатор (правый/левый Ctrl и т.п.) — только через свой LL-хук
        _run_lowlevel_hook(VK_SIDE_KEYS[HOTKEY.strip().lower()])
        return
    parsed = _parse_hotkey(HOTKEY) if sys.platform == "win32" else None
    if parsed is None:
        _fallback_keyboard_loop()
        return
    mods, vk = parsed
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312
    if not _user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
        print("Не удалось зарегистрировать глобальную клавишу (возможно, занята другой программой).")
        _fallback_keyboard_loop()
        return
    print(f"Глобальная клавиша {HOTKEY.upper()} зарегистрирована (RegisterHotKey).")
    msg = wintypes.MSG()
    try:
        while True:
            ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            if msg.message == WM_HOTKEY:
                toggle()
    finally:
        _user32.UnregisterHotKey(None, 1)


def main():
    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=audio_callback
    ):
        try:
            run_hotkey_loop()
        except KeyboardInterrupt:
            print("\nВыход.")
            sys.exit(0)


if __name__ == "__main__":
    main()
