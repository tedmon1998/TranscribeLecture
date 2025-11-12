#!/usr/bin/env python3
"""
Веб-сервер для транскрибации лекций.
FastAPI сервер с WebSocket для передачи текста в реальном времени.
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import asyncio
import threading
import time
import json
import os
from pathlib import Path

from transcribe_lecture import LectureTranscriber, AudioRecorder, SYSTEM_RECOGNIZER_AVAILABLE, AUDIO_AVAILABLE

# Импорт для перевода
try:
    from deep_translator import GoogleTranslator
    TRANSLATOR_AVAILABLE = True
except ImportError:
    TRANSLATOR_AVAILABLE = False
    GoogleTranslator = None

app = FastAPI(title="Транскрибатор лекций")

# CORS для фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В продакшене указать конкретный домен
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Глобальные переменные для управления транскрибацией
active_transcribers: dict[str, dict] = {}


class TranscriptionRequest(BaseModel):
    """Модель запроса на транскрибацию."""
    method: str  # whisper_base, whisper_small, whisper_medium, google
    source: str  # microphone, system
    language: str  # ru, en
    session_id: str


class TranscriptionResponse(BaseModel):
    """Модель ответа."""
    success: bool
    message: str
    session_id: Optional[str] = None


def add_logical_line_breaks(text: str, full_text: str, pending_text: str) -> tuple[str, str]:
    """
    Добавляет логические переносы строк на основе предложений.
    Возвращает (formatted_text, new_pending_text)
    """
    import re
    
    if not text:
        return full_text + (pending_text if pending_text else ""), pending_text
    
    # Добавляем новый текст к накопленному
    new_pending = pending_text + " " + text if pending_text else text
    
    # Ищем законченные предложения (заканчиваются на . ! ?)
    sentence_pattern = r'([.!?])\s+([А-ЯЁA-Z])'
    
    # Заменяем концы предложений на перенос строки
    formatted = re.sub(sentence_pattern, r'\1\n\2', new_pending)
    
    # Если есть законченные предложения
    if '\n' in formatted:
        parts = formatted.split('\n')
        # Все части кроме последней - законченные предложения
        completed = '\n'.join(parts[:-1])
        new_full = full_text + completed + "\n"
        # Последняя часть - незаконченное предложение
        new_pending = parts[-1] if parts[-1] else ""
    # Если нет законченных предложений, но текст длинный
    elif len(new_pending) > 150:
        # Ищем запятую для разрыва
        comma_pattern = r'([,;])\s+'
        if re.search(comma_pattern, new_pending):
            parts = re.split(r'([,;])\s+', new_pending, maxsplit=1)
            if len(parts) >= 3:
                new_full = full_text + parts[0] + parts[1] + "\n"
                new_pending = parts[2] if len(parts) > 2 else ""
            else:
                new_full = full_text
        else:
            new_full = full_text
    else:
        new_full = full_text
    
    # Формируем результат
    result = new_full
    if new_pending:
        result += new_pending
    
    # Убираем множественные переносы
    result = re.sub(r'\n{3,}', '\n\n', result)
    
    return result.strip(), new_pending


async def send_websocket_message(websocket: WebSocket, message: dict):
    """Вспомогательная функция для отправки сообщения через WebSocket."""
    try:
        # Проверяем, что WebSocket не закрыт
        if hasattr(websocket, 'client_state') and websocket.client_state.name == "DISCONNECTED":
            return
        await websocket.send_json(message)
    except Exception as e:
        # Игнорируем ошибки, если WebSocket уже закрыт
        error_str = str(e).lower()
        if "close" not in error_str and "disconnect" not in error_str:
            print(f"⚠️  Ошибка отправки WebSocket сообщения: {e}")


def translate_text(text: str, target_lang: str, source_lang: str = "auto") -> str:
    """
    Переводит текст на целевой язык.
    
    Args:
        text: Текст для перевода
        target_lang: Целевой язык (ru, en, и т.д.)
        source_lang: Исходный язык (auto для автоопределения)
    
    Returns:
        Переведенный текст или оригинал при ошибке
    """
    if not TRANSLATOR_AVAILABLE or not text or not text.strip():
        return text
    
    try:
        # Используем deep-translator (более стабильная библиотека)
        if source_lang == "auto":
            # Автоопределение языка
            translator = GoogleTranslator(source='auto', target=target_lang)
        else:
            translator = GoogleTranslator(source=source_lang, target=target_lang)
        
        result = translator.translate(text)
        return result
    except Exception as e:
        # При ошибке возвращаем оригинал
        return text


def transcription_worker(
    session_id: str,
    method: str,
    source: str,
    language: str,
    chunk_duration: float,
    enable_translation: bool,
    target_language: str,
    websocket: WebSocket,
    loop: asyncio.AbstractEventLoop
):
    """Рабочий поток для транскрибации."""
    try:
        # Получаем тип распознавателя из данных (если есть)
        recognizer_type = active_transcribers.get(session_id, {}).get("recognizer_type", "google")
        
        # Определяем параметры
        use_system = method == "system_recognizer"
        system_audio = source == "system"
        
        # Определяем модель Whisper
        whisper_model = "base"
        if method == "whisper_small":
            whisper_model = "small"
        elif method == "whisper_medium":
            whisper_model = "medium"
        
        # Создаем транскрибер
        if use_system:
            transcriber = LectureTranscriber(
                whisper_model="base",
                use_system_recognizer=True,
                recognizer_type=recognizer_type
            )
        else:
            transcriber = LectureTranscriber(
                whisper_model=whisper_model,
                use_system_recognizer=False
            )
        
        # Сохраняем ссылку на transcriber для возможности остановки
        if session_id in active_transcribers:
            active_transcribers[session_id]["transcriber"] = transcriber
        
        # НЕ создаем файл - сохраняем только в память, файл будет создан по запросу
        # Состояние для логических переносов и накопления текста
        # Используем словарь для хранения состояния, чтобы можно было сбрасывать извне
        session_state = active_transcribers.get(session_id, {})
        if "full_text_state" not in session_state:
            session_state["full_text_state"] = {"full": "", "pending": ""}
            session_state["translated_text_state"] = {"full": "", "pending": ""}
            session_state["accumulated_text"] = []
        
        full_text_state = session_state["full_text_state"]
        translated_text_state = session_state["translated_text_state"]
        accumulated_text = session_state["accumulated_text"]
        
        def text_callback(text: str):
            """Callback для получения нового текста."""
            try:
                # Проверяем, была ли очистка - если да, сбрасываем состояние
                session_state = active_transcribers.get(session_id, {})
                if session_state.get("text_cleared", False):
                    # Сбрасываем состояние и убираем флаг
                    full_text_state["full"] = ""
                    full_text_state["pending"] = ""
                    translated_text_state["full"] = ""
                    translated_text_state["pending"] = ""
                    accumulated_text.clear()
                    session_state["text_cleared"] = False
                
                # Убеждаемся, что текст - это строка и правильно закодирован
                if isinstance(text, bytes):
                    text = text.decode('utf-8', errors='replace')
                elif not isinstance(text, str):
                    text = str(text)
                
                # Пропускаем пустой текст
                if not text or not text.strip():
                    return
                
                # Очищаем текст от проблемных символов
                text = text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                
                # Добавляем логические переносы
                # formatted будет содержать весь накопленный текст
                formatted, new_pending = add_logical_line_breaks(
                    text,
                    full_text_state["full"],
                    full_text_state["pending"]
                )
                # formatted содержит весь накопленный текст (new_full + new_pending)
                # где new_full = full_text + completed + "\n" (или просто full_text)
                # Обновляем состояние для следующего вызова
                # Для следующего вызова нужно разделить formatted на new_full и new_pending
                # formatted = new_full + new_pending (если new_pending не пустой)
                # или formatted = new_full (если new_pending пустой)
                if new_pending and formatted.endswith(new_pending):
                    # Извлекаем new_full из formatted
                    full_text_state["full"] = formatted[:-len(new_pending)].rstrip()
                else:
                    # new_pending пустой или не в конце, весь formatted это new_full
                    full_text_state["full"] = formatted
                full_text_state["pending"] = new_pending
                
                # Сохраняем текст в память
                accumulated_text.append(text)
                
                # Отправляем оригинал сразу (в реальном времени)
                # Проверяем, есть ли активное WebSocket соединение
                session_state = active_transcribers.get(session_id, {})
                current_websocket = session_state.get("websocket")
                
                if current_websocket:
                    try:
                        message = {
                            "type": "text",
                            "text": formatted,  # Полный накопленный текст с логическими переносами
                            "new_text": text  # Только новый кусок текста
                        }
                        
                        future = asyncio.run_coroutine_threadsafe(
                            send_websocket_message(current_websocket, message),
                            loop
                        )
                        # Не ждем результата, чтобы не блокировать
                    except Exception as ws_error:
                        # Игнорируем ошибки, если WebSocket закрыт
                        pass
                
                # Переводим текст асинхронно (с задержкой), если включен перевод
                if enable_translation and target_language and target_language != language:
                    def translate_async():
                        """Асинхронный перевод в отдельном потоке."""
                        try:
                            # Переводим только новый текст для скорости
                            translated_new_text = translate_text(text, target_language, language)
                            if translated_new_text and translated_new_text.strip():
                                # Применяем логические переносы к переводу
                                translated_formatted, translated_new_pending = add_logical_line_breaks(
                                    translated_new_text,
                                    translated_text_state["full"],
                                    translated_text_state["pending"]
                                )
                                # Обновляем состояние перевода
                                if translated_new_pending and translated_formatted.endswith(translated_new_pending):
                                    translated_text_state["full"] = translated_formatted[:-len(translated_new_pending)].rstrip()
                                else:
                                    translated_text_state["full"] = translated_formatted
                                translated_text_state["pending"] = translated_new_pending
                                
                                # Отправляем перевод через WebSocket
                                session_state = active_transcribers.get(session_id, {})
                                current_websocket = session_state.get("websocket")
                                if current_websocket:
                                    try:
                                        translate_message = {
                                            "type": "translated_text",
                                            "translated_text": translated_formatted
                                        }
                                        asyncio.run_coroutine_threadsafe(
                                            send_websocket_message(current_websocket, translate_message),
                                            loop
                                        )
                                    except Exception:
                                        pass
                        except Exception:
                            # Игнорируем ошибки перевода
                            pass
                    
                    # Запускаем перевод в отдельном потоке (не блокирует основной поток)
                    threading.Thread(target=translate_async, daemon=True).start()
            except UnicodeDecodeError as e:
                # Пробуем обработать текст с заменой проблемных символов
                try:
                    safe_text = text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                    formatted, new_pending = add_logical_line_breaks(
                        safe_text,
                        full_text_state["full"],
                        full_text_state["pending"]
                    )
                    asyncio.run_coroutine_threadsafe(
                        send_websocket_message(websocket, {
                            "type": "text",
                            "text": formatted,
                            "new_text": safe_text
                        }),
                        loop
                    )
                except Exception:
                    pass
            except Exception:
                pass
        
        # Запускаем транскрибацию БЕЗ сохранения в файл
        # Используем временный путь, но не будем его использовать
        temp_output = "/dev/null" if os.name != 'nt' else "nul"
        
        try:
            transcriber.record_and_transcribe_live(
                output_path=temp_output,
                language=language,
                system_audio=system_audio,
                chunk_duration=chunk_duration,
                text_callback=text_callback
            )
        except Exception as e:
            # Если произошла ошибка, отправляем сообщение
            try:
                asyncio.run_coroutine_threadsafe(
                    send_websocket_message(websocket, {
                        "type": "error",
                        "message": str(e)
                    }),
                    loop
                )
            except:
                pass
            raise
        
        # Сохраняем накопленный текст в сессии
        if session_id in active_transcribers:
            active_transcribers[session_id]["text"] = full_text_state["full"] + (full_text_state["pending"] if full_text_state["pending"] else "")
        
        # Отправляем сообщение о завершении
        try:
            asyncio.run_coroutine_threadsafe(
                send_websocket_message(websocket, {
                    "type": "complete",
                    "text": full_text_state["full"] + (full_text_state["pending"] if full_text_state["pending"] else "")
                }),
                loop
            )
        except:
            pass
        
    except Exception as e:
        error_msg = str(e)
        print(f"Ошибка транскрибации: {error_msg}")
        try:
            asyncio.run_coroutine_threadsafe(
                send_websocket_message(websocket, {
                    "type": "error",
                    "message": error_msg
                }),
                loop
            )
        except:
            pass
    finally:
        # НЕ удаляем сессию автоматически - она будет удалена только при явной остановке
        # Это позволяет переподключаться и восстанавливать состояние
        # Сессия удаляется только в websocket_transcribe при получении сообщения "stop"
        pass


@app.get("/")
async def root():
    """Корневой маршрут."""
    return {"message": "Транскрибатор лекций API"}


@app.get("/api/health")
async def health():
    """Проверка здоровья сервера."""
    return {
        "status": "ok",
        "audio_available": AUDIO_AVAILABLE,
        "system_recognizer_available": SYSTEM_RECOGNIZER_AVAILABLE
    }


@app.get("/api/devices")
async def list_devices():
    """Список доступных аудио устройств."""
    if not AUDIO_AVAILABLE:
        raise HTTPException(status_code=503, detail="Audio recording not available")
    
    try:
        recorder = AudioRecorder()
        devices = recorder.list_audio_devices()
        return {"devices": devices}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/session/{session_id}/status")
async def get_session_status(session_id: str):
    """Получить статус сессии и текущий текст."""
    if session_id not in active_transcribers:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session_state = active_transcribers[session_id]
    transcriber = session_state.get("transcriber")
    
    # Получаем текущий текст
    full_text_state = session_state.get("full_text_state", {"full": "", "pending": ""})
    translated_text_state = session_state.get("translated_text_state", {"full": "", "pending": ""})
    
    current_text = full_text_state.get("full", "") + (full_text_state.get("pending", "") if full_text_state.get("pending") else "")
    current_translated = translated_text_state.get("full", "") + (translated_text_state.get("pending", "") if translated_text_state.get("pending") else "")
    
    return {
        "session_id": session_id,
        "is_recording": transcriber.is_live_recording if transcriber else False,
        "has_websocket": session_state.get("websocket") is not None,
        "text": current_text,
        "translated_text": current_translated if session_state.get("enable_translation") else ""
    }


@app.websocket("/ws/transcribe/{session_id}")
async def websocket_transcribe(websocket: WebSocket, session_id: str):
    """WebSocket endpoint для транскрибации в реальном времени."""
    await websocket.accept()
    
    try:
        # Получаем параметры из первого сообщения
        data = await websocket.receive_json()
        
        method = data.get("method", "whisper_base")
        recognizer_type = data.get("recognizer_type", "google")
        source = data.get("source", "microphone")
        language = data.get("language", "ru")
        chunk_duration = float(data.get("chunk_duration", 0.2 if method == "system_recognizer" else 30.0))
        enable_translation = data.get("enable_translation", False)
        target_language = data.get("target_language", "en")
        
        # Валидация chunk_duration
        if method == "system_recognizer":
            # Для системного распознавателя разрешаем от 0.1 до 10 секунд (0.1 для ультра-быстрого режима)
            if chunk_duration < 0.1:
                chunk_duration = 0.1
            elif chunk_duration > 10.0:
                chunk_duration = 10.0
        else:
            # Для Whisper от 1 до 60 секунд
            if chunk_duration < 1.0:
                chunk_duration = 1.0
            elif chunk_duration > 60.0:
                chunk_duration = 60.0
        
        # Проверяем доступность
        if not AUDIO_AVAILABLE:
            await websocket.send_json({
                "type": "error",
                "message": "Audio recording not available. Install: pip install sounddevice soundfile"
            })
            return
        
        if method == "system_recognizer" and not SYSTEM_RECOGNIZER_AVAILABLE:
            await websocket.send_json({
                "type": "error",
                "message": "System recognizer not available. Install: pip install SpeechRecognition"
            })
            return
        
        # Сохраняем информацию о сессии
        active_transcribers[session_id] = {
            "websocket": websocket,
            "method": method,
            "recognizer_type": recognizer_type,
            "source": source,
            "language": language,
            "chunk_duration": chunk_duration,
            "enable_translation": enable_translation,
            "target_language": target_language
        }
        
        # Проверяем, существует ли уже сессия (переподключение)
        is_reconnect = session_id in active_transcribers and active_transcribers[session_id].get("transcriber") is not None
        
        if is_reconnect:
            # Переподключение - отправляем текущее состояние
            session_state = active_transcribers[session_id]
            full_text_state = session_state.get("full_text_state", {"full": "", "pending": ""})
            translated_text_state = session_state.get("translated_text_state", {"full": "", "pending": ""})
            
            current_text = full_text_state.get("full", "") + (full_text_state.get("pending", "") if full_text_state.get("pending") else "")
            current_translated = translated_text_state.get("full", "") + (translated_text_state.get("pending", "") if translated_text_state.get("pending") else "")
            
            # Обновляем WebSocket в сессии
            active_transcribers[session_id]["websocket"] = websocket
            
            await websocket.send_json({
                "type": "reconnected",
                "session_id": session_id,
                "text": current_text,
                "translated_text": current_translated if enable_translation else ""
            })
        else:
            # Новая сессия
            await websocket.send_json({
                "type": "started",
                "session_id": session_id
            })
        
        # Получаем event loop для использования в worker потоке
        loop = asyncio.get_event_loop()
        
        # Запускаем транскрибацию только если она еще не запущена (не переподключение)
        if not is_reconnect:
            # Запускаем транскрибацию в отдельном потоке
            thread = threading.Thread(
                target=transcription_worker,
                args=(session_id, method, source, language, chunk_duration, enable_translation, target_language, websocket, loop),
                daemon=True
            )
            thread.start()
        
        # Ждем сообщений от клиента (для остановки)
        while True:
            try:
                # Используем timeout, чтобы периодически проверять состояние
                try:
                    message = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
                    if message.get("type") == "ping":
                        # Отвечаем на ping для поддержания соединения
                        await websocket.send_json({"type": "pong"})
                        continue
                    elif message.get("type") == "stop":
                        # Останавливаем транскрибацию
                        print(f"Получен запрос на остановку для сессии {session_id}")
                        if session_id in active_transcribers:
                            transcriber = active_transcribers[session_id].get("transcriber")
                            if transcriber:
                                print(f"Останавливаем транскрибацию...")
                                transcriber.is_live_recording = False
                            # Удаляем сессию только при явной остановке
                            del active_transcribers[session_id]
                        # Отправляем подтверждение остановки
                        await websocket.send_json({
                            "type": "stopped",
                            "message": "Запись остановлена"
                        })
                        break
                    elif message.get("type") == "clear":
                        # Очищаем текст, но продолжаем запись
                        if session_id in active_transcribers:
                            session_state = active_transcribers[session_id]
                            # Полностью сбрасываем состояние накопления
                            session_state["full_text_state"] = {"full": "", "pending": ""}
                            session_state["translated_text_state"] = {"full": "", "pending": ""}
                            session_state["accumulated_text"] = []
                            # Устанавливаем флаг очистки, чтобы text_callback знал, что нужно начинать с нуля
                            session_state["text_cleared"] = True
                        # Отправляем подтверждение очистки
                        await websocket.send_json({
                            "type": "cleared",
                            "message": "Текст очищен"
                        })
                except asyncio.TimeoutError:
                    # Отправляем ping для поддержания соединения
                    try:
                        await websocket.send_json({"type": "ping"})
                    except:
                        pass
                    # Проверяем, не остановлена ли уже запись
                    if session_id in active_transcribers:
                        transcriber = active_transcribers[session_id].get("transcriber")
                        if transcriber and not transcriber.is_live_recording:
                            # Запись остановлена извне
                            break
                    continue
            except WebSocketDisconnect:
                # Клиент отключился, но НЕ останавливаем транскрибацию
                # Транскрибация продолжается в фоне
                print(f"WebSocket отключен для сессии {session_id}, транскрибация продолжается")
                if session_id in active_transcribers:
                    active_transcribers[session_id]["websocket"] = None
                # Выходим из цикла ожидания сообщений, но транскрибация продолжается
                break
            except Exception as e:
                print(f"Ошибка получения сообщения: {e}")
                # Не прерываем транскрибацию при ошибке получения сообщений
                break
        
    except WebSocketDisconnect:
        # Клиент отключился, но НЕ останавливаем транскрибацию
        # Состояние сохраняется, можно переподключиться
        print(f"Клиент отключился для сессии {session_id}, транскрибация продолжается в фоне")
        # НЕ удаляем сессию, чтобы можно было переподключиться
        # Только помечаем, что WebSocket закрыт
        if session_id in active_transcribers:
            active_transcribers[session_id]["websocket"] = None
    except Exception as e:
        print(f"Ошибка WebSocket: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
        except:
            pass
    finally:
        # НЕ удаляем сессию при отключении - только при явной остановке
        # Сессия будет удалена только когда транскрибация остановлена
        try:
            await websocket.close()
        except:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

