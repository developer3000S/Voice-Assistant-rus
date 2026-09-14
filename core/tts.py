"""
Движки озвучки (Text-to-Speech) для Anfisa XL.

EdgeTTS     – бесплатный TTS от Microsoft (нужен интернет, ключ API не требуется)
Kokoro      – полностью офлайн нейросетевая озвучка (модель ~330 МБ)
ElevenLabs  – облачный API (нужен ключ API, лучшее качество)
"""
from __future__ import annotations

import asyncio
import os
import queue as _queue
import threading
from typing import Callable, Optional

import numpy as np
import sounddevice as sd



# USE_TF=0 запрещает transformers импортировать TensorFlow (экономит 4-8 с запуска).
# НЕ выставляйте USE_TORCH и USE_JAX явно — принудительные значения ломают
# ленивый загрузчик transformers в отдельных версиях, из-за чего AutoModel и
# другие классы исчезают из публичного пространства имён. Автоопределение
# работает надёжно.
os.environ.setdefault("USE_TF",                 "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


# ---------------------------------------------------------------------------
# Вспомогательные функции воспроизведения звука
# ---------------------------------------------------------------------------

def _to_numpy(samples) -> np.ndarray:
    """Преобразовать отсчёты в numpy-массив float32.

    Работает и с numpy-массивами, и с тензорами PyTorch (Kokoro >= 0.9).

    PyTorch, собранный с numpy 1.x, бросает RuntimeError('Numpy is not available'),
    когда установлена numpy 2.x. Запасной путь через .tolist() работает всегда,
    независимо от того, как соотносятся версии PyTorch и numpy.
    """
    if hasattr(samples, "detach"):                  # тензор PyTorch
        t = samples.detach().cpu().float()
        try:
            return t.numpy()                        # быстрый путь (совместимые версии)
        except RuntimeError:
            # Несовпадение версий PyTorch/numpy — конвертируем через список Python (всегда безопасно)
            return np.asarray(t.tolist(), dtype=np.float32)
    return np.asarray(samples, dtype=np.float32)


def _compress_silence(
    arr: np.ndarray,
    sample_rate: int    = 24_000,
    max_silence_ms: int = 500,    # ограничиваем паузы на знаках препинания — сохраняет естественный ритм
    threshold: float    = 0.003,  # RMS ниже этого = тишина; меньше порог = меньше обрезание
) -> np.ndarray:
    """
    Укорачивает очень длинные паузы на знаках препинания у Kokoro (1-2 с → ≤500 мс).
    Осторожные настройки сохраняют естественную интонацию; обрезаются только крайние паузы.
    """
    max_samp  = int(max_silence_ms * sample_rate / 1000)
    frame_len = 240                   # ~10 мс при 24 кГц
    out: list[np.ndarray] = []
    silent_acc = 0

    for i in range(0, len(arr), frame_len):
        chunk = arr[i : i + frame_len]
        if np.sqrt(np.mean(chunk ** 2) + 1e-12) < threshold:
            silent_acc += len(chunk)
            if silent_acc <= max_samp:
                out.append(chunk)
        else:
            silent_acc = 0
            out.append(chunk)

    return np.concatenate(out) if out else arr


def _play_np(samples, sample_rate: int) -> None:
    """Проиграть float32 моно (или стерео) звук через sounddevice.
    Принимает numpy-массивы и тензоры PyTorch.
    """
    sd.play(_to_numpy(samples), sample_rate)
    sd.wait()


def _play_audio_bytes(audio_bytes: bytes) -> None:
    """Раскодировать байты MP3/WAV/OGG и проиграть через sounddevice (используется miniaudio)."""
    import miniaudio
    decoded = miniaudio.decode(
        audio_bytes,
        output_format=miniaudio.SampleFormat.FLOAT32,
        nchannels=1,
    )
    samples = np.array(decoded.samples, dtype=np.float32)
    sd.play(samples, decoded.sample_rate)
    sd.wait()


# ---------------------------------------------------------------------------
# Движки
# ---------------------------------------------------------------------------

class EdgeTTSEngine:
    """Microsoft EdgeTTS — бесплатно, нужен интернет."""

    def __init__(self, voice: str = "en-US-GuyNeural"):
        self.voice = voice

    def speak(self, text: str) -> None:
        loop = asyncio.new_event_loop()
        try:
            audio_bytes = loop.run_until_complete(self._synth(text))
        finally:
            loop.close()
        if audio_bytes:
            _play_audio_bytes(audio_bytes)

    async def _synth(self, text: str) -> bytes:
        import edge_tts
        comm = edge_tts.Communicate(text, self.voice)
        buf  = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)


