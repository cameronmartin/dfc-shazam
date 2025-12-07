"""FastAPI application."""
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
async def home():
    return {"message": "Hello from FastAPI!"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
