# Trimmed copy for the route-finder tests: the route declarations only.
from fastapi import FastAPI, Depends, Query
from Dtos import *

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    ...

@app.on_event("shutdown")
async def shutdown_event():
    ...

@app.post("/like")
async def like_post(blog_id: int, user: User = Depends(get_user_from_request), db: Session = Depends(get_db)):
    ...

@app.get("/has-liked/{blog_id}")
async def has_liked(blog_id: int, user: User = Depends(get_user_from_request), db: Session = Depends(get_db)) -> bool:
    ...