# ---------------------------------------------------------------------------
# Вспомогательный импорт Kokoro — при ошибке несовместимости версий обновляет сам
# ---------------------------------------------------------------------------

# Ошибки, которые означают, что установленный kokoro использует старые классы
# transformers (AlbertModel, AutoModel), больше не экспортируемые на верхнем уровне.
_KOKORO_COMPAT_ERRORS = ("AlbertModel", "AutoModel", "cannot import name")


def _import_kokoro_pipeline():
    """Импортировать KPipeline, при несовпадении версий автоматически обновив kokoro.

    Старый kokoro (<0.9) импортирует AlbertModel / AutoModel из transformers.
    Новые версии transformers больше не экспортируют их с верхнего уровня,
    что вызывает ImportError. В kokoro>=0.9 эти зависимости убраны.

    Когда ошибка распознана, мы:
      1. Обновляем kokoro до >=0.9 через pip (тихо, в фоне)
      2. Вычищаем устаревшие записи kokoro из sys.modules
      3. Повторяем импорт — теперь он должен получиться
    """
    import sys

    def _try_import():
        from kokoro import KPipeline  # noqa: PLC0415
        return KPipeline

    try:
        return _try_import()
    except Exception as first_err:
        err_msg = str(first_err)
        if not any(marker in err_msg for marker in _KOKORO_COMPAT_ERRORS):
            # Несвязанная ошибка (kokoro не установлен и т. п.)
            raise RuntimeError(
                f"Kokoro import failed: {first_err}\n"
                "Run: pip install kokoro>=0.9 soundfile"
            ) from first_err

        # ── Несовпадение версий: тихо обновляем kokoro и пробуем снова ──────
        print("[TTS] Обнаружено несовпадение версий Kokoro/transformers — обновляю kokoro…")
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "kokoro>=0.9",
             "--upgrade", "--quiet", "--disable-pip-version-check"],
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"Kokoro auto-upgrade failed: {stderr[:200]}\n"
                "Run manually: pip install kokoro>=0.9 soundfile"
            ) from first_err

        # Вычищаем устаревшие подмодули kokoro из кэша импорта
        stale = [k for k in sys.modules if k == "kokoro" or k.startswith("kokoro.")]
        for key in stale:
            del sys.modules[key]

        print("[TTS] Kokoro обновлён — повторяю импорт…")
        try:
            return _try_import()
        except Exception as retry_err:
            raise RuntimeError(
                f"Kokoro still broken after upgrade: {retry_err}\n"
                "Run manually: pip install --upgrade kokoro transformers"
            ) from retry_err


# Префикс голоса Kokoro → соответствие lang_code в KPipeline
_KOKORO_LANG_CODES = {
    "a": "a",   # американский английский  (af_*, am_*)
    "b": "b",   # британский английский   (bf_*, bm_*)
    "j": "j",   # японский                (jf_*, jm_*)
    "z": "z",   # китайский (путунхуа)    (zf_*, zm_*)
    "s": "s",   # испанский               (sf_*, sm_*)
    "f": "f",   # французский             (ff_*, fm_*)
    "h": "h",   # хинди                   (hf_*, hm_*)
    "i": "i",   # итальянский             (if_*, im_*)
    "p": "p",   # бразильский португальский
    "r": "r",   # русский                 (rf_*, rm_*)
    "e": "e",   # немецкий                (ef_*, em_*)
}


