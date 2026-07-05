from fastapi import FastAPI

from app.database.connection import check_database_connection

app = FastAPI(
    title="OhMySmell API",
    version="0.1.0"
)


@app.get("/")
def root():
    return {
        "status": "OK",
        "message": "OhMySmell Backend is running!"
    }


@app.get("/health/db")
async def health_db():
    is_connected = await check_database_connection()

    return {
        "database": "connected" if is_connected else "not connected"
    }