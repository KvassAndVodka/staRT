#!/usr/bin/env bash
set -e

echo "================================================="
echo " Starting staRT — Local Transcript Service"
echo "================================================="

# Start backend
echo "[1/2] Starting FastAPI Backend on http://127.0.0.1:8000..."
cd backend
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!
cd ..

# Start frontend
echo "[2/2] Starting Next.js Frontend on http://localhost:3000..."
cd frontend
npm run dev -- -p 3000 &
FRONTEND_PID=$!
cd ..

trap "echo 'Shutting down staRT...'; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true" EXIT INT TERM

wait
