// Trimmed copy for the route-finder tests: the route declarations only.
using Microsoft.AspNetCore.Mvc;

namespace UserService.Controllers;

[ApiController]
[Route("[controller]")]
public class UserController : ControllerBase
{
    [HttpGet]
    public IActionResult GetUser()
    {
        return Ok();
    }
}
