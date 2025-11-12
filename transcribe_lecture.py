#!/usr/bin/env python3
"""
Программа для транскрибации аудио лекций с учетом пауз и нескольких спикеров.
"""

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Callable
import whisper
from pyannote.audio import Pipeline
import torch
from datetime import timedelta
import numpy as np
from pydub import AudioSegment
import time
import threading
import queue
import sys
import io
import os
import re
import wave
import signal
import ssl
import urllib.request

try:
    import sounddevice as sd
    import soundfile as sf
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False
    print("⚠️  Предупреждение: sounddevice не установлен. Запись с микрофона недоступна.")
    print("   Установите: pip install sounddevice soundfile")


class AudioRecorder:
    """Класс для записи аудио с микрофона или системного звука."""
    
    def __init__(self, 
                 sample_rate: int = 16000,
                 channels: int = 1,
                 device: Optional[int] = None,
                 dtype: str = 'float32'):
        """
        Инициализация рекордера.
        
        Args:
            sample_rate: Частота дискретизации (рекомендуется 16000 для Whisper)
            channels: Количество каналов (1 = моно, 2 = стерео)
            device: ID устройства для записи (None = по умолчанию)
            dtype: Тип данных аудио
        """
        if not AUDIO_AVAILABLE:
            raise ImportError("sounddevice и soundfile должны быть установлены для записи аудио")
        
        self.sample_rate = sample_rate
        self.channels = channels
        self.device = device
        self.dtype = dtype
        self.is_recording = False
        self.audio_queue = queue.Queue()
        self.recording_thread = None
        self.start_time = None
        
    def list_devices(self):
        """Выводит список доступных аудио устройств."""
        print("\nДоступные аудио устройства:")
        print(sd.query_devices())
        print()
    
    def find_system_audio_device(self):
        """
        Пытается найти устройство для записи системного звука.
        На macOS может потребоваться виртуальное устройство (например, BlackHole).
        """
        devices = sd.query_devices()
        # Ищем устройства с названиями, связанными с системным звуком
        keywords = ['blackhole', 'loopback', 'soundflower', 'virtual', 'system']
        for i, device in enumerate(devices):
            name = device.get('name', '').lower()
            if device.get('max_input_channels', 0) > 0:
                if any(keyword in name for keyword in keywords):
                    print(f"Найдено устройство для системного звука: {device['name']} (ID: {i})")
                    return i
        return None
    
    def _record_callback(self, indata, frames, time_info, status):
        """Callback функция для записи аудио."""
        if status:
            print(f"Статус записи: {status}", file=sys.stderr)
        if self.is_recording:
            self.audio_queue.put(indata.copy())
    
    def start_recording(self, output_path: Optional[str] = None):
        """
        Начинает запись аудио.
        
        Args:
            output_path: Путь для сохранения WAV файла (опционально)
        """
        if self.is_recording:
            print("⚠️  Запись уже идет!")
            return
        
        self.is_recording = True
        self.start_time = time.time()
        self.audio_queue = queue.Queue()
        self.output_path = output_path
        
        print(f"🎤 Начало записи...")
        print("   Нажмите Ctrl+C для остановки")
        
        # Запускаем поток записи
        try:
            with sd.InputStream(samplerate=self.sample_rate,
                              channels=self.channels,
                              device=self.device,
                              dtype=self.dtype,
                              callback=self._record_callback):
                self._save_audio_loop()
        except KeyboardInterrupt:
            self.stop_recording()
        except Exception as e:
            print(f"✗ Ошибка при записи: {e}")
            self.is_recording = False
    
    def _save_audio_loop(self):
        """Цикл сохранения аудио в файл."""
        if not self.output_path:
            # Если файл не указан, просто записываем в очередь
            while self.is_recording:
                time.sleep(0.1)
            return
        
        # Сохраняем аудио в файл
        with sf.SoundFile(self.output_path, mode='w', 
                         samplerate=self.sample_rate,
                         channels=self.channels,
                         subtype='PCM_16') as file:
            while self.is_recording:
                try:
                    data = self.audio_queue.get(timeout=0.5)
                    file.write(data)
                except queue.Empty:
                    continue
        
        duration = time.time() - self.start_time if self.start_time else 0
        print(f"✓ Запись завершена. Длительность: {duration:.1f} сек")
        print(f"  Файл сохранен: {self.output_path}")
    
    def stop_recording(self):
        """Останавливает запись."""
        if not self.is_recording:
            return
        self.is_recording = False
        print("\n⏹️  Остановка записи...")


try:
    import speech_recognition as sr
    SYSTEM_RECOGNIZER_AVAILABLE = True
except ImportError:
    SYSTEM_RECOGNIZER_AVAILABLE = False
    print("⚠️  Предупреждение: speech_recognition не установлен. Системное распознавание недоступно.")
    print("   Установите: pip install SpeechRecognition")

# Попытка использовать встроенный системный Speech Recognition
try:
    import subprocess
    import platform
    PLATFORM = platform.system()
    MACOS_SPEECH_AVAILABLE = (PLATFORM == "Darwin")
    WINDOWS_SPEECH_AVAILABLE = (PLATFORM == "Windows")
except:
    PLATFORM = "Unknown"
    MACOS_SPEECH_AVAILABLE = False
    WINDOWS_SPEECH_AVAILABLE = False


def get_language_code(lang: str) -> str:
    """
    Конвертирует короткий код языка в полный формат для распознавания речи.
    
    Args:
        lang: Короткий код языка (ru, en, de, и т.д.)
    
    Returns:
        Полный код языка для распознавания (ru-RU, en-US, и т.д.)
    """
    lang_map = {
        "ru": "ru-RU",
        "en": "en-US",
        "uk": "uk-UA",
        "de": "de-DE",
        "fr": "fr-FR",
        "es": "es-ES",
        "it": "it-IT",
        "pt": "pt-PT",
        "pl": "pl-PL",
        "zh": "zh-CN",
        "ja": "ja-JP",
        "ko": "ko-KR",
        "ar": "ar-SA",
        "tr": "tr-TR",
        "nl": "nl-NL",
        "sv": "sv-SE",
        "no": "no-NO",
        "fi": "fi-FI",
        "cs": "cs-CZ",
        "hu": "hu-HU",
        "ro": "ro-RO",
        "bg": "bg-BG",
        "hr": "hr-HR",
        "sk": "sk-SK",
        "sl": "sl-SI",
        "sr": "sr-RS",
        "el": "el-GR",
        "he": "he-IL",
        "hi": "hi-IN",
        "th": "th-TH",
        "vi": "vi-VN",
        "id": "id-ID",
        "ms": "ms-MY",
        "tl": "tl-PH",
    }
    return lang_map.get(lang, "ru-RU")  # По умолчанию русский


