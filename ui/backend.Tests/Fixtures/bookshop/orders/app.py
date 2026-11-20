# Trimmed copy for the route-finder tests: the route declarations only.
from fastapi import FastAPI

app = FastAPI()

@app.post("/orders")
def create_order(book_id: int, qty: int = 1):
    ...

@app.get("/orders/{order_id}")
def get_order(order_id: str):
    ...

@app.get("/health")
def health():
    ...
