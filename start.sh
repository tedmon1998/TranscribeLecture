#!/bin/bash

# Скрипт для запуска сервера и фронтенда транскрибатора лекций

# Цвета для вывода
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Функция для очистки при выходе
cleanup() {
    echo -e "\n${YELLOW}Остановка процессов...${NC}"
    if [ ! -z "$SERVER_PID" ]; then
        kill $SERVER_PID 2>/dev/null
        echo -e "${GREEN}✓ Сервер остановлен${NC}"
    fi
    if [ ! -z "$FRONTEND_PID" ]; then
        kill $FRONTEND_PID 2>/dev/null
        echo -e "${GREEN}✓ Фронтенд остановлен${NC}"
    fi
    exit 0
}

# Устанавливаем обработчик сигналов
trap cleanup SIGINT SIGTERM

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Запуск транскрибатора лекций${NC}"
echo -e "${BLUE}========================================${NC}\n"

# Проверяем наличие Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}✗ Python3 не найден!${NC}"
    exit 1
fi

# Проверяем наличие Node.js
if ! command -v node &> /dev/null; then
    echo -e "${RED}✗ Node.js не найден!${NC}"
    exit 1
fi

# Проверяем наличие npm
if ! command -v npm &> /dev/null; then
    echo -e "${RED}✗ npm не найден!${NC}"
    exit 1
fi

# Получаем директорию скрипта
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Проверяем зависимости Python
echo -e "${YELLOW}Проверка зависимостей Python...${NC}"
if ! python3 -c "import fastapi" 2>/dev/null; then
    echo -e "${YELLOW}Установка зависимостей Python...${NC}"
    pip install -r requirements.txt
fi

# Проверяем зависимости Node.js
echo -e "${YELLOW}Проверка зависимостей Node.js...${NC}"
if [ ! -d "frontend/node_modules" ]; then
    echo -e "${YELLOW}Установка зависимостей Node.js...${NC}"
    cd frontend
    npm install
    cd ..
fi

# Запускаем сервер
echo -e "\n${GREEN}Запуск сервера (порт 8000)...${NC}"
python3 server.py > server.log 2>&1 &
SERVER_PID=$!

# Ждем немного, чтобы сервер запустился
sleep 2

# Проверяем, что сервер запустился
if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo -e "${RED}✗ Ошибка запуска сервера!${NC}"
    echo -e "${YELLOW}Проверьте server.log для деталей${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Сервер запущен (PID: $SERVER_PID)${NC}"

# Запускаем фронтенд
echo -e "\n${GREEN}Запуск фронтенда...${NC}"
cd frontend
npm run dev > ../frontend.log 2>&1 &
FRONTEND_PID=$!
cd ..

# Ждем немного, чтобы фронтенд запустился
sleep 3

# Проверяем, что фронтенд запустился
if ! kill -0 $FRONTEND_PID 2>/dev/null; then
    echo -e "${RED}✗ Ошибка запуска фронтенда!${NC}"
    echo -e "${YELLOW}Проверьте frontend.log для деталей${NC}"
    kill $SERVER_PID 2>/dev/null
    exit 1
fi

echo -e "${GREEN}✓ Фронтенд запущен (PID: $FRONTEND_PID)${NC}"

echo -e "\n${BLUE}========================================${NC}"
echo -e "${GREEN}✓ Все сервисы запущены!${NC}"
echo -e "${BLUE}========================================${NC}"
echo -e "\n${YELLOW}Сервер:${NC} http://localhost:8000"
echo -e "${YELLOW}Фронтенд:${NC} http://localhost:5173 (или другой порт)"
echo -e "\n${YELLOW}Логи сервера:${NC} server.log"
echo -e "${YELLOW}Логи фронтенда:${NC} frontend.log"
echo -e "\n${YELLOW}Нажмите Ctrl+C для остановки${NC}\n"

# Ждем завершения процессов
wait