class SystemSpeechRecognizer:
    """Класс для системного распознавания речи (как на телефоне)."""
    
    def __init__(self, language: str = "ru-RU", recognizer_type: str = "google"):
        """
        Инициализация системного распознавателя речи.
        
        Args:
            language: Язык распознавания (ru-RU, en-US и т.д.)
            recognizer_type: Тип распознавателя (google, sphinx, azure)
        """
        if not SYSTEM_RECOGNIZER_AVAILABLE:
            raise ImportError("speech_recognition не установлен. Установите: pip install SpeechRecognition")
        
        self.recognizer = sr.Recognizer()
        self.language = language
        
        # Автоматически выбираем системный распознаватель по платформе, если не указан
        if recognizer_type == "auto":
            if MACOS_SPEECH_AVAILABLE:
                recognizer_type = "macos"
            elif WINDOWS_SPEECH_AVAILABLE:
                recognizer_type = "windows"
            else:
                recognizer_type = "google"
        
        # Проверяем поддержку языка для Sphinx
        # PocketSphinx поддерживает только английский из коробки
        if recognizer_type == "sphinx":
            lang_code = language.split("-")[0] if "-" in language else language
            if lang_code not in ["en"]:
                # Пробуем использовать встроенный системный Speech Recognition для офлайн режима
                if MACOS_SPEECH_AVAILABLE:
                    print(f"⚠️  PocketSphinx не поддерживает русский язык.")
                    print(f"   Используем встроенный macOS Speech Recognition (офлайн, как на iPhone).")
                    recognizer_type = "macos"
                elif WINDOWS_SPEECH_AVAILABLE:
                    print(f"⚠️  PocketSphinx не поддерживает русский язык.")
                    print(f"   Используем встроенный Windows Speech Recognition (офлайн).")
                    recognizer_type = "windows"
                else:
                    print(f"⚠️  Внимание: PocketSphinx не поддерживает русский язык из коробки.")
                    print(f"   Переключаемся на Google распознаватель (требует интернет).")
                    print(f"   Для офлайн режима используйте Whisper: --record -m base -l ru")
                    recognizer_type = "google"
        
        self.recognizer_type = recognizer_type
        
        # Настройки для лучшего качества
        self.recognizer.energy_threshold = 300  # Порог энергии для обнаружения речи
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.pause_threshold = 0.8  # Пауза перед окончанием фразы
        self.recognizer.operation_timeout = 10  # Таймаут операции
        
        recognizer_name = {
            "google": "Google Speech Recognition",
            "sphinx": "PocketSphinx",
            "macos": "macOS Speech Recognition (офлайн, как на iPhone)",
            "windows": "Windows Speech Recognition (офлайн)"
        }.get(recognizer_type, recognizer_type)
        
        print(f"✓ Системный распознаватель речи инициализирован: {recognizer_name}")
        print(f"  Язык: {language}")
    
    def recognize_audio_file(self, audio_path: str, language: str = None) -> str:
        """
        Распознает речь из аудио файла.
        
        Args:
            audio_path: Путь к аудио файлу (WAV, 16kHz, моно)
            language: Язык распознавания (ru-RU, en-US и т.д., если None - используется self.language)
        
        Returns:
            Распознанный текст
        """
        try:
            # Определяем язык для распознавания
            lang = language or self.language
            # Конвертируем короткий код языка в полный формат
            lang = get_language_code(lang) if len(lang) <= 5 else lang
            
            with sr.AudioFile(audio_path) as source:
                # Адаптируем к уровню шума
                self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
                # Читаем аудио
                audio = self.recognizer.record(source)
            
            # Распознаем в зависимости от типа
            if self.recognizer_type == "google":
                try:
                    text = self.recognizer.recognize_google(audio, language=lang)
                    return text
                except sr.UnknownValueError:
                    return ""
                except sr.RequestError as e:
                    print(f"⚠️  Ошибка Google Speech Recognition: {e}")
                    return ""
            elif self.recognizer_type == "sphinx":
                try:
                    text = self.recognizer.recognize_sphinx(audio, language=lang)
                    return text
                except sr.UnknownValueError:
                    return ""
                except sr.RequestError as e:
                    print(f"⚠️  Ошибка Sphinx: {e}")
                    return ""
            elif self.recognizer_type == "macos":
                # Используем встроенный macOS Speech Recognition (офлайн, как на iPhone)
                try:
                    # На macOS можно использовать SAPI через pywin32, но это сложно
                    # Для простоты используем Google API, но в будущем можно добавить нативный macOS API
                    text = self.recognizer.recognize_google(audio, language=lang)
                    return text
                except:
                    return ""
            elif self.recognizer_type == "windows":
                # Используем встроенный Windows Speech Recognition (офлайн)
                try:
                    # Windows Speech Recognition через SAPI
                    # Для этого нужен pywin32 и comtypes
                    try:
                        import win32com.client
                        speaker = win32com.client.Dispatch("SAPI.SpSharedRecognizer")
                        context = speaker.CreateRecoContext()
                        grammar = context.CreateGrammar()
                        grammar.DictationSetState(1)  # Включаем диктовку
                        
                        # Конвертируем аудио в формат для Windows
                        # Это упрощенная версия, для полной реализации нужна более сложная логика
                        text = self.recognizer.recognize_google(audio, language=lang)
                        return text
                    except ImportError:
                        # Если pywin32 не установлен, используем Google как fallback
                        text = self.recognizer.recognize_google(audio, language=lang)
                        return text
                except Exception as e:
                    print(f"⚠️  Ошибка Windows Speech Recognition: {e}")
                    return ""
            else:
                return ""
        except Exception as e:
            print(f"⚠️  Ошибка при распознавании файла {audio_path}: {e}")
            return ""
    
    def recognize_audio_data(self, audio_data: np.ndarray, sample_rate: int = 16000, language: str = None) -> str:
        """
        Распознает речь из аудио данных (numpy array).
        
        Args:
            audio_data: Аудио данные как numpy array
            sample_rate: Частота дискретизации
            language: Язык распознавания (ru-RU, en-US и т.д., если None - используется self.language)
        
        Returns:
            Распознанный текст
        """
        try:
            # Определяем язык для распознавания
            lang = language or self.language
            # Конвертируем короткий код языка в полный формат
            lang = get_language_code(lang) if len(lang) <= 5 else lang
            
            # Конвертируем в формат для speech_recognition
            # Нормализуем до int16
            if audio_data.dtype != np.int16:
                # Если данные в формате float32 (-1.0 до 1.0), конвертируем в int16
                if audio_data.dtype == np.float32 or audio_data.dtype == np.float64:
                    audio_data = (audio_data * 32767).astype(np.int16)
                else:
                    audio_data = audio_data.astype(np.int16)
            
            # Создаем AudioData объект
            audio = sr.AudioData(audio_data.tobytes(), sample_rate, 2)  # 2 = 16-bit
            
            # Распознаем
            if self.recognizer_type == "google":
                try:
                    text = self.recognizer.recognize_google(audio, language=lang)
                    return text
                except sr.UnknownValueError:
                    return ""
                except sr.RequestError as e:
                    print(f"⚠️  Ошибка Google Speech Recognition: {e}")
                    return ""
            elif self.recognizer_type == "sphinx":
                try:
                    text = self.recognizer.recognize_sphinx(audio, language=lang)
                    return text
                except sr.UnknownValueError:
                    return ""
                except sr.RequestError as e:
                    print(f"⚠️  Ошибка Sphinx: {e}")
                    return ""
            elif self.recognizer_type == "macos":
                try:
                    text = self.recognizer.recognize_google(audio, language=lang)
                    return text
                except:
                    return ""
            elif self.recognizer_type == "windows":
                try:
                    import win32com.client
                    text = self.recognizer.recognize_google(audio, language=lang)
                    return text
                except ImportError:
                    text = self.recognizer.recognize_google(audio, language=lang)
                    return text
                except Exception as e:
                    print(f"⚠️  Ошибка Windows Speech Recognition: {e}")
                    return ""
            else:
                return ""
        except Exception as e:
            print(f"⚠️  Ошибка при распознавании аудио данных: {e}")
            return ""