class KokoroTTSEngine:
    """Полностью офлайн нейросетевая озвучка Kokoro.

    Модель (~330 МБ) при первом использовании скачивается с HuggingFace,
    после чего кэшируется локально — дальнейшие запуски идут с диска.

    Стратегия прогрева: _init() выполняется синхронно в фоновом
    потоке _do_tts() (не в потоке UI).  После загрузки конвейера
    пробный прогон сразу компилирует JIT-граф PyTorch, так что
    первый настоящий вызов speak() не тратит время на компиляцию.
    """

    def __init__(self, voice: str = "af_heart", speed: float = 1.0):
        self.voice     = voice
        self.speed     = speed
        self._pipeline = None
        self._lock     = threading.Lock()
        self._init()   # блокирующий, но вызывается из фонового потока

    @property
    def _lang_code(self) -> str:
        prefix = self.voice[0].lower() if self.voice else "a"
        return _KOKORO_LANG_CODES.get(prefix, "a")

    def _init(self) -> None:
        if self._pipeline is not None:
            return

        lang = self._lang_code

        # Предпочитаем GPU — Kokoro на CUDA примерно в 10 раз быстрее, чем на CPU.
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            if device == "cpu":
                import os as _os
                n_threads = max(1, min(4, (_os.cpu_count() or 4) // 2))
                try:
                    torch.set_num_threads(n_threads)
                    torch.set_num_interop_threads(2)
                except RuntimeError:
                    pass
                print(
                    f"[TTS] Kokoro работает на CPU — чтобы речь была быстрее, поставьте PyTorch с CUDA:\n"
                    "      pip install torch --index-url https://download.pytorch.org/whl/cu118"
                )
        except Exception:
            device = "cpu"

        print(f"[TTS] Kokoro — загрузка (lang='{lang}', device='{device}')…")

        KPipeline = _import_kokoro_pipeline()

        def _create_pipeline():
            try:
                return KPipeline(lang_code=lang, device=device)
            except TypeError:
                return KPipeline(lang_code=lang)   # старая сборка — нет параметра device

        try:
            self._pipeline = _create_pipeline()
        except Exception as _first_err:
            # Флаг офлайн-режима стоит, но модель ещё не в кэше → снимаем флаги
            # и один раз скачиваем.
            # Ключевые слова покрывают несколько вариантов сообщений об ошибке
            # huggingface_hub в разных версиях.
            _e = str(_first_err).lower()
            _offline_keywords = (
                "offline", "not found", "cache", "localentry",
                "does not exist", "outgoing", "local_files_only",
            )
            if any(k in _e for k in _offline_keywords):
                print("[TTS] Модели Kokoro нет в локальном кэше — скачиваю (один раз, нужен интернет)…")
                os.environ.pop("HF_HUB_OFFLINE",      None)
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
                os.environ.pop("HF_DATASETS_OFFLINE",  None)
                try:
                    self._pipeline = _create_pipeline()
                except Exception as _dl_err:
                    raise RuntimeError(
                        f"Не удалось скачать модель Kokoro.\n"
                        f"При первом запуске нужен интернет, чтобы скачать модель голоса (~330 МБ).\n"
                        f"После первого скачивания работает полностью офлайн.\n"
                        f"Совет: если интернета нет, переключитесь на EdgeTTS (бесплатно, без скачивания) в панели настройки.\n"
                        f"Подробности: {_dl_err}"
                    ) from _dl_err
            else:
                raise

        print("[TTS] Kokoro компилируется (только в первый раз)…")
        # Прогрев: компилирует JIT-граф PyTorch, чтобы первый настоящий speak() был мгновенным.
        try:
            for _ in self._pipeline("hello", voice=self.voice, speed=self.speed):
                pass
            print("[TTS] Kokoro готов.")
        except Exception as e:
            print(f"[TTS] Предупреждение прогрева Kokoro: {e}")

    def speak(self, text: str) -> None:
        with self._lock:
            if self._pipeline is None:
                self._init()

        # ── Одновременные синтез и воспроизведение ──────────────────────────
        # Kokoro выдаёт куски аудио лениво. Без потоков у нас:
        #   синтез куска N → играть N → синтез N+1 → играть N+1 …
        # В паре «производитель/потребитель» кусок N+1 синтезируется ПОКА играет
        # кусок N, что сокращает ощущаемую задержку на длительность воспроизведения
        # всех кусков, кроме последнего (обычно 1-3 с на ответах из нескольких фраз).
        audio_q: "_queue.Queue[np.ndarray | None]" = _queue.Queue(maxsize=4)
        synth_error: list[Exception] = []

        def _synth():
            try:
                for _, _, audio in self._pipeline(text, voice=self.voice, speed=self.speed):
                    if audio is not None:
                        arr = _to_numpy(audio)
                        arr = _compress_silence(arr)
                        if arr.size > 0:
                            audio_q.put(arr)          # блокируется, если проигрыватель отстал (обратное давление)
            except Exception as exc:
                synth_error.append(exc)
            finally:
                audio_q.put(None)                     # страж-маркер → проигрыватель завершается

        synth_thread = threading.Thread(target=_synth, daemon=True)
        synth_thread.start()

        # Проигрыватель работает в этом потоке, чтобы sd.wait() не блокировал поток синтеза.
        while True:
            arr = audio_q.get()
            if arr is None:
                break
            _play_np(arr, 24000)

        synth_thread.join()

        if synth_error:
            raise synth_error[0]


class ElevenLabsTTSEngine:
    """Облачная озвучка ElevenLabs — нужен ключ API."""

    def __init__(self, api_key: str, voice_id: str = "pNInz6obpgDQGcFmaJgB"):
        self.api_key  = api_key
        self.voice_id = voice_id

    def speak(self, text: str) -> None:
        import requests
        headers = {
            "xi-api-key":   self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "text":     text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}",
            json=payload, headers=headers, timeout=30,
        )
        resp.raise_for_status()
        _play_audio_bytes(resp.content)


# ---------------------------------------------------------------------------
# Потокобезопасная обёртка проигрывателя
# ---------------------------------------------------------------------------

class TTSPlayer:
    """
    Оборачивает любой *Engine. Отдаёт блокирующий speak(), который нужно
    вызывать из отдельного фонового потока.
    """

    def __init__(self, engine):
        self._engine  = engine
        self._playing = False
        self._lock    = threading.Lock()

    @property
    def is_playing(self) -> bool:
        return self._playing

    def speak(
        self,
        text:     str,
        on_start: Optional[Callable] = None,
        on_done:  Optional[Callable] = None,
    ) -> None:
        """Синтезировать и произнести текст. БЛОКИРУЮЩИЙ — вызывать из отдельного потока."""
        try:
            with self._lock:
                self._playing = True
            if on_start:
                on_start()
            self._engine.speak(text)
        except Exception as e:
            print(f"[TTS] Ошибка: {e}")
        finally:
            with self._lock:
                self._playing = False
            if on_done:
                on_done()

    def stop(self) -> None:
        sd.stop()
        with self._lock:
            self._playing = False


# ---------------------------------------------------------------------------
# Фабрика
# ---------------------------------------------------------------------------

def create_tts_player(config: dict) -> TTSPlayer:
    engine_name = config.get("tts_engine", "edgetts").lower()
    if engine_name == "kokoro":
        voice  = config.get("tts_voice", "af_heart")
        speed  = float(config.get("tts_speed", 1.0))
        engine = KokoroTTSEngine(voice=voice, speed=speed)
    elif engine_name == "elevenlabs":
        api_key  = config.get("elevenlabs_api_key", "")
        voice_id = config.get("tts_voice", "pNInz6obpgDQGcFmaJgB")
        engine   = ElevenLabsTTSEngine(api_key=api_key, voice_id=voice_id)
    else:   # edgetts (по умолчанию)
        voice  = config.get("tts_voice", "en-US-GuyNeural")
        engine = EdgeTTSEngine(voice=voice)
    return TTSPlayer(engine)
