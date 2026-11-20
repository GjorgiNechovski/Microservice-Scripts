using System.Security.Cryptography;
using System.Text;

namespace MicroserviceScripts.Ui.Infrastructure;

/// <summary>
/// The server can run docker and write files, so it answers only this machine's
/// own browser: requests must name a local host (a site whose domain resolves to
/// 127.0.0.1 is turned away), and API calls must carry the launch token.
/// </summary>
public sealed class LocalOnlyMiddleware(RequestDelegate next, LaunchOptions options)
{
    public const string TokenHeader = "X-Setup-Token";

    private static readonly HashSet<string> LocalHosts =
        new(StringComparer.OrdinalIgnoreCase) { "localhost", "127.0.0.1", "[::1]", "::1" };

    public Task InvokeAsync(HttpContext context)
    {
        if (!LocalHosts.Contains(context.Request.Host.Host))
        {
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return Task.CompletedTask;
        }

        if (context.Request.Path.StartsWithSegments("/api") && !HasToken(context))
        {
            context.Response.StatusCode = StatusCodes.Status401Unauthorized;
            return Task.CompletedTask;
        }

        return next(context);
    }

    private bool HasToken(HttpContext context)
    {
        var sent = Encoding.UTF8.GetBytes(context.Request.Headers[TokenHeader].ToString());
        var expected = Encoding.UTF8.GetBytes(options.Token);
        return CryptographicOperations.FixedTimeEquals(sent, expected);
    }
}