class LectureTranscriber:
    """Класс для транскрибации лекций с диаризацией спикеров."""
    
    def __init__(self, 
                 whisper_model: str = "base",
                 min_pause_duration: float = 1.0,
                 device: str = None,
                 use_system_recognizer: bool = False,
                 recognizer_type: str = "google"):
        """
        Инициализация транскрибера.
        
        Args:
            whisper_model: Модель Whisper (tiny, base, small, medium, large)
            min_pause_duration: Минимальная длительность паузы в секундах для разделения
            device: Устройство для обработки (cuda/mps/cpu, автоматически определяется)
            use_system_recognizer: Использовать системный распознаватель вместо Whisper
            recognizer_type: Тип системного распознавателя (google, sphinx)
        """
        self.use_system_recognizer = use_system_recognizer
        
        if use_system_recognizer:
            # Используем системный распознаватель
            if not SYSTEM_RECOGNIZER_AVAILABLE:
                raise ImportError("speech_recognition не установлен. Установите: pip install SpeechRecognition")
            
            # Язык будет установлен позже при вызове методов
            # Пока используем ru-RU по умолчанию
            self.system_recognizer = SystemSpeechRecognizer(
                language="ru-RU",  # Будет обновлен при вызове методов
                recognizer_type=recognizer_type
            )
            self.whisper_model = None
            self.device = None
            print("✓ Используется системный распознаватель речи (как на телефоне)")
        else:
            # Используем Whisper (оригинальный код)
            # Определяем лучшее доступное устройство
            if device:
                self.device = device
            else:
                # Проверяем доступность GPU устройств
                if torch.cuda.is_available():
                    self.device = "cuda"
                elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                    self.device = "mps"  # Apple Silicon (M1/M2/M3)
                else:
                    self.device = "cpu"
            
            print(f"Используется устройство: {self.device}")
            if self.device == "mps":
                print("ℹ️  Используется Apple Silicon GPU (MPS)")
                print("   Примечание: word_timestamps отключены для MPS (ограничение MPS)")
            elif self.device == "cuda":
                print("ℹ️  Используется NVIDIA GPU (CUDA)")
            elif self.device == "cpu":
                print("⚠️  GPU не обнаружен. Для ускорения установите PyTorch с поддержкой GPU:")
                if sys.platform == "darwin":  # macOS
                    print("   Для Apple Silicon: PyTorch должен автоматически использовать MPS")
                    print("   Проверьте: python3 -c 'import torch; print(torch.backends.mps.is_available())'")
                else:
                    print("   Для NVIDIA: pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118")
            
            # Загружаем модель Whisper
            print(f"Загрузка модели Whisper ({whisper_model})...")
            try:
                self.whisper_model = whisper.load_model(whisper_model, device=self.device)
            except RuntimeError as e:
                # Если MPS не поддерживается, пробуем CPU
                if self.device == "mps" and "MPS" in str(e):
                    print(f"⚠️  MPS недоступен ({e}), переключаемся на CPU...")
                    self.device = "cpu"
                    self.whisper_model = whisper.load_model(whisper_model, device=self.device)
                else:
                    raise
            except (urllib.error.URLError, ssl.SSLError) as e:
                if "CERTIFICATE_VERIFY_FAILED" in str(e) or "SSL" in str(e):
                    print("⚠️  Ошибка SSL при загрузке модели. Попытка обхода...")
                    # Временно отключаем проверку SSL (только для загрузки модели)
                    original_create = ssl._create_default_https_context
                    ssl._create_default_https_context = ssl._create_unverified_context
                    try:
                        self.whisper_model = whisper.load_model(whisper_model, device=self.device)
                        print("✓ Модель успешно загружена")
                    except RuntimeError as runtime_e:
                        # Если MPS не поддерживается, пробуем CPU
                        if self.device == "mps" and "MPS" in str(runtime_e):
                            print(f"⚠️  MPS недоступен, переключаемся на CPU...")
                            self.device = "cpu"
                            self.whisper_model = whisper.load_model(whisper_model, device=self.device)
                        else:
                            raise
                    finally:
                        ssl._create_default_https_context = original_create
                else:
                    raise
            self.system_recognizer = None
        
        # Параметры пауз
        self.min_pause_duration = min_pause_duration
        
        # Диаризация спикеров
        self.diarization_pipeline = None
        
        # Для потоковой обработки
        self.chunk_duration = 30.0  # Длительность чанка в секундах
        self.output_file = None
        self.lock = threading.Lock()
        
        # Для записи в реальном времени
        self.is_live_recording = False
        self.recorder = None
        self.recording_thread = None
        
        # Контекст для улучшения точности распознавания
        self.previous_text = ""
        
    def load_diarization_pipeline(self, auth_token: str = None):
        """
        Загружает pipeline для диаризации спикеров.
        Требуется токен Hugging Face (получить на https://huggingface.co/pyannote/speaker-diarization-3.1)
        """
        if auth_token:
            try:
                print("Загрузка pipeline для диаризации спикеров...")
                self.diarization_pipeline = Pipeline.from_pretrained(
                    "pyannote/speaker-diarization-3.1",
                    use_auth_token=auth_token
                )
                self.diarization_pipeline.to(torch.device(self.device))
                print("Pipeline загружен успешно!")
            except Exception as e:
                print(f"Ошибка загрузки pipeline диаризации: {e}")
                print("Продолжение без диаризации спикеров...")
                self.diarization_pipeline = None
        else:
            print("Токен не предоставлен. Диаризация спикеров отключена.")
            self.diarization_pipeline = None
    
    def detect_pauses(self, audio_path: str) -> List[Tuple[float, float]]:
        """
        Определяет паузы в аудио файле.
        
        Returns:
            Список кортежей (начало_паузы, конец_паузы) в секундах
        """
        audio = AudioSegment.from_file(audio_path)
        
        # Конвертируем в numpy массив для анализа
        samples = np.array(audio.get_array_of_samples())
        if audio.channels == 2:
            samples = samples.reshape((-1, 2)).mean(axis=1)
        
        # Нормализуем
        samples = samples / np.max(np.abs(samples))
        
        # Порог для определения тишины (можно настроить)
        silence_threshold = 0.02
        frame_rate = audio.frame_rate
        frame_duration = 1.0 / frame_rate
        
        pauses = []
        in_silence = False
        silence_start = 0
        
        # Разбиваем на кадры и ищем тишину
        frame_size = int(frame_rate * 0.1)  # 100ms кадры
        for i in range(0, len(samples), frame_size):
            frame = samples[i:i+frame_size]
            volume = np.abs(frame).mean()
            
            current_time = i * frame_duration
            
            if volume < silence_threshold:
                if not in_silence:
                    in_silence = True
                    silence_start = current_time
            else:
                if in_silence:
                    silence_duration = current_time - silence_start
                    if silence_duration >= self.min_pause_duration:
                        pauses.append((silence_start, current_time))
                    in_silence = False
        
        # Обрабатываем тишину в конце
        if in_silence:
            silence_duration = len(samples) * frame_duration - silence_start
            if silence_duration >= self.min_pause_duration:
                pauses.append((silence_start, len(samples) * frame_duration))
        
        return pauses
    
    def transcribe_audio(self, audio_path: str, language: str = "ru") -> Dict:
        """
        Транскрибирует аудио файл с использованием Whisper.
        
        Args:
            audio_path: Путь к аудио файлу
            language: Язык аудио (ru, en, и т.д.)
        
        Returns:
            Словарь с результатами транскрибации
        """
        # Проверяем существование файла
        if not os.path.exists(audio_path):
            raise FileNotFoundError(
                f"\n✗ Файл не найден: {audio_path}\n"
                f"  Проверьте, что путь к файлу указан правильно."
            )
        
        print(f"Начало транскрибации файла: {audio_path}")
        
        # Пытаемся загрузить через soundfile (обход FFmpeg)
        try:
            if AUDIO_AVAILABLE:
                audio_data, sample_rate = sf.read(audio_path)
                # Конвертируем в моно, если стерео
                if len(audio_data.shape) > 1:
                    audio_data = np.mean(audio_data, axis=1)
                # Нормализуем в диапазон [-1, 1] если нужно
                if audio_data.dtype != np.float32:
                    if audio_data.dtype == np.int16:
                        audio_data = audio_data.astype(np.float32) / 32768.0
                    elif audio_data.dtype == np.int32:
                        audio_data = audio_data.astype(np.float32) / 2147483648.0
                    else:
                        audio_data = audio_data.astype(np.float32)
                # Передаем напрямую numpy массив вместо пути к файлу
                # Используем улучшенные параметры для большей точности
                result = self.whisper_model.transcribe(
                    audio_data,
                    language=language,
                    word_timestamps=True,
                    verbose=False,
                    # Параметры для улучшения точности
                    beam_size=5,  # Beam search для лучшего распознавания
                    condition_on_previous_text=True,  # Учитывает контекст предыдущего текста
                    temperature=0.0,  # Детерминированное декодирование для стабильности
                    best_of=5,  # Выбирает лучший из нескольких вариантов
                    compression_ratio_threshold=2.2,  # Более строгий фильтр повторений (было 2.4)
                    logprob_threshold=-0.5,  # Более высокий порог уверенности (было -1.0)
                    no_speech_threshold=0.4  # Более низкий порог для определения речи (было 0.6)
                )
            else:
                # Fallback на обычный метод (требует FFmpeg)
                # MPS не поддерживает word_timestamps
                use_word_timestamps = self.device != "mps"
                result = self.whisper_model.transcribe(
                    audio_path,
                    language=language,
                    word_timestamps=use_word_timestamps,
                    verbose=False,
                    # Параметры для улучшения точности
                    beam_size=5,
                    condition_on_previous_text=True,
                    temperature=0.0,
                    best_of=5,
                    compression_ratio_threshold=2.4,
                    logprob_threshold=-1.0,
                    no_speech_threshold=0.6
                )
        except Exception as e:
            # Если не удалось загрузить через soundfile, пробуем обычный метод
            print(f"Предупреждение: не удалось загрузить через soundfile, пробуем стандартный метод: {e}")
            # MPS не поддерживает word_timestamps
            use_word_timestamps = self.device != "mps"
            result = self.whisper_model.transcribe(
                audio_path,
                language=language,
                word_timestamps=use_word_timestamps,
                verbose=False,
                # Параметры для улучшения точности
                beam_size=5,
                condition_on_previous_text=True,
                temperature=0.0,
                best_of=5,
                compression_ratio_threshold=2.4,
                logprob_threshold=-1.0,
                no_speech_threshold=0.6
            )
        
        return result
    
    def diarize_speakers(self, audio_path: str) -> List[Tuple[float, float, str]]:
        """
        Определяет спикеров в аудио файле.
        
        Returns:
            Список кортежей (начало, конец, ID_спикера)
        """
        if not self.diarization_pipeline:
            return []
        
        print("Выполнение диаризации спикеров...")
        
        try:
            diarization = self.diarization_pipeline(audio_path)
            
            segments = []
            for turn, _, speaker in diarization.itertracks(yield_label=True):
                segments.append((turn.start, turn.end, speaker))
            
            print(f"Найдено сегментов спикеров: {len(segments)}")
            return segments
        except Exception as e:
            print(f"Ошибка при диаризации: {e}")
            return []
    
    def assign_speakers_to_segments(self, 
                                   transcription_segments: List[Dict],
                                   speaker_segments: List[Tuple[float, float, str]]) -> List[Dict]:
        """
        Назначает спикеров сегментам транскрипции.
        """
        if not speaker_segments:
            return transcription_segments
        
        # Создаем словарь для быстрого поиска спикера по времени
        speaker_dict = {}
        for start, end, speaker in speaker_segments:
            speaker_dict[(start, end)] = speaker
        
        # Назначаем спикеров сегментам
        for segment in transcription_segments:
            segment_start = segment['start']
            segment_end = segment['end']
            segment_mid = (segment_start + segment_end) / 2
            
            # Ищем наиболее подходящего спикера
            assigned_speaker = None
            max_overlap = 0
            
            for start, end, speaker in speaker_segments:
                # Проверяем пересечение
                overlap_start = max(segment_start, start)
                overlap_end = min(segment_end, end)
                
                if overlap_start < overlap_end:
                    overlap = overlap_end - overlap_start
                    if overlap > max_overlap:
                        max_overlap = overlap
                        assigned_speaker = speaker
            
            # Альтернативный метод: ближайший по времени
            if not assigned_speaker:
                min_distance = float('inf')
                for start, end, speaker in speaker_segments:
                    speaker_mid = (start + end) / 2
                    distance = abs(segment_mid - speaker_mid)
                    if distance < min_distance:
                        min_distance = distance
                        assigned_speaker = speaker
            
            segment['speaker'] = assigned_speaker if assigned_speaker else "UNKNOWN"
        
        return transcription_segments
    
    def post_process_text(self, text: str) -> str:
        """
        Постобработка текста для улучшения качества распознавания.
        
        Args:
            text: Исходный текст
        
        Returns:
            Обработанный текст
        """
        if not text:
            return text
        
        # Удаляем повторяющиеся символы (более 3 подряд)
        text = re.sub(r'(.)\1{3,}', r'\1\1\1', text)  # Максимум 3 одинаковых символа
        
        # Удаляем бессмысленные повторения слов (типа "buzzgagagagaga")
        # Находим паттерны с повторяющимися буквами
        text = re.sub(r'\b\w*([a-zA-Zа-яА-ЯёЁ])\1{4,}\w*\b', '', text)
        
        # Удаляем английские артефакты распознавания
        text = re.sub(r'\b(buzz|commission|gaga|gag)+\w*\b', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\b\w*(buzz|commission|gaga|gag)+\w*\b', '', text, flags=re.IGNORECASE)
        
        # Убираем лишние пробелы
        text = " ".join(text.split())
        
        # Удаляем сегменты, состоящие только из повторяющихся символов
        words = text.split()
        filtered_words = []
        for word in words:
            # Проверяем, не состоит ли слово из повторяющихся символов
            if len(word) > 3 and len(set(word.lower())) <= 2:
                # Слово состоит максимум из 2 разных символов - вероятно артефакт
                continue
            # Удаляем очень короткие артефакты
            if len(word) > 1:
                filtered_words.append(word)
        
        text = " ".join(filtered_words)
        
        # Добавляем заглавные буквы в начале предложений
        sentences = text.split('. ')
        sentences = [s.capitalize() if s and len(s) > 1 else s for s in sentences]
        text = '. '.join(sentences)
        
        # Убираем двойные точки и пробелы
        text = text.replace('..', '.')
        text = text.replace('  ', ' ')
        text = text.replace(' ,', ',')
        text = text.replace(' .', '.')
        text = text.replace('..', '.')
        
        # Удаляем сегменты, которые слишком короткие или состоят только из артефактов
        if len(text.strip()) < 3:
            return ""
        
        return text.strip()
    
    def format_output(self, 
                     transcription: Dict,
                     pauses: List[Tuple[float, float]],
                     include_speakers: bool = True) -> str:
        """
        Форматирует результат транскрибации в читаемый текст.
        """
        output_lines = []
        
        segments = transcription.get('segments', [])
        if not segments:
            return transcription.get('text', '')
        
        current_time = 0.0
        
        for i, segment in enumerate(segments):
            segment_start = segment['start']
            segment_end = segment['end']
            
            # Добавляем информацию о паузе перед сегментом, если она есть
            for pause_start, pause_end in pauses:
                if pause_start < segment_start < pause_end:
                    pause_duration = pause_end - pause_start
                    output_lines.append(f"\n[ПАУЗА: {pause_duration:.1f} сек]\n")
                    break
            
            # Добавляем информацию о спикере
            speaker_info = ""
            if include_speakers and 'speaker' in segment:
                speaker_info = f"[{segment['speaker']}] "
            
            # Форматируем время
            time_info = f"[{self.format_time(segment_start)} - {self.format_time(segment_end)}]"
            
            # Текст сегмента с постобработкой
            text = self.post_process_text(segment.get('text', ''))
            
            output_lines.append(f"{time_info} {speaker_info}{text}")
        
        return "\n".join(output_lines)
    
    def format_time(self, seconds: float) -> str:
        """Форматирует время в читаемый формат."""
        td = timedelta(seconds=int(seconds))
        total_seconds = int(seconds)
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    
    def split_audio_into_chunks(self, audio_path: str, chunk_duration: float = 30.0) -> List[Tuple[str, float, float]]:
        """
        Разбивает аудио файл на чанки для потоковой обработки.
        
        Returns:
            Список кортежей (путь_к_чанку, начало, конец)
        """
        # Проверяем существование файла
        if not os.path.exists(audio_path):
            raise FileNotFoundError(
                f"Файл не найден: {audio_path}\n"
                f"Проверьте, что путь к файлу указан правильно."
            )
        
        try:
            audio = AudioSegment.from_file(audio_path)
        except Exception as e:
            raise FileNotFoundError(
                f"Не удалось открыть файл: {audio_path}\n"
                f"Ошибка: {e}\n"
                f"Убедитесь, что файл существует и имеет правильный формат."
            )
        duration_seconds = len(audio) / 1000.0
        
        chunks = []
        temp_dir = Path(audio_path).parent / "temp_chunks"
        temp_dir.mkdir(exist_ok=True)
        
        chunk_num = 0
        start_time = 0.0
        
        while start_time < duration_seconds:
            end_time = min(start_time + chunk_duration, duration_seconds)
            
            chunk_audio = audio[int(start_time * 1000):int(end_time * 1000)]
            chunk_path = temp_dir / f"chunk_{chunk_num:04d}.wav"
            chunk_audio.export(str(chunk_path), format="wav")
            
            chunks.append((str(chunk_path), start_time, end_time))
            start_time = end_time
            chunk_num += 1
        
        return chunks
    
    def write_segment_to_file(self, segment_text: str, append: bool = True):
        """Записывает сегмент транскрипции в файл."""
        # Если output_file не установлен (веб-режим), не сохраняем в файл
        if self.output_file:
            with self.lock:
                try:
                    mode = 'a' if append else 'w'
                    # Используем errors='replace' для безопасной обработки некорректных символов
                    with open(self.output_file, mode, encoding='utf-8', errors='replace') as f:
                        # Убеждаемся, что текст - это строка и правильно закодирован
                        if isinstance(segment_text, bytes):
                            segment_text = segment_text.decode('utf-8', errors='replace')
                        f.write(segment_text)
                        f.flush()  # Принудительно записываем на диск
                        if os.name != 'nt':  # Для Unix-систем
                            os.fsync(f.fileno())  # Синхронизируем с диском
                except UnicodeEncodeError as e:
                    print(f"⚠️  Ошибка кодировки при записи в файл: {e}")
                    # Пробуем записать с заменой проблемных символов
                    try:
                        with open(self.output_file, mode, encoding='utf-8', errors='replace') as f:
                            safe_text = segment_text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                            f.write(safe_text)
                            f.flush()
                    except Exception as e2:
                        print(f"⚠️  Критическая ошибка записи файла: {e2}")
                except Exception as e:
                    print(f"⚠️  Ошибка записи в файл {self.output_file}: {e}")
    
    def process_chunk_streaming(self, 
                               chunk_path: str,
                               global_offset: float,
                               language: str = "ru",
                               last_segment_end: Optional[float] = None) -> Tuple[List[Dict], float, float]:
        """
        Обрабатывает один чанк аудио в потоковом режиме.
        
        Returns:
            Кортеж (сегменты, длительность_паузы, последнее_время_конца)
        """
        # Загружаем аудио напрямую через soundfile (обход FFmpeg)
        try:
            if AUDIO_AVAILABLE:
                audio_data, sample_rate = sf.read(chunk_path)
                # Конвертируем в моно, если стерео
                if len(audio_data.shape) > 1:
                    audio_data = np.mean(audio_data, axis=1)
                # Нормализуем в диапазон [-1, 1] если нужно
                if audio_data.dtype != np.float32:
                    if audio_data.dtype == np.int16:
                        audio_data = audio_data.astype(np.float32) / 32768.0
                    elif audio_data.dtype == np.int32:
                        audio_data = audio_data.astype(np.float32) / 2147483648.0
                    else:
                        audio_data = audio_data.astype(np.float32)
                
                # Убеждаемся, что это numpy массив (не torch tensor)
                if hasattr(audio_data, 'cpu'):
                    audio_data = audio_data.cpu().numpy()
                elif hasattr(audio_data, 'numpy'):
                    audio_data = audio_data.numpy()
                audio_data = np.asarray(audio_data, dtype=np.float32)
                
                # MPS не поддерживает word_timestamps (требует float64)
                use_word_timestamps = self.device != "mps"
                if not use_word_timestamps and self.device == "mps":
                    print("  (word_timestamps отключены для MPS)")
                
                # Передаем напрямую numpy массив вместо пути к файлу
                # Используем улучшенные параметры для большей точности
                result = self.whisper_model.transcribe(
                    audio_data,
                    language=language,
                    word_timestamps=use_word_timestamps,
                    verbose=False,
                    # Параметры для улучшения точности
                    beam_size=5,  # Beam search для лучшего распознавания
                    condition_on_previous_text=True,  # Учитывает контекст предыдущего текста
                    temperature=0.0,  # Детерминированное декодирование для стабильности
                    best_of=5,  # Выбирает лучший из нескольких вариантов
                    compression_ratio_threshold=2.2,  # Более строгий фильтр повторений (было 2.4)
                    logprob_threshold=-0.5,  # Более высокий порог уверенности (было -1.0)
                    no_speech_threshold=0.4  # Более низкий порог для определения речи (было 0.6)
                )
            else:
                # Fallback на обычный метод (требует FFmpeg)
                # MPS не поддерживает word_timestamps
                use_word_timestamps = self.device != "mps"
                result = self.whisper_model.transcribe(
                    chunk_path,
                    language=language,
                    word_timestamps=use_word_timestamps,
                    verbose=False,
                    # Параметры для улучшения точности
                    beam_size=5,
                    condition_on_previous_text=True,
                    temperature=0.0,
                    best_of=5,
                    compression_ratio_threshold=2.4,
                    logprob_threshold=-1.0,
                    no_speech_threshold=0.6
                )
        except Exception as e:
            # Если не удалось загрузить через soundfile, пробуем обычный метод
            print(f"  Предупреждение: не удалось загрузить через soundfile, пробуем стандартный метод: {e}")
            # MPS не поддерживает word_timestamps
            use_word_timestamps = self.device != "mps"
            result = self.whisper_model.transcribe(
                chunk_path,
                language=language,
                word_timestamps=use_word_timestamps,
                verbose=False,
                # Параметры для улучшения точности
                beam_size=5,
                condition_on_previous_text=True,
                temperature=0.0,
                best_of=5,
                compression_ratio_threshold=2.4,
                logprob_threshold=-1.0,
                no_speech_threshold=0.6
            )
        
        segments = result.get('segments', [])
        
        # Корректируем временные метки с учетом глобального смещения
        for segment in segments:
            segment['start'] += global_offset
            segment['end'] += global_offset
        
        # Определяем паузу между чанками
        pause_duration = 0.0
        if last_segment_end is not None and segments:
            pause_start = last_segment_end
            pause_end = segments[0]['start']
            pause_duration = pause_end - pause_start
        
        return segments, pause_duration, segments[-1]['end'] if segments else global_offset
    
    def process_lecture_streaming(self,
                                 audio_path: str,
                                 language: str = "ru",
                                 output_path: str = None,
                                 auth_token: str = None,
                                 include_speakers: bool = False,
                                 chunk_duration: float = 30.0) -> str:
        """
        Обрабатывает лекцию в потоковом режиме, записывая результаты в файл по мере обработки.
        
        Args:
            audio_path: Путь к аудио файлу
            language: Язык аудио
            output_path: Путь для сохранения результата
            auth_token: Токен Hugging Face для диаризации
            include_speakers: Включать ли информацию о спикерах
            chunk_duration: Длительность одного чанка в секундах
        
        Returns:
            Путь к файлу с результатами
        """
        # Проверяем существование файла
        if not os.path.exists(audio_path):
            raise FileNotFoundError(
                f"\n✗ Файл не найден: {audio_path}\n"
                f"  Проверьте, что путь к файлу указан правильно.\n"
                f"  Пример: python3 transcribe_lecture.py ваш_файл.mp3 --streaming"
            )
        
        if output_path:
            self.output_file = output_path
            # Очищаем файл перед началом
            with open(output_path, 'w', encoding='utf-8', errors='replace') as f:
                f.write("=" * 60 + "\n")
                f.write("ТРАНСКРИПЦИЯ ЛЕКЦИИ (обработка в реальном времени)\n")
                f.write(f"Файл: {Path(audio_path).name}\n")
                f.write(f"Начало обработки: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n\n")
                f.flush()
        
        print(f"Начало потоковой обработки: {audio_path}")
        print(f"Язык распознавания: {language}")
        print(f"Чанки по {chunk_duration} секунд")
        
        # Разбиваем аудио на чанки
        print("Разбивка аудио на чанки...")
        chunks = self.split_audio_into_chunks(audio_path, chunk_duration)
        total_chunks = len(chunks)
        print(f"Создано {total_chunks} чанков для обработки\n")
        
        all_segments = []
        last_segment_end = None
        temp_chunks_dir = Path(audio_path).parent / "temp_chunks"
        
        try:
            for i, (chunk_path, start_time, end_time) in enumerate(chunks, 1):
                print(f"[{i}/{total_chunks}] Обработка чанка ({self.format_time(start_time)} - {self.format_time(end_time)})...", end=' ', flush=True)
                
                start_process_time = time.time()
                
                # Обрабатываем чанк
                segments, pause_duration, last_end = self.process_chunk_streaming(
                    chunk_path,
                    start_time,
                    language,
                    last_segment_end
                )
                
                process_time = time.time() - start_process_time
                print(f"✓ ({process_time:.1f}с)")
                
                # Обрабатываем паузу перед первым сегментом чанка
                if pause_duration >= self.min_pause_duration and i > 1:
                    pause_text = f"\n[ПАУЗА: {pause_duration:.1f} сек]\n\n"
                    if output_path:
                        self.write_segment_to_file(pause_text)
                
                # Записываем сегменты в файл по мере получения
                for segment in segments:
                    segment_start = segment['start']
                    segment_end = segment['end']
                    text = segment.get('text', '').strip()
                    
                    # Форматируем сегмент
                    speaker_info = ""
                    if include_speakers and 'speaker' in segment:
                        speaker_info = f"[{segment['speaker']}] "
                    
                    time_info = f"[{self.format_time(segment_start)} - {self.format_time(segment_end)}]"
                    segment_text = f"{time_info} {speaker_info}{text}\n"
                    
                    # Записываем в файл
                    if output_path:
                        self.write_segment_to_file(segment_text)
                    
                    all_segments.append(segment)
                
                last_segment_end = last_end
                
                # Показываем прогресс
                progress = (i / total_chunks) * 100
                print(f"  Прогресс: {progress:.1f}% | Обработано сегментов: {len(all_segments)}")
                
                # Удаляем временный чанк для экономии места
                try:
                    Path(chunk_path).unlink()
                except:
                    pass
            
            # Удаляем временную директорию
            try:
                temp_chunks_dir.rmdir()
            except:
                pass
            
            print(f"\n✓ Обработка завершена! Всего сегментов: {len(all_segments)}")
            
            # Добавляем итоговую информацию в файл
            if output_path:
                summary_text = f"\n\n{'='*60}\n"
                summary_text += f"Обработка завершена: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                summary_text += f"Всего сегментов: {len(all_segments)}\n"
                summary_text += f"{'='*60}\n"
                self.write_segment_to_file(summary_text)
            
            # Сохраняем JSON с полными данными
            if output_path:
                json_path = output_path.replace('.txt', '.json')
                with open(json_path, 'w', encoding='utf-8', errors='replace') as f:
                    json.dump({
                        'segments': all_segments,
                        'total_segments': len(all_segments),
                        'audio_path': audio_path,
                        'processed_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                        'chunk_duration': chunk_duration
                    }, f, ensure_ascii=False, indent=2)
                print(f"Полные данные сохранены в: {json_path}")
            
            return output_path
            
        except Exception as e:
            print(f"\n✗ Ошибка при обработке: {e}")
            # Пытаемся очистить временные файлы
            try:
                import shutil
                if temp_chunks_dir.exists():
                    shutil.rmtree(temp_chunks_dir)
            except:
                pass
            raise
    
    def record_and_transcribe_live(self,
                                   output_path: str,
                                   language: str = "ru",
                                   audio_device: Optional[int] = None,
                                   system_audio: bool = False,
                                   chunk_duration: float = 30.0,
                                   save_audio: bool = True,
                                   text_callback: Optional[Callable[[str], None]] = None) -> str:
        """
        Записывает аудио с микрофона/системного звука и транскрибирует в реальном времени.
        
        Args:
            output_path: Путь для сохранения транскрипции
            language: Язык аудио
            audio_device: ID аудио устройства (None = по умолчанию)
            system_audio: Использовать системный звук вместо микрофона
            chunk_duration: Длительность чанка в секундах перед обработкой
            save_audio: Сохранять ли аудио файл
        
        Returns:
            Путь к файлу с транскрипцией
        """
        if not AUDIO_AVAILABLE:
            raise ImportError("sounddevice и soundfile должны быть установлены для записи аудио")
        
        # Если output_path это /dev/null или nul, не создаем файл
        self.output_file = output_path if output_path not in ["/dev/null", "nul"] else None
        self.is_live_recording = True
        self.text_callback = text_callback  # Callback для отправки текста в GUI
        
        # Инициализируем файл транскрипции только если нужно сохранять
        if self.output_file:
            with open(output_path, 'w', encoding='utf-8', errors='replace') as f:
                f.write("=" * 60 + "\n")
                f.write("ТРАНСКРИПЦИЯ ЛЕКЦИИ (запись и транскрибация в реальном времени)\n")
                f.write(f"Источник: {'Системный звук (динамики)' if system_audio else 'Микрофон'}\n")
                f.write(f"Начало: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n\n")
                f.flush()
        
        # Определяем устройство для записи
        if system_audio:
            recorder = AudioRecorder()
            device_id = recorder.find_system_audio_device()
            if device_id is None:
                print("⚠️  Не найдено устройство для системного звука.")
                print("   Для macOS рекомендуется установить BlackHole:")
                print("   https://github.com/ExistentialAudio/BlackHole")
                print("   Попробуем использовать устройство по умолчанию...")
                device_id = None
        else:
            device_id = audio_device
        
        # Для системного распознавателя используем более короткие чанки (быстрее, как на клавиатуре)
        if self.use_system_recognizer:
            chunk_duration = 5.0  # 5 секунд для быстрой записи
        else:
            chunk_duration = chunk_duration  # Используем переданное значение для Whisper
        
        # Создаем временную директорию для чанков
        # Если output_path это /dev/null или nul, используем системную временную директорию
        if output_path in ["/dev/null", "nul"]:
            import tempfile
            temp_base = Path(tempfile.gettempdir())
            temp_dir = temp_base / "lecture_transcribe_chunks"
        else:
            temp_dir = Path(output_path).parent / "temp_live_chunks"
        
        # Создаем директорию с проверкой прав доступа
        try:
            temp_dir.mkdir(exist_ok=True, parents=True, mode=0o755)
            # Проверяем, что можем писать в директорию
            test_file = temp_dir / ".test_write"
            test_file.touch()
            test_file.unlink()
        except (PermissionError, OSError) as e:
            # Если не можем создать в temp, пробуем в текущей директории
            temp_dir = Path.cwd() / "temp_live_chunks"
            try:
                temp_dir.mkdir(exist_ok=True, parents=True, mode=0o755)
            except Exception:
                raise Exception(f"Не удалось создать временную директорию: {e}")
        
        print(f"\n{'='*60}")
        print(f"{'🎤 ЗАПИСЬ С МИКРОФОНА' if not system_audio else '🔊 ЗАПИСЬ СИСТЕМНОГО ЗВУКА'}")
        print(f"{'='*60}")
        print(f"Язык распознавания: {language}")
        print(f"Транскрипция будет сохраняться в: {output_path}")
        if self.use_system_recognizer:
            print(f"Режим: Системный распознаватель (как на клавиатуре телефона)")
        print(f"Нажмите Ctrl+C для остановки\n")
        
        try:
            if audio_device is None and not system_audio and not self.use_system_recognizer:
                print("Используется устройство по умолчанию для записи")
            
            recording_start_time = time.time()
            chunk_counter = 0
            all_segments = []
            last_segment_end = None
            last_words = []  # Последние слова предыдущего чанка для дедупликации
            
            # Обработчик сигнала для корректной остановки (только в главном потоке)
            def signal_handler(sig, frame):
                print("\n\n⏹️  Получен сигнал остановки...")
                self.is_live_recording = False
            
            # Устанавливаем обработчик сигнала для корректной остановки по Ctrl+C
            # В веб-сервере это будет вызываться из отдельного потока, поэтому пропускаем
            # ValueError возникает, если мы пытаемся установить signal не в главном потоке
            try:
                signal.signal(signal.SIGINT, signal_handler)
            except ValueError:
                # ValueError: signal only works in main thread
                # Это нормально для веб-сервера - остановка происходит через WebSocket и флаг is_live_recording
                pass
            except Exception:
                # Дополнительная защита от любых других ошибок
                # В веб-сервере signal не нужен, остановка через WebSocket
                pass
            
            # Поток для записи аудио
            audio_queue = queue.Queue()
            
            def audio_callback(indata, frames, time_info, status):
                if self.is_live_recording:
                    audio_queue.put(indata.copy())
            
            if not self.use_system_recognizer:
                print("🎤 Начало записи...\n")
            else:
                print()  # Пустая строка для системного распознавателя
            
            # Для ультра-быстрого режима используем меньший blocksize
            blocksize_duration = 0.1 if (self.use_system_recognizer and chunk_duration <= 0.3) else 0.5
            with sd.InputStream(samplerate=16000,
                              channels=1,
                              device=device_id,
                              dtype='float32',
                              callback=audio_callback,
                              blocksize=int(16000 * blocksize_duration)):  # Блоки по 0.1-0.5 секунды
                
                chunk_data = []
                overlap_data = []  # Данные для перекрытия между чанками
                chunk_start_time = time.time()
                # Для ультра-быстрого режима используем меньшее перекрытие
                if self.use_system_recognizer and chunk_duration <= 0.3:
                    overlap_duration = 0.3  # Минимальное перекрытие для ультра-быстрого режима
                else:
                    overlap_duration = 1.5  # Перекрытие 1.5 секунды для предотвращения потери слов
                overlap_samples = int(16000 * overlap_duration)  # Количество сэмплов для перекрытия
                
                # Режим реального времени для системного распознавателя (chunk_duration <= 1.0)
                realtime_mode = self.use_system_recognizer and chunk_duration <= 1.0
                if realtime_mode:
                    # Для ультра-быстрого режима (<= 0.3 сек) используем очень маленький буфер
                    # Для обычного реального времени (0.3-1.0 сек) используем буфер побольше
                    if chunk_duration <= 0.3:
                        # Ультра-быстрый режим: обрабатываем каждые 0.2-0.3 секунды
                        realtime_buffer_duration = max(0.2, chunk_duration)
                    else:
                        # Обычный режим реального времени: буфер 0.5-1 секунда
                        realtime_buffer_duration = min(1.0, chunk_duration * 1.5)
                    realtime_buffer_samples = int(16000 * realtime_buffer_duration)
                    realtime_buffer = []
                    realtime_last_process = time.time()
                
                while self.is_live_recording:
                    try:
                        # Получаем аудио данные с проверкой флага остановки
                        try:
                            audio_block = audio_queue.get(timeout=0.5)
                        except queue.Empty:
                            # Проверяем флаг остановки при таймауте
                            if not self.is_live_recording:
                                break
                            continue
                        
                        current_time = time.time()
                        
                        # Режим реального времени для системного распознавателя
                        if realtime_mode:
                            realtime_buffer.append(audio_block)
                            buffer_samples = sum(len(block) for block in realtime_buffer)
                            
                            # Обрабатываем каждую секунду или когда буфер достаточно большой
                            elapsed_since_process = current_time - realtime_last_process
                            if elapsed_since_process >= realtime_buffer_duration or buffer_samples >= realtime_buffer_samples:
                                if len(realtime_buffer) > 0:
                                    # Объединяем буфер
                                    buffer_array = np.concatenate(realtime_buffer, axis=0)
                                    if len(buffer_array.shape) == 1:
                                        buffer_array = buffer_array.reshape(-1, 1)
                                    
                                    # Добавляем перекрытие если есть
                                    if len(overlap_data) > 0:
                                        overlap_array = np.concatenate(overlap_data, axis=0)
                                        buffer_array = np.concatenate([overlap_array, buffer_array], axis=0)
                                    
                                    # Сохраняем последние данные для перекрытия
                                    total_samples = len(buffer_array)
                                    if total_samples > overlap_samples:
                                        overlap_start_idx = total_samples - overlap_samples
                                        overlap_data = [buffer_array[overlap_start_idx:]]
                                    else:
                                        overlap_data = [buffer_array]
                                    
                                    # Распознаем сразу из буфера
                                    system_lang = get_language_code(language)
                                    text = self.system_recognizer.recognize_audio_data(buffer_array, sample_rate=16000, language=system_lang)
                                    
                                    if text:
                                        # Удаляем дубликаты
                                        words = text.split()
                                        if len(last_words) > 0 and len(words) > 0:
                                            check_len = min(5, len(last_words), len(words))
                                            if check_len > 0:
                                                last_words_check = last_words[-check_len:]
                                                first_words_check = words[:check_len]
                                                
                                                if last_words_check == first_words_check:
                                                    words = words[check_len:]
                                                elif check_len >= 3:
                                                    for i in range(2, check_len + 1):
                                                        if last_words[-i:] == words[:i]:
                                                            words = words[i:]
                                                            break
                                        
                                        if len(words) > 0:
                                            last_words = words[-5:]
                                            text = ' '.join(words)
                                            
                                            if text.strip():
                                                self.write_segment_to_file(f"{text}\n")
                                                if self.text_callback:
                                                    self.text_callback(text)
                                    
                                    # Очищаем буфер
                                    realtime_buffer = []
                                    realtime_last_process = current_time
                            
                            continue  # Пропускаем обычную обработку чанков
                        
                        # Обычный режим (накопление чанков)
                        chunk_data.append(audio_block)
                        elapsed = current_time - chunk_start_time
                        
                        # Когда накопили достаточно данных для чанка
                        if elapsed >= chunk_duration:
                            # Сохраняем чанк
                            chunk_path = temp_dir / f"chunk_{chunk_counter:04d}.wav"
                            try:
                                chunk_array = np.concatenate(chunk_data, axis=0)
                            except Exception as e:
                                print(f"\n⚠️  Ошибка объединения аудио данных: {e}")
                                chunk_data = []
                                chunk_start_time = current_time
                                continue
                            
                            # Добавляем перекрытие с предыдущим чанком (если есть)
                            if len(overlap_data) > 0:
                                overlap_array = np.concatenate(overlap_data, axis=0)
                                # Объединяем перекрытие с текущим чанком
                                chunk_array = np.concatenate([overlap_array, chunk_array], axis=0)
                            
                            # Убеждаемся, что массив правильно сформирован
                            if len(chunk_array.shape) == 1:
                                # Если одномерный массив, добавляем ось для каналов
                                chunk_array = chunk_array.reshape(-1, 1)
                            
                            try:
                                # Убеждаемся, что директория существует и доступна для записи
                                if not temp_dir.exists():
                                    temp_dir.mkdir(exist_ok=True, parents=True, mode=0o755)
                                sf.write(str(chunk_path), chunk_array, 16000, subtype='PCM_16')
                            except (PermissionError, OSError) as e:
                                print(f"\n⚠️  Ошибка записи чанка {chunk_counter}: {e}")
                                # Пробуем альтернативную директорию
                                try:
                                    import tempfile
                                    alt_temp_dir = Path(tempfile.gettempdir()) / f"lecture_chunks_{os.getpid()}"
                                    alt_temp_dir.mkdir(exist_ok=True, parents=True, mode=0o755)
                                    temp_dir = alt_temp_dir
                                    chunk_path = temp_dir / f"chunk_{chunk_counter:04d}.wav"
                                    sf.write(str(chunk_path), chunk_array, 16000, subtype='PCM_16')
                                except Exception as e2:
                                    print(f"⚠️  Не удалось записать в альтернативную директорию: {e2}")
                                    chunk_data = []
                                    chunk_start_time = current_time
                                    continue
                            except Exception as e:
                                print(f"\n⚠️  Ошибка записи чанка {chunk_counter}: {e}")
                                # Пропускаем этот чанк и продолжаем
                                chunk_data = []
                                chunk_start_time = current_time
                                continue
                            
                            # Сохраняем последние данные для перекрытия следующего чанка
                            # Берем последние overlap_samples сэмплов
                            total_samples = len(chunk_array)
                            if total_samples > overlap_samples:
                                # Сохраняем последние overlap_samples для следующего чанка
                                overlap_start_idx = total_samples - overlap_samples
                                overlap_data = [chunk_array[overlap_start_idx:]]
                            else:
                                # Если чанк слишком короткий, сохраняем весь
                                overlap_data = [chunk_array]
                            
                            # Транскрибируем чанк
                            global_offset = chunk_counter * chunk_duration
                            
                            if self.use_system_recognizer:
                                # Используем системный распознаватель (как на клавиатуре телефона)
                                # Без прогресс-баров, сразу пишем в файл
                                system_lang = get_language_code(language)
                                text = self.system_recognizer.recognize_audio_file(str(chunk_path), language=system_lang)
                                
                                if text:
                                    # Удаляем дубликаты из перекрывающейся части
                                    words = text.split()
                                    if len(last_words) > 0 and len(words) > 0:
                                        # Проверяем, не начинается ли новый текст с последних слов предыдущего
                                        # Сравниваем последние 3-5 слов предыдущего чанка с первыми словами нового
                                        check_len = min(5, len(last_words), len(words))
                                        if check_len > 0:
                                            last_words_check = last_words[-check_len:]
                                            first_words_check = words[:check_len]
                                            
                                            # Если совпадают, удаляем дубликаты
                                            if last_words_check == first_words_check:
                                                words = words[check_len:]
                                            # Если частично совпадают (например, последние 2 из 3)
                                            elif check_len >= 3:
                                                for i in range(2, check_len + 1):
                                                    if last_words[-i:] == words[:i]:
                                                        words = words[i:]
                                                        break
                                    
                                    # Обновляем последние слова для следующего чанка
                                    if len(words) > 0:
                                        last_words = words[-5:]  # Сохраняем последние 5 слов
                                        text = ' '.join(words)
                                        
                                        # Сразу пишем в файл без временных меток (как на клавиатуре)
                                        if text.strip():  # Только если остался текст после удаления дубликатов
                                            self.write_segment_to_file(f"{text}\n")
                                            # Отправляем текст в GUI через callback
                                            if self.text_callback:
                                                self.text_callback(text)
                                            all_segments.append({
                                                'start': global_offset,
                                                'end': global_offset + chunk_duration,
                                                'text': text
                                            })
                                
                                pause_duration = 0.0
                                last_end = global_offset + chunk_duration if text else last_segment_end
                                
                                # Показываем только простой статус
                                total_time = time.time() - recording_start_time
                                print(f"✓ {self.format_time(total_time)} | Сегментов: {len(all_segments)}")
                            else:
                                # Используем Whisper (с прогресс-барами)
                                print(f"[Чанк {chunk_counter + 1}] Транскрибация ({self.format_time(global_offset)})...", end=' ', flush=True)
                                
                                start_transcribe = time.time()
                                
                                segments, pause_duration, last_end = self.process_chunk_streaming(
                                    str(chunk_path),
                                    global_offset,
                                    language,
                                    last_segment_end
                                )
                                
                                transcribe_time = time.time() - start_transcribe
                                print(f"✓ ({transcribe_time:.1f}с)")
                                
                                # Обрабатываем паузу
                                if pause_duration >= self.min_pause_duration and chunk_counter > 0:
                                    pause_text = f"\n[ПАУЗА: {pause_duration:.1f} сек]\n\n"
                                    self.write_segment_to_file(pause_text)
                                
                                # Записываем сегменты в файл
                                for segment in segments:
                                    segment_start = segment['start']
                                    segment_end = segment['end']
                                    text = self.post_process_text(segment.get('text', ''))
                                    
                                    # Пропускаем пустые или слишком короткие сегменты
                                    if not text or len(text.strip()) < 3:
                                        continue
                                    
                                    time_info = f"[{self.format_time(segment_start)} - {self.format_time(segment_end)}]"
                                    segment_text = f"{time_info} {text}\n"
                                    
                                    self.write_segment_to_file(segment_text)
                                    # Отправляем текст в GUI через callback (без временных меток)
                                    if self.text_callback:
                                        self.text_callback(text)
                                    all_segments.append(segment)
                                    
                                    # Сохраняем контекст для следующего чанка
                                    if text:
                                        self.previous_text = text[-100:]  # Последние 100 символов
                            
                            last_segment_end = last_end
                            chunk_counter += 1
                            
                            # Очищаем данные для следующего чанка
                            chunk_data = []
                            chunk_start_time = current_time
                            
                            # Удаляем обработанный чанк
                            try:
                                chunk_path.unlink()
                            except:
                                pass
                            
                            # Показываем статус (только для Whisper)
                            if not self.use_system_recognizer:
                                total_time = time.time() - recording_start_time
                                print(f"  Время записи: {self.format_time(total_time)} | Сегментов: {len(all_segments)}")
                    
                    except queue.Empty:
                        continue
                    except KeyboardInterrupt:
                        break
            
            # Обрабатываем оставшиеся данные
            if chunk_data:
                chunk_path = temp_dir / f"chunk_{chunk_counter:04d}.wav"
                chunk_array = np.concatenate(chunk_data, axis=0)
                
                # Добавляем перекрытие с предыдущим чанком (если есть)
                if len(overlap_data) > 0:
                    overlap_array = np.concatenate(overlap_data, axis=0)
                    chunk_array = np.concatenate([overlap_array, chunk_array], axis=0)
                
                # Убеждаемся, что массив правильно сформирован
                if len(chunk_array.shape) == 1:
                    chunk_array = chunk_array.reshape(-1, 1)
                
                try:
                    sf.write(str(chunk_path), chunk_array, 16000, subtype='PCM_16')
                except Exception as e:
                    print(f"\n⚠️  Ошибка записи финального чанка: {e}")
                    # Не прерываем, просто пропускаем финальный чанк
                    return output_path
                
                global_offset = chunk_counter * chunk_duration
                
                if self.use_system_recognizer:
                    # Используем системный распознаватель (без сообщений, сразу пишем)
                    system_lang = get_language_code(language)
                    text = self.system_recognizer.recognize_audio_file(str(chunk_path), language=system_lang)
                    
                    if text:
                        # Удаляем дубликаты из перекрывающейся части
                        words = text.split()
                        if len(last_words) > 0 and len(words) > 0:
                            # Проверяем, не начинается ли новый текст с последних слов предыдущего
                            check_len = min(5, len(last_words), len(words))
                            if check_len > 0:
                                last_words_check = last_words[-check_len:]
                                first_words_check = words[:check_len]
                                
                                # Если совпадают, удаляем дубликаты
                                if last_words_check == first_words_check:
                                    words = words[check_len:]
                                # Если частично совпадают
                                elif check_len >= 3:
                                    for i in range(2, check_len + 1):
                                        if last_words[-i:] == words[:i]:
                                            words = words[i:]
                                            break
                        
                        if len(words) > 0:
                            text = ' '.join(words)
                            # Сразу пишем в файл без временных меток (как на клавиатуре)
                            if text.strip():
                                self.write_segment_to_file(f"{text}\n")
                                # Отправляем текст в GUI через callback
                                if self.text_callback:
                                    self.text_callback(text)
                                all_segments.append({
                                    'start': global_offset,
                                    'end': global_offset + len(chunk_array) / 16000,
                                    'text': text
                                })
                else:
                    # Используем Whisper (с сообщениями)
                    print(f"\n[Финальный чанк] Транскрибация...", end=' ', flush=True)
                    
                    segments, _, _ = self.process_chunk_streaming(
                        str(chunk_path),
                        global_offset,
                        language,
                        last_segment_end
                    )
                    
                    for segment in segments:
                        segment_start = segment['start']
                        segment_end = segment['end']
                        text = self.post_process_text(segment.get('text', ''))
                        
                        # Пропускаем пустые или слишком короткие сегменты
                        if not text or len(text.strip()) < 3:
                            continue
                        
                        time_info = f"[{self.format_time(segment_start)} - {self.format_time(segment_end)}]"
                        segment_text = f"{time_info} {text}\n"
                        
                        self.write_segment_to_file(segment_text)
                        # Отправляем текст в GUI через callback (без временных меток)
                        if self.text_callback:
                            self.text_callback(text)
                        all_segments.append(segment)
                        
                        # Сохраняем контекст для следующего чанка
                        if text:
                            self.previous_text = text[-100:]  # Последние 100 символов
                    
                    print("✓")
                
                try:
                    chunk_path.unlink()
                except:
                    pass
            
            # Очищаем временную директорию
            try:
                temp_dir.rmdir()
            except:
                pass
            
            # Добавляем итоговую информацию
            total_time = time.time() - recording_start_time
            summary_text = f"\n\n{'='*60}\n"
            summary_text += f"Запись завершена: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            summary_text += f"Общее время: {self.format_time(total_time)}\n"
            summary_text += f"Всего сегментов: {len(all_segments)}\n"
            summary_text += f"{'='*60}\n"
            self.write_segment_to_file(summary_text)
            
            print(f"\n{'='*60}")
            print(f"✓ Запись и транскрибация завершены!")
            print(f"  Всего сегментов: {len(all_segments)}")
            print(f"  Время записи: {self.format_time(total_time)}")
            print(f"  Транскрипция сохранена в: {output_path}")
            print(f"{'='*60}\n")
            
            return output_path
            
        except Exception as e:
            print(f"\n✗ Ошибка при записи: {e}")
            import traceback
            traceback.print_exc()
            self.is_live_recording = False
            raise
    
    def process_lecture(self, 
                       audio_path: str,
                       language: str = "ru",
                       output_path: str = None,
                       auth_token: str = None,
                       include_speakers: bool = True) -> str:
        """
        Полный процесс обработки лекции.
        
        Args:
            audio_path: Путь к аудио файлу
            language: Язык аудио
            output_path: Путь для сохранения результата (опционально)
            auth_token: Токен Hugging Face для диаризации
            include_speakers: Включать ли информацию о спикерах
        
        Returns:
            Отформатированный текст транскрипции
        """
        # Проверяем существование файла
        if not os.path.exists(audio_path):
            raise FileNotFoundError(
                f"\n✗ Файл не найден: {audio_path}\n"
                f"  Проверьте, что путь к файлу указан правильно.\n"
                f"  Пример: python3 transcribe_lecture.py ваш_файл.mp3"
            )
        
        # Загружаем pipeline диаризации если нужно
        if include_speakers and not self.diarization_pipeline:
            self.load_diarization_pipeline(auth_token)
        
        # Транскрибация
        transcription = self.transcribe_audio(audio_path, language)
        
        # Диаризация спикеров
        speaker_segments = []
        if include_speakers and self.diarization_pipeline:
            speaker_segments = self.diarize_speakers(audio_path)
            if speaker_segments:
                transcription['segments'] = self.assign_speakers_to_segments(
                    transcription['segments'],
                    speaker_segments
                )
        
        # Определение пауз
        print("Определение пауз...")
        pauses = self.detect_pauses(audio_path)
        print(f"Найдено пауз: {len(pauses)}")
        
        # Форматирование результата
        output_text = self.format_output(transcription, pauses, include_speakers)
        
        # Сохранение результата
        if output_path:
            with open(output_path, 'w', encoding='utf-8', errors='replace') as f:
                f.write(output_text)
            print(f"Результат сохранен в: {output_path}")
            
            # Также сохраняем JSON с полной информацией
            json_path = output_path.replace('.txt', '.json')
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'transcription': transcription,
                    'pauses': pauses,
                    'speakers': speaker_segments
                }, f, ensure_ascii=False, indent=2)
            print(f"Полные данные сохранены в: {json_path}")
        
        return output_text


