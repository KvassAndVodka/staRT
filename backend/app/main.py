"""
Main FastAPI Application Entrypoint for staRT.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.adapters.storage.database import init_db
from app.application.job_coordinator import coordinator
from app.api import routes_sessions, routes_editor, routes_export, routes_storage, routes_audio, websocket

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB schema on startup
    print("[staRT] Initializing database...")
    await init_db()
    print("[staRT] Database initialized successfully.")
    
    # Run startup recovery for orphaned or queued jobs
    await coordinator.startup_recovery()
    
    yield
    print("[staRT] Shutting down...")
    await coordinator.shutdown()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include Routers
app.include_router(routes_sessions.router, prefix="/api", tags=["sessions"])
app.include_router(routes_audio.router, prefix="/api", tags=["audio"])
app.include_router(routes_editor.router, prefix="/api", tags=["editor"])
app.include_router(routes_export.router, prefix="/api", tags=["export"])
app.include_router(routes_storage.router, prefix="/api", tags=["storage"])
app.include_router(websocket.router, tags=["websocket"])

@app.get("/")
async def root():
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs_url": "/docs"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
