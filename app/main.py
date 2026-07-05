from fastapi import FastAPI

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