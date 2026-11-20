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

@app.post("/create-comment")
async def create_comment(comment: CommentPydantic, user: User = Depends(get_user_from_request), db: Session = Depends(get_db)):
    ...

@app.post("/delete-comment")
async def delete_comment(comment_id: int, db: Session = Depends(get_db)):
    ...