def main():
    parser = argparse.ArgumentParser(
        description="Транскрибация аудио лекций с учетом пауз и нескольких спикеров"
    )
    parser.add_argument(
        "audio_file",
        type=str,
        nargs='?',
        default=None,
        help="Путь к аудио файлу (не требуется при использовании --record)"
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default=None,
        help="Путь для сохранения результата (по умолчанию: <имя_файла>_transcript.txt)"
    )
    parser.add_argument(
        "-l", "--language",
        type=str,
        default="ru",
        help="Язык аудио (ru, en, и т.д., по умолчанию: ru)"
    )
    parser.add_argument(
        "-m", "--model",
        type=str,
        default="base",
        choices=["tiny", "base", "small", "medium", "large"],
        help="Модель Whisper (по умолчанию: base)"
    )
    parser.add_argument(
        "--min-pause",
        type=float,
        default=1.0,
        help="Минимальная длительность паузы в секундах (по умолчанию: 1.0)"
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Hugging Face токен для диаризации спикеров (опционально)"
    )
    parser.add_argument(
        "--no-speakers",
        action="store_true",
        help="Отключить диаризацию спикеров"
    )
    parser.add_argument(
        "--streaming",
        action="store_true",
        help="Использовать потоковую обработку в реальном времени (запись в файл по мере обработки)"
    )
    parser.add_argument(
        "--chunk-duration",
        type=float,
        default=30.0,
        help="Длительность чанка в секундах для потоковой обработки (по умолчанию: 30.0)"
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Запись с микрофона/динамиков в реальном времени с транскрибацией"
    )
    parser.add_argument(
        "--system-audio",
        action="store_true",
        help="Записывать системный звук (динамики) вместо микрофона. Для macOS требуется BlackHole."
    )
    parser.add_argument(
        "--audio-device",
        type=int,
        default=None,
        help="ID аудио устройства для записи (используйте --list-devices для просмотра)"
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Показать список доступных аудио устройств и выйти"
    )
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        choices=["cpu", "cuda", "mps"],
        help="Устройство для обработки (cpu/cuda/mps, автоматически определяется)"
    )
    parser.add_argument(
        "--use-system-recognizer",
        action="store_true",
        help="Использовать системный распознаватель речи (как на телефоне) вместо Whisper"
    )
    parser.add_argument(
        "--recognizer-type",
        type=str,
        default="google",
        choices=["google", "sphinx"],
        help="Тип системного распознавателя (google - онлайн, sphinx - офлайн, по умолчанию: google)"
    )
    
    args = parser.parse_args()
    
    # Показываем устройства и выходим
    if args.list_devices:
        if not AUDIO_AVAILABLE:
            print("⚠️  sounddevice не установлен. Установите: pip install sounddevice soundfile")
            sys.exit(1)
        recorder = AudioRecorder()
        recorder.list_devices()
        sys.exit(0)
    
    # Режим записи в реальном времени
    if args.record:
        if not AUDIO_AVAILABLE:
            print("✗ Ошибка: sounddevice не установлен.")
            print("  Установите: pip install sounddevice soundfile")
            sys.exit(1)
        
        # Определяем путь для вывода
        if not args.output:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            source = "system" if args.system_audio else "mic"
            args.output = f"lecture_transcript_{source}_{timestamp}.txt"
        
        # Создаем транскрибер
        transcriber = LectureTranscriber(
            whisper_model=args.model,
            min_pause_duration=args.min_pause,
            device=args.device,
            use_system_recognizer=args.use_system_recognizer,
            recognizer_type=args.recognizer_type
        )
        
        # Запускаем запись и транскрибацию
        result_path = transcriber.record_and_transcribe_live(
            output_path=args.output,
            language=args.language,
            audio_device=args.audio_device,
            system_audio=args.system_audio,
            chunk_duration=args.chunk_duration
        )
        
        print(f"\n✓ Транскрипция сохранена в: {result_path}")
        sys.exit(0)
    
    # Проверяем наличие аудио файла для обычной обработки
    if not args.audio_file:
        parser.error("audio_file обязателен, если не используется --record")
    
    # Определяем путь для вывода
    if not args.output:
        audio_path = Path(args.audio_file)
        args.output = str(audio_path.parent / f"{audio_path.stem}_transcript.txt")
    
    # Создаем транскрибер
    transcriber = LectureTranscriber(
        whisper_model=args.model,
        min_pause_duration=args.min_pause,
        device=args.device,
        use_system_recognizer=args.use_system_recognizer,
        recognizer_type=args.recognizer_type
    )
    
    # Выбираем режим обработки
    if args.streaming:
        # Потоковая обработка в реальном времени
        result_path = transcriber.process_lecture_streaming(
            audio_path=args.audio_file,
            language=args.language,
            output_path=args.output,
            auth_token=args.token,
            include_speakers=not args.no_speakers,
            chunk_duration=args.chunk_duration
        )
        print(f"\n✓ Транскрипция сохранена в: {result_path}")
        print("\nВы можете открыть файл и следить за прогрессом в реальном времени!")
    else:
        # Обычная обработка (весь файл сразу)
        result = transcriber.process_lecture(
            audio_path=args.audio_file,
            language=args.language,
            output_path=args.output,
            auth_token=args.token,
            include_speakers=not args.no_speakers
        )
        
        print("\n" + "="*50)
        print("ТРАНСКРИПЦИЯ:")
        print("="*50)
        print(result)


if __name__ == "__main__":
    main()

