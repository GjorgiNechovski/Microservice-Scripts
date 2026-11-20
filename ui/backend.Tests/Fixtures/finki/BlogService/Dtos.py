from pydantic import BaseModel


class User(BaseModel):
    userName: str
    firstName: str
    lastName: str
    email: str

class CreateBlogDto(BaseModel):
    title: str
    blog_text: str

