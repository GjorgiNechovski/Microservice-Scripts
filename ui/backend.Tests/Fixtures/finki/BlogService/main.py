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

@app.get("/blogs")
def get_blogs(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    ...

@app.post("/create")
def create(blog: CreateBlogDto, user: User = Depends(get_user_from_request), db: Session = Depends(get_db)):
    ...

@app.get("/blogs/{blog_id}")
def get_blog(blog_id: int, db: Session = Depends(get_db)):
    ...

@app.post("/deleteBlog/{blog_id}")
def delete_blog(blog_id: int, db: Session = Depends(get_db)):
    ...
