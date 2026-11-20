using System.Security.Cryptography;

namespace MicroserviceScripts.Ui;

/// <summary>
/// How the setup app was started: <c>--port N</c> and <c>--open</c> (open the
/// browser once the server is up).
/// </summary>
public sealed record LaunchOptions(int Port, bool OpenBrowser, string Token)
{
    public const int DefaultPort = 5199;

    /// <summary>The address to open: the token in it is what lets the page call the API.</summary>
    public string Url => $"http://127.0.0.1:{Port}/?token={Token}";

    public static LaunchOptions Parse(string[] args)
    {
        var port = DefaultPort;
        var open = false;
        for (var i = 0; i < args.Length; i++)
        {
            if (args[i] == "--open")
            {
                open = true;
            }
            else if (args[i] == "--port" && i + 1 < args.Length && int.TryParse(args[i + 1], out var p))
            {
                port = p;
                i++;
            }
        }

        // Every launch gets a fresh token, so only the page this launch opened can
        // drive docker and write files. SETUP_TOKEN fixes it, for development with
        // the Vite dev server (ui/README.md).
        var token = Environment.GetEnvironmentVariable("SETUP_TOKEN");
        if (string.IsNullOrWhiteSpace(token))
        {
            token = Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        }

        return new LaunchOptions(port, open, token);
    }
}
