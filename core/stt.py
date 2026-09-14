"""
Движки распознавания речи (Speech-to-Text) для Anfisa XL.

Whisper  – офлайн-транскрипция через faster-whisper (с VAD-буферизацией)
Vosk     – офлайн-транскрипция потоком (легче)
"""
import json
import numpy as np


class WhisperSTT:
    """Офлайн-транскрипция на faster-whisper."""

    def __init__(self, model_name: str = "base", language: str | None = None):
        import os
        from faster_whisper import WhisperModel
        print(f"[STT] Загружаю Whisper '{model_name}'…")
        try:
            import torch
            device  = "cuda" if torch.cuda.is_available() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
        except Exception:
            device, compute = "cpu", "int8"

        try:
            self._model = WhisperModel(model_name, device=device, compute_type=compute)
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
                print(f"[STT] Модель Whisper '{model_name}' не в локальном кэше — скачиваю (один раз, нужен интернет)…")
                os.environ.pop("HF_HUB_OFFLINE",      None)
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
                os.environ.pop("HF_DATASETS_OFFLINE",  None)
                try:
                    self._model = WhisperModel(model_name, device=device, compute_type=compute)
                except Exception as _dl_err:
                    raise RuntimeError(
                        f"Не удалось скачать модель Whisper '{model_name}'.\n"
                        f"При первом запуске нужен интернет, чтобы скачать модель речи (~75–290 МБ).\n"
                        f"После первого скачивания работает полностью офлайн.\n"
                        f"Подробности: {_dl_err}"
                    ) from _dl_err
            else:
                raise

        self._language = None if (not language or language.strip().lower() == "auto") else language.strip().lower()
        print(f"[STT] Whisper '{model_name}' готов ({device})")

    def transcribe(self, audio: np.ndarray) -> str:
        """Транскрибировать numpy-массив float32 моно 16 кГц. Возвращает строку текста."""
        try:
            segments, _ = self._model.transcribe(
                audio,
                language=self._language,
                beam_size=1,                       # жадный поиск — в 2-3 раза быстрее
                best_of=1,
                condition_on_previous_text=False,  # без галлюцинаций, быстрее
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            return " ".join(s.text for s in segments).strip()
        except Exception as e:
            print(f"[STT] Ошибка транскрипции: {e}")
            raise


class VoskSTT:
    """Потоковая транскрипция на Vosk."""

    def __init__(self, model_path: str | None = None, language: str = "en-us"):
        from vosk import Model, KaldiRecognizer
        print("[STT] Загружаю модель Vosk…")
        if model_path:
            model = Model(model_path)
        else:
            lang  = language.strip().lower() if language and language.strip().lower() != "auto" else "en-us"
            model = Model(lang=lang)
        self._rec = KaldiRecognizer(model, 16000)
        print("[STT] Vosk готов.")

    def process_chunk(self, audio_bytes: bytes) -> tuple[str, bool]:
        """Подать сырые байты int16 LE PCM. Возвращает (text, is_final)."""
        if self._rec.AcceptWaveform(audio_bytes):
            result = json.loads(self._rec.Result())
            return result.get("text", ""), True
        partial = json.loads(self._rec.PartialResult())
        return partial.get("partial", ""), False
