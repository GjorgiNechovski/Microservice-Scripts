# Trimmed copy for the route-finder tests: the route declarations only.
from fastapi import FastAPI

app = FastAPI()

@app.on_event("startup")
def seed() -> None:
    ...

@app.get("/books")
def list_books(limit: int = 20):
    ...

@app.get("/books/{book_id}")
def get_book(book_id: int):
    ...

@app.get("/health")
def health():
    ...
