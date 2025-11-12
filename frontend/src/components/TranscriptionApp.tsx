import { useState, useEffect, useRef } from "react";
import "./TranscriptionApp.css";

interface TranscriptionState {
  isRecording: boolean;
  text: string;
  translatedText: string;
  error: string | null;
  sessionId: string | null;
}

function TranscriptionApp() {
  const [state, setState] = useState<TranscriptionState>({
    isRecording: false,
    text: "",
    translatedText: "",
    error: null,
    sessionId: null,
  });

  // Определяем платформу для выбора дефолтного распознавателя
  const getDefaultRecognizerType = (): string => {
    const platform = navigator.platform.toLowerCase();
    if (platform.includes("mac") || platform.includes("darwin")) {
      return "macos";
    } else if (platform.includes("win")) {
      return "windows";
    }
    return "google"; // fallback
  };

  const [method, setMethod] = useState("system_recognizer");
  const [recognizerType, setRecognizerType] = useState(
    getDefaultRecognizerType()
  );
  const [source, setSource] = useState("microphone");
  const [language, setLanguage] = useState("ru");
  const [chunkDuration, setChunkDuration] = useState(0.2);
  const [enableTranslation, setEnableTranslation] = useState(false);
  const [targetLanguage, setTargetLanguage] = useState("en");

  // Автоматически обновляем chunk_duration при смене метода
  useEffect(() => {
    if (method === "system_recognizer") {
      setChunkDuration(0.2); // Ультра-быстрый режим по умолчанию (как на телефоне)
    } else {
      setChunkDuration(30.0);
    }
  }, [method]);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const pingIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const recordingParamsRef = useRef<any>(null);
  const statusPollIntervalRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    return () => {
      // Закрываем WebSocket при размонтировании
      if (wsRef.current) {
        wsRef.current.close();
      }
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (pingIntervalRef.current) {
        clearInterval(pingIntervalRef.current);
      }
      if (statusPollIntervalRef.current) {
        clearInterval(statusPollIntervalRef.current);
      }
    };
  }, []);

  // Обработка видимости страницы и периодическая проверка соединения
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.hidden) {
        // Страница скрыта - соединение может разорваться, но транскрибация продолжается
        // Увеличиваем интервал ping, чтобы не нагружать браузер
      } else {
        // Страница снова видна - немедленно проверяем и переподключаемся при необходимости
        if (state.isRecording) {
          if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
            reconnectAttemptsRef.current = 0; // Сбрасываем счетчик для быстрого переподключения
            reconnectWebSocket();
          }
        }
      }
    };

    // Периодическая проверка соединения (даже когда страница в фоне)
    const connectionCheckInterval = setInterval(() => {
      if (state.isRecording && (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN)) {
        // Соединение потеряно, переподключаемся
        reconnectWebSocket();
      }
    }, 3000); // Проверяем каждые 3 секунды для более быстрого обнаружения разрыва

    // Периодический опрос статуса сессии через HTTP (работает даже когда WebSocket недоступен)
    // Это обходной путь для ограничений браузера в неактивных вкладках
    const pollSessionStatus = async () => {
      if (!state.isRecording || !sessionIdRef.current) return;
      
      try {
        // Всегда опрашиваем статус, даже если WebSocket подключен (для надежности в фоне)
        const response = await fetch(`http://localhost:8000/api/session/${sessionIdRef.current}/status`);
        if (response.ok) {
          const data = await response.json();
          if (data.is_recording) {
            // Обновляем текст из статуса (всегда, для работы в фоне)
            setState((prev) => ({
              ...prev,
              text: data.text || prev.text,
              translatedText: data.translated_text || prev.translatedText,
            }));
          } else {
            // Запись остановлена
            setState((prev) => ({
              ...prev,
              isRecording: false,
            }));
          }
        }
      } catch (error) {
        // Игнорируем ошибки опроса
      }
    };

    // Опрашиваем статус каждую секунду для более быстрого обновления в фоне
    statusPollIntervalRef.current = setInterval(pollSessionStatus, 1000);

    document.addEventListener("visibilitychange", handleVisibilityChange);
    
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      clearInterval(connectionCheckInterval);
      if (statusPollIntervalRef.current) {
        clearInterval(statusPollIntervalRef.current);
      }
    };
  }, [state.isRecording]);

  const reconnectWebSocket = () => {
    if (!state.isRecording || !sessionIdRef.current || !recordingParamsRef.current) {
      return;
    }

    // Отменяем предыдущую попытку переподключения, если она есть
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
    }

    // Для первой попытки переподключения - сразу, затем экспоненциальная задержка
    const delay = reconnectAttemptsRef.current === 0 
      ? 100  // Первая попытка - почти сразу
      : Math.min(1000 * Math.pow(2, reconnectAttemptsRef.current - 1), 10000); // Максимум 10 секунд
    reconnectAttemptsRef.current++;

    reconnectTimeoutRef.current = setTimeout(() => {
      if (!state.isRecording) return;

      try {
        const ws = new WebSocket(`ws://localhost:8000/ws/transcribe/${sessionIdRef.current}`);
        wsRef.current = ws;

        ws.onopen = () => {
          reconnectAttemptsRef.current = 0; // Сброс счетчика при успешном подключении
          // Отправляем параметры (для переподключения сервер восстановит состояние)
          ws.send(
            JSON.stringify({
              method: recordingParamsRef.current.method,
              recognizer_type: recordingParamsRef.current.recognizer_type,
              source: recordingParamsRef.current.source,
              language: recordingParamsRef.current.language,
              chunk_duration: recordingParamsRef.current.chunk_duration,
              enable_translation: recordingParamsRef.current.enable_translation,
              target_language: recordingParamsRef.current.target_language,
            })
          );
          setupWebSocketHandlers(ws);
        };

        ws.onerror = (error) => {
          // Пробуем переподключиться снова
          if (state.isRecording) {
            reconnectWebSocket();
          }
        };

        ws.onclose = (event) => {
          // Переподключаемся только если это не нормальное закрытие (код 1000)
          if (state.isRecording && event.code !== 1000) {
            reconnectWebSocket();
          }
        };
      } catch (error) {
        // Ошибка создания WebSocket - пробуем снова
        if (state.isRecording) {
          reconnectWebSocket();
        }
      }
    }, delay);
  };

  const setupWebSocketHandlers = (ws: WebSocket) => {
    // Ping каждые 10 секунд для поддержания соединения (чаще для надежности)
    if (pingIntervalRef.current) {
      clearInterval(pingIntervalRef.current);
    }
    pingIntervalRef.current = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify({ type: "ping" }));
        } catch (e) {
          // Если не удалось отправить ping, переподключаемся
          if (state.isRecording) {
            reconnectWebSocket();
          }
        }
      } else if (state.isRecording && (!ws || ws.readyState === WebSocket.CLOSED)) {
        // Соединение закрыто, переподключаемся
        reconnectWebSocket();
      }
    }, 10000); // Уменьшено до 10 секунд для более быстрого обнаружения разрыва

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === "pong") {
        // Ответ на ping - соединение активно
        return;
      }

      if (data.type === "started") {
        // Транскрибация началась
      } else if (data.type === "reconnected") {
        // Переподключение - восстанавливаем состояние
        setState((prev) => ({
          ...prev,
          text: data.text || "",
          translatedText: data.translated_text || "",
        }));
      } else if (data.type === "text") {
        // Сервер отправляет data.text - это уже весь накопленный отформатированный текст
        const fullText = data.text || "";
        if (fullText) {
          setState((prev) => ({
            ...prev,
            text: fullText,
          }));
        }
      } else if (data.type === "translated_text") {
        // Перевод приходит отдельно с задержкой
        const translatedText = data.translated_text || "";
        if (translatedText) {
          setState((prev) => ({
            ...prev,
            translatedText: translatedText,
          }));
        }
      } else if (data.type === "complete") {
        if (data.text) {
          setState((prev) => ({
            ...prev,
            text: data.text,
            isRecording: false,
            error: null,
          }));
        } else {
          setState((prev) => ({
            ...prev,
            isRecording: false,
            error: null,
          }));
        }
      } else if (data.type === "stopped") {
        setState((prev) => ({
          ...prev,
          isRecording: false,
          error: null,
        }));
        if (pingIntervalRef.current) {
          clearInterval(pingIntervalRef.current);
        }
      } else if (data.type === "cleared") {
        setState((prev) => ({
          ...prev,
          text: "",
          translatedText: "",
        }));
      } else if (data.type === "error") {
        setState((prev) => ({
          ...prev,
          isRecording: false,
          error: data.message,
        }));
      }
    };

    ws.onerror = (error) => {
      console.error("WebSocket error:", error);
      // Не останавливаем запись при ошибке - пробуем переподключиться
      if (state.isRecording) {
        reconnectWebSocket();
      }
    };

    ws.onclose = (event) => {
      // Всегда переподключаемся, если запись еще идет (кроме нормального закрытия при остановке)
      if (state.isRecording) {
        // Небольшая задержка перед переподключением
        setTimeout(() => {
          if (state.isRecording) {
            reconnectWebSocket();
          }
        }, 500);
      }
    };
  };

  // Удаляем локальную функцию форматирования - используем текст с сервера

  const startRecording = () => {
    const sessionId = `session_${Date.now()}`;
    sessionIdRef.current = sessionId;
    reconnectAttemptsRef.current = 0;

    // Сохраняем параметры для переподключения
    recordingParamsRef.current = {
      method,
      recognizer_type: method === "system_recognizer" ? recognizerType : undefined,
      source,
      language,
      chunk_duration: chunkDuration,
      enable_translation: enableTranslation,
      target_language: targetLanguage,
    };

    // Сбрасываем состояние
    setState({
      isRecording: true,
      text: "",
      translatedText: "",
      error: null,
      sessionId,
    });

    // Подключаемся к WebSocket
    const ws = new WebSocket(`ws://localhost:8000/ws/transcribe/${sessionId}`);
    wsRef.current = ws;

    ws.onopen = () => {
      // Отправляем параметры
      ws.send(JSON.stringify(recordingParamsRef.current));
    };

    setupWebSocketHandlers(ws);
  };

  const stopRecording = () => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (pingIntervalRef.current) {
      clearInterval(pingIntervalRef.current);
      pingIntervalRef.current = null;
    }
    if (statusPollIntervalRef.current) {
      clearInterval(statusPollIntervalRef.current);
      statusPollIntervalRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({ type: "stop" }));
      wsRef.current.close();
      wsRef.current = null;
    }
    sessionIdRef.current = null;
    recordingParamsRef.current = null;
    setState((prev) => ({
      ...prev,
      isRecording: false,
    }));
  };

  const copyText = () => {
    if (state.text) {
      navigator.clipboard
        .writeText(state.text)
        .then(() => {
          alert("Текст скопирован в буфер обмена!");
        })
        .catch((err) => {
          console.error("Ошибка копирования:", err);
          alert("Не удалось скопировать текст");
        });
    } else {
      alert("Нет текста для копирования");
    }
  };

  const saveText = async () => {
    if (!state.text) {
      alert("Нет текста для сохранения");
      return;
    }

    // Если есть активная сессия, сохраняем через API
    if (state.sessionId) {
      try {
        const response = await fetch(
          `http://localhost:8000/api/save/${state.sessionId}`,
          {
            method: "POST",
          }
        );
        const data = await response.json();
        if (data.success) {
          alert(`Текст сохранен в файл: ${data.file}`);
        } else {
          throw new Error(data.detail || "Ошибка сохранения");
        }
      } catch (error) {
        console.error("Ошибка сохранения через API:", error);
        // Fallback: сохраняем локально
        const blob = new Blob([state.text], {
          type: "text/plain;charset=utf-8",
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `transcript_${new Date().toISOString().replace(/[:.]/g, "-")}.txt`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      }
    } else {
      // Сохраняем локально, если нет сессии
      const blob = new Blob([state.text], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `transcript_${new Date().toISOString().replace(/[:.]/g, "-")}.txt`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }
  };

  return (
    <div className="transcription-app">
      <header className="app-header">
        <h1>🎤 Транскрибатор лекций</h1>
      </header>

      <div className="settings-panel">
        <div className="setting-group">
          <label>Источник звука:</label>
          <div className="radio-group">
            <label>
              <input
                type="radio"
                value="microphone"
                checked={source === "microphone"}
                onChange={(e) => setSource(e.target.value)}
                disabled={state.isRecording}
              />
              Микрофон
            </label>
            <label>
              <input
                type="radio"
                value="system"
                checked={source === "system"}
                onChange={(e) => setSource(e.target.value)}
                disabled={state.isRecording}
              />
              Системный звук
            </label>
          </div>
        </div>

        <div className="setting-group">
          <label>Метод распознавания:</label>
          <div className="radio-group">
            <label>
              <input
                type="radio"
                value="whisper_base"
                checked={method === "whisper_base"}
                onChange={(e) => setMethod(e.target.value)}
                disabled={state.isRecording}
              />
              Whisper Base (офлайн)
            </label>
            <label>
              <input
                type="radio"
                value="whisper_small"
                checked={method === "whisper_small"}
                onChange={(e) => setMethod(e.target.value)}
                disabled={state.isRecording}
              />
              Whisper Small (офлайн)
            </label>
            <label>
              <input
                type="radio"
                value="whisper_medium"
                checked={method === "whisper_medium"}
                onChange={(e) => setMethod(e.target.value)}
                disabled={state.isRecording}
              />
              Whisper Medium (офлайн)
            </label>
            <label>
              <input
                type="radio"
                value="system_recognizer"
                checked={method === "system_recognizer"}
                onChange={(e) => setMethod(e.target.value)}
                disabled={state.isRecording}
              />
              Системный распознаватель (как на телефоне)
            </label>
          </div>
        </div>

        {method === "system_recognizer" && (
          <div className="setting-group">
            <label>Тип системного распознавателя:</label>
            <div className="radio-group">
              <label>
                <input
                  type="radio"
                  value="google"
                  checked={recognizerType === "google"}
                  onChange={(e) => setRecognizerType(e.target.value)}
                  disabled={state.isRecording}
                />
                Google (онлайн, быстро)
              </label>
              {(navigator.platform.toLowerCase().includes("mac") ||
                navigator.platform.toLowerCase().includes("darwin")) && (
                <label>
                  <input
                    type="radio"
                    value="macos"
                    checked={recognizerType === "macos"}
                    onChange={(e) => setRecognizerType(e.target.value)}
                    disabled={state.isRecording}
                  />
                  macOS Speech (офлайн, как на iPhone)
                </label>
              )}
              {navigator.platform.toLowerCase().includes("win") && (
                <label>
                  <input
                    type="radio"
                    value="windows"
                    checked={recognizerType === "windows"}
                    onChange={(e) => setRecognizerType(e.target.value)}
                    disabled={state.isRecording}
                  />
                  Windows Speech (офлайн, встроенный)
                </label>
              )}
              <label>
                <input
                  type="radio"
                  value="sphinx"
                  checked={recognizerType === "sphinx"}
                  onChange={(e) => setRecognizerType(e.target.value)}
                  disabled={state.isRecording}
                />
                PocketSphinx (офлайн, только английский)
              </label>
            </div>
          </div>
        )}

        <div className="setting-group">
          <label>Язык:</label>
          <select
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
            disabled={state.isRecording}
            className="language-select"
          >
            <option value="ru">Русский</option>
            <option value="en">English</option>
            <option value="uk">Українська</option>
            <option value="de">Deutsch</option>
            <option value="fr">Français</option>
            <option value="es">Español</option>
            <option value="it">Italiano</option>
            <option value="pt">Português</option>
            <option value="pl">Polski</option>
            <option value="zh">中文</option>
            <option value="ja">日本語</option>
            <option value="ko">한국어</option>
            <option value="ar">العربية</option>
            <option value="tr">Türkçe</option>
            <option value="nl">Nederlands</option>
            <option value="sv">Svenska</option>
            <option value="no">Norsk</option>
            <option value="fi">Suomi</option>
            <option value="cs">Čeština</option>
            <option value="hu">Magyar</option>
            <option value="ro">Română</option>
            <option value="bg">Български</option>
            <option value="hr">Hrvatski</option>
            <option value="sk">Slovenčina</option>
            <option value="sl">Slovenščina</option>
            <option value="sr">Српски</option>
            <option value="el">Ελληνικά</option>
            <option value="he">עברית</option>
            <option value="hi">हिन्दी</option>
            <option value="th">ไทย</option>
            <option value="vi">Tiếng Việt</option>
            <option value="id">Bahasa Indonesia</option>
            <option value="ms">Bahasa Melayu</option>
            <option value="tl">Filipino</option>
          </select>
        </div>

        <div className="setting-group">
          <label>
            Интервал сегментов (секунды):{" "}
            <span className="setting-value">{chunkDuration}с</span>
          </label>
          <div className="slider-group">
            <input
              type="range"
              min="0.1"
              max={method === "system_recognizer" ? "10" : "60"}
              step="0.1"
              value={chunkDuration}
              onChange={(e) => setChunkDuration(parseFloat(e.target.value))}
              disabled={state.isRecording}
              className="chunk-duration-slider"
            />
            <div className="slider-labels">
              <span>{method === "system_recognizer" ? "0.1с" : "1с"}</span>
              <span>{method === "system_recognizer" ? "1с" : "30с"}</span>
              <span>{method === "system_recognizer" ? "10с" : "60с"}</span>
            </div>
          </div>
          <div className="setting-hint">
            {method === "system_recognizer"
              ? "Ультра-быстрый режим: 0.1-0.3 секунды для мгновенного отображения по словам (как на телефоне). 0.5-1с = стабильнее, но медленнее"
              : "Рекомендуется: 20-30 секунд для лучшего качества"}
          </div>
        </div>

        <div className="setting-group">
          <label>
            <input
              type="checkbox"
              checked={enableTranslation}
              onChange={(e) => setEnableTranslation(e.target.checked)}
              disabled={state.isRecording}
            />
            Включить перевод
          </label>
          {enableTranslation && (
            <div style={{ marginTop: "10px" }}>
              <label>Язык перевода:</label>
              <select
                value={targetLanguage}
                onChange={(e) => setTargetLanguage(e.target.value)}
                disabled={state.isRecording}
                className="language-select"
                style={{ marginTop: "5px" }}
              >
                <option value="en">English</option>
                <option value="ru">Русский</option>
                <option value="uk">Українська</option>
                <option value="de">Deutsch</option>
                <option value="fr">Français</option>
                <option value="es">Español</option>
                <option value="it">Italiano</option>
                <option value="pt">Português</option>
                <option value="pl">Polski</option>
                <option value="zh">中文</option>
                <option value="ja">日本語</option>
                <option value="ko">한국어</option>
                <option value="ar">العربية</option>
                <option value="tr">Türkçe</option>
              </select>
            </div>
          )}
        </div>
      </div>

      <div className="control-panel">
        <button
          className="btn btn-start"
          onClick={startRecording}
          disabled={state.isRecording}
        >
          ▶ Начать запись
        </button>
        <button
          className="btn btn-stop"
          onClick={stopRecording}
          disabled={!state.isRecording}
        >
          ⏹ Остановить
        </button>
        <button
          className="btn btn-copy"
          onClick={copyText}
          disabled={!state.text}
        >
          📋 Копировать
        </button>
        <button
          className="btn btn-save"
          onClick={saveText}
          disabled={!state.text}
        >
          💾 Сохранить
        </button>
        <button
          className="btn btn-clear"
          onClick={async () => {
            const textToCopy = enableTranslation && state.translatedText 
              ? `${state.text}\n\n--- Перевод ---\n${state.translatedText}`
              : state.text;
            if (textToCopy) {
              // Копируем в буфер обмена перед очисткой
              try {
                await navigator.clipboard.writeText(textToCopy);
              } catch (err) {
                console.error("Ошибка копирования:", err);
              }
            }
            // Очищаем текст на клиенте и отправляем запрос на сервер
            setState((prev) => ({ 
              ...prev, 
              text: "", 
              translatedText: "" 
            }));
            
            // Отправляем запрос на очистку на сервере (чтобы сбросить состояние накопления)
            if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
              wsRef.current.send(JSON.stringify({ type: "clear" }));
            }
          }}
          disabled={!state.text && !state.translatedText}
        >
          🗑️ Очистить
        </button>
      </div>

      {state.error && <div className="error-message">⚠️ {state.error}</div>}

      <div className={`text-panels-container ${enableTranslation ? "has-translation" : ""}`}>
        <div className="text-panel">
          <label>
            Оригинал ({language === "ru" ? "Русский" : language === "en" ? "English" : language}):
          </label>
          <textarea
            className="transcription-text"
            value={state.text}
            onChange={(e) =>
              setState((prev) => ({ ...prev, text: e.target.value }))
            }
            placeholder={
              state.isRecording
                ? "Запись..."
                : "Текст появится здесь... (можно редактировать)"
            }
          />
        </div>

        {enableTranslation && (
          <div className="text-panel">
            <label>
              Перевод ({targetLanguage === "ru" ? "Русский" : targetLanguage === "en" ? "English" : targetLanguage}):
            </label>
            <textarea
              className="transcription-text"
              value={state.translatedText}
              onChange={(e) =>
                setState((prev) => ({ ...prev, translatedText: e.target.value }))
              }
              placeholder={
                state.isRecording
                  ? "Перевод появится здесь..."
                  : "Переведенный текст появится здесь... (можно редактировать)"
              }
            />
          </div>
        )}
      </div>

      <div className="status-bar">
        {state.isRecording ? (
          <span className="status-recording">● Запись...</span>
        ) : (
          <span className="status-ready">Готов к записи</span>
        )}
      </div>
    </div>
  );
}

export default TranscriptionApp;
