// Trimmed copy for the route-finder tests: the route declarations only.
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace UserService.Controllers;

[ApiController]
[Route("[controller]")]
[AllowAnonymous]
public class AuthenticationController : ControllerBase
{
    [HttpPost("register")]
    public async Task<ActionResult> Register([FromBody] RegisterDto registerDto)
    {
        return Ok();
    }

    [HttpPost("login")]
    public async Task<ActionResult> Register([FromBody] LoginDto loginDto)
    {
        return Ok();
    }
}
