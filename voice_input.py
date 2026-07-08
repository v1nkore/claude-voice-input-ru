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
#   VOICE_HOTKEY = горячая клавиша (по умолчанию f9)
#   VOICE_VOLUME = громкость звуковых сигналов 0.0–1.0 (по умолчанию 0.2)
#   VOICE_TARGET_WINDOW = подстрока заголовка окна, куда всегда вставлять текст
#       (по умолчанию «Claude»). Пусто → вставлять в то окно, что активно при остановке.
MODEL_SIZE = os.environ.get("VOICE_MODEL", "small")
TARGET_WINDOW = os.environ.get("VOICE_TARGET_WINDOW", "Claude")
LANGUAGE = os.environ.get("VOICE_LANG", "ru")
HOTKEY = os.environ.get("VOICE_HOTKEY", "f9")
SOUND_VOLUME = float(os.environ.get("VOICE_VOLUME", "0.2"))
SAMPLE_RATE = 16000

recording = False
frames = []
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

# До-мажорные интервалы — привычные «уведомительные» звуки
SND_START = _make_chime([523.25, 783.99])          # C5 -> G5, вверх: запись пошла
SND_STOP = _make_chime([783.99, 523.25])           # G5 -> C5, вниз: записал, думаю
SND_DONE = _make_chime([659.25, 1046.5], 0.09)     # E5 -> C6: текст вставлен
SND_EMPTY = _make_chime([311.13, 233.08], 0.12)    # мягкий низкий: ничего не распознано


def play(sound):
    try:
        sd.play(sound, SAMPLE_RATE)
    except Exception:
        pass  # звук — не критичен, работу не прерываем


# ---------- Запись и распознавание ----------

print(f"Загрузка модели Whisper '{MODEL_SIZE}' (при первом запуске скачивается)...")
from faster_whisper import WhisperModel
# cpu_threads=0 → ctranslate2 сам берёт число физических ядер; задаём явно все ядра
# для ускорения тяжёлой large-v3-turbo на CPU.
_cpu_threads = int(os.environ.get("VOICE_CPU_THREADS", str(os.cpu_count() or 0)))
model = WhisperModel(
    MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=_cpu_threads
)
# распознавание — по одному прогону за раз: две тяжёлые транскрипции внахлёст
# конкурируют за CPU и тормозят обе
transcribe_lock = threading.Lock()
print(f"Модель загружена (cpu_threads={_cpu_threads}).")
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


def transcribe_and_paste(audio: np.ndarray, target_hwnd=None):
    if len(audio) < SAMPLE_RATE // 2:  # меньше полсекунды — игнорируем
        print("Слишком короткая запись, пропускаю.")
        play(SND_EMPTY)
        return
    print("Распознаю...")
    with transcribe_lock:  # не даём двум прогонам конкурировать за CPU
        segments, _ = model.transcribe(
            audio, language=LANGUAGE, vad_filter=True, beam_size=5
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
    if not text:
        print("Речь не распознана.")
        play(SND_EMPTY)
        return
    print(f"Распознано: {text}")
    paste_text(text, target_hwnd)
    play(SND_DONE)


_last_toggle = 0.0  # антидребезг: повторный триггер не должен дёргать старт/стоп дважды
_toggle_lock = threading.Lock()


def toggle():
    global recording, frames, _last_toggle
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


def _toggle_body():
    global recording, frames
    if not recording:
        frames = []
        while not audio_queue.empty():
            audio_queue.get_nowait()
        recording = True
        play(SND_START)
        print(f"Запись... ({HOTKEY} — остановить)")
    else:
        recording = False
        # Ищем окно Claude по заголовку и всегда шлём текст туда — независимо от
        # того, где сейчас курсор. Если не нашли (Claude закрыт) — откат на активное окно.
        target_hwnd = find_target_window(TARGET_WINDOW) or get_active_window()
        title = _window_title(target_hwnd)
        print(f"Целевое окно: {title or '<без заголовка>'} (hwnd={target_hwnd})")
        play(SND_STOP)
        chunks = []
        while not audio_queue.empty():
            chunks.append(audio_queue.get_nowait())
        if chunks:
            audio = np.concatenate(chunks).flatten().astype(np.float32)
            threading.Thread(
                target=transcribe_and_paste, args=(audio, target_hwnd), daemon=True
            ).start()
        else:
            print("Пустая запись.")
            play(SND_EMPTY)


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
