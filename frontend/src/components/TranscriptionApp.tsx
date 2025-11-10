import { useState, useEffect, useRef } from "react";
import "./TranscriptionApp.css";

interface TranscriptionState {
  isRecording: boolean;
  text: string;
  error: string | null;
  sessionId: string | null;
}

function TranscriptionApp() {
  const [state, setState] = useState<TranscriptionState>({
    isRecording: false,
    text: "",
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

  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    return () => {
      // Закрываем WebSocket при размонтировании
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  // Удаляем локальную функцию форматирования - используем текст с сервера

  const startRecording = () => {
    const sessionId = `session_${Date.now()}`;

    // Сбрасываем состояние
    setState({
      isRecording: true,
      text: "",
      error: null,
      sessionId,
    });

    // Подключаемся к WebSocket
    const ws = new WebSocket(`ws://localhost:8000/ws/transcribe/${sessionId}`);
    wsRef.current = ws;

    ws.onopen = () => {
      // Отправляем параметры
      ws.send(
        JSON.stringify({
          method,
          recognizer_type:
            method === "system_recognizer" ? recognizerType : undefined,
          source,
          language,
        })
      );
    };

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === "started") {
        // Транскрибация началась
      } else if (data.type === "text") {
        // Сервер отправляет data.text - это уже весь накопленный отформатированный текст
        // Используем его напрямую, так как сервер уже накопил весь текст
        const fullText = data.text || "";
        if (fullText) {
          setState((prev) => ({
            ...prev,
            text: fullText, // Используем полный накопленный текст с сервера
          }));
        }
      } else if (data.type === "complete") {
        // Обновляем финальный текст, если он есть
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
      setState((prev) => ({
        ...prev,
        isRecording: false,
        error: "Ошибка подключения к серверу",
      }));
    };

    ws.onclose = () => {
      setState((prev) => ({
        ...prev,
        isRecording: false,
      }));
    };
  };

  const stopRecording = () => {
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({ type: "stop" }));
      wsRef.current.close();
      wsRef.current = null;
    }
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
          <div className="radio-group">
            <label>
              <input
                type="radio"
                value="ru"
                checked={language === "ru"}
                onChange={(e) => setLanguage(e.target.value)}
                disabled={state.isRecording}
              />
              Русский
            </label>
            <label>
              <input
                type="radio"
                value="en"
                checked={language === "en"}
                onChange={(e) => setLanguage(e.target.value)}
                disabled={state.isRecording}
              />
              English
            </label>
          </div>
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
            if (state.text) {
              // Копируем в буфер обмена перед очисткой
              try {
                await navigator.clipboard.writeText(state.text);
              } catch (err) {
                console.error("Ошибка копирования:", err);
              }
            }
            setState((prev) => ({ ...prev, text: "" }));
          }}
          disabled={!state.text}
        >
          🗑️ Очистить
        </button>
      </div>

      {state.error && <div className="error-message">⚠️ {state.error}</div>}

      <div className="text-panel">
        <label>Транскрипция:</label>
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
