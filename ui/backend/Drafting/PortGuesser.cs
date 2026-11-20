using System.Text.RegularExpressions;
using MicroserviceScripts.Ui.Compose;

namespace MicroserviceScripts.Ui.Drafting;

/// <param name="Source">Where it came from, shown next to the guess.</param>
public sealed record PortGuess(int Port, string Source);

/// <summary>The language a service is written in, from its image.</summary>
public sealed record Runtime(string Language, int? DefaultPort, string? DefaultPortReason);

/// <summary>
/// Which TCP port means "this component is serving": the readiness port fault
/// injection waits for when a component has no healthcheck. The most explicit
/// evidence wins: what the compose file says, then the Dockerfile, then what
/// the image or language listens on by default.
/// </summary>
public static class PortGuesser
{
    private static readonly string[] PortVariables =
        ["ASPNETCORE_HTTP_PORTS", "ASPNETCORE_URLS", "PORT", "SERVER_PORT", "HTTP_PORT", "APP_PORT", "UVICORN_PORT"];

    private static readonly Regex[] PortFlags =
    [
        new(@"--port[=\s]+(\d{2,5})\b"),
        new(@"(?:--bind|\s-b)[=\s]+\S*?:(\d{2,5})\b"),
        new(@"--urls[=\s]+\S*?:(\d{2,5})\b"),
        new(@"server\.port[=\s]+(\d{2,5})\b"),
        new(@"(?:0\.0\.0\.0|\[::\]|\*|\+|localhost):(\d{2,5})\b"),
        new(@"\bPORT=(\d{2,5})\b"),
    ];

    public static PortGuess? Guess(
        ComposeService service, DockerfileFacts? dockerfile, CatalogEntry? known, Runtime? runtime)
    {
        if (FromFlags(string.Join(' ', service.Entrypoint.Concat(service.Command))) is { } composeFlag)
        {
            return new PortGuess(composeFlag.Port, $"compose command: {composeFlag.Text}");
        }

        if (FromVariables(service.Environment) is { } composeVariable)
        {
            return new PortGuess(composeVariable.Port, $"compose environment {composeVariable.Key}");
        }

        if (service.PortTargets.Count > 0)
        {
            return new PortGuess(service.PortTargets[0], "compose ports (container side)");
        }

        if (service.Expose.Count > 0)
        {
            return new PortGuess(service.Expose[0], "compose expose");
        }

        if (dockerfile is not null)
        {
            var command = string.Join(' ', new[] { dockerfile.Entrypoint, dockerfile.Command }.OfType<string>());
            if (FromFlags(command) is { } flag)
            {
                return new PortGuess(flag.Port, $"Dockerfile CMD: {flag.Text}");
            }

            if (FromVariables(dockerfile.Environment) is { } variable)
            {
                return new PortGuess(variable.Port, $"Dockerfile ENV {variable.Key}");
            }

            if (dockerfile.Exposed.Count > 0)
            {
                return new PortGuess(dockerfile.Exposed[0], $"Dockerfile EXPOSE {dockerfile.Exposed[0]}");
            }
        }

        if (known?.Port is { } knownPort)
        {
            return new PortGuess(knownPort, $"{known.Label} listens on {knownPort} by default");
        }

        if (runtime?.DefaultPort is { } runtimePort)
        {
            return new PortGuess(runtimePort, runtime.DefaultPortReason ?? runtime.Language);
        }

        return null;
    }

    /// <summary>The language behind an image name (a Dockerfile's base, or the service's image).</summary>
    public static Runtime? RuntimeOf(string? image)
    {
        var parts = ImageCatalog.PathParts(image);
        if (parts.Count == 0)
        {
            return null;
        }

        var path = string.Join('/', parts);
        var tag = image!.Contains(':') && image.LastIndexOf(':') > image.LastIndexOf('/')
            ? image[(image.LastIndexOf(':') + 1)..]
            : "";
        if (path.Contains("dotnet"))
        {
            // .NET 8 moved the default port from 80 to 8080 (ASPNETCORE_HTTP_PORTS)
            var major = Regex.Match(tag, @"^(\d+)") is { Success: true } m ? int.Parse(m.Value) : 8;
            return major >= 8
                ? new Runtime(".NET", 8080, $".NET {major} listens on 8080 by default")
                : new Runtime(".NET", 80, $".NET {major} listens on 80 by default");
        }

        if (parts.Any(p => p.Contains("python") || p.Contains("uvicorn") || p.Contains("gunicorn")))
        {
            return new Runtime("Python", null, null);
        }

        if (parts.Any(p => p is "deno" || p.StartsWith("deno-")))
        {
            return new Runtime("Deno", 8000, "Deno.serve listens on 8000 by default");
        }

        if (parts.Any(p => p is "bun" || p.StartsWith("bun-")))
        {
            return new Runtime("Bun", 3000, "Bun.serve listens on 3000 by default");
        }

        if (parts.Any(p => p is "node" || p.StartsWith("node-")))
        {
            return new Runtime("Node.js", null, null);
        }

        if (parts.Any(p => p is "maven" or "gradle" or "openjdk" or "eclipse-temurin" or "amazoncorretto"
                || p.Contains("jdk") || p.Contains("jre") || p.StartsWith("ibm-semeru")))
        {
            return new Runtime("Java", null, null);
        }

        if (parts.Any(p => p is "golang" or "go"))
        {
            return new Runtime("Go", null, null);
        }

        if (parts.Any(p => p is "php" || p.StartsWith("php-")))
        {
            return tag.Contains("fpm", StringComparison.Ordinal)
                ? new Runtime("PHP", 9000, "PHP-FPM listens on 9000 by default")
                : tag.Contains("apache", StringComparison.Ordinal)
                    ? new Runtime("PHP", 80, "PHP with Apache listens on 80 by default")
                    : new Runtime("PHP", null, null);
        }

        // the rest by the image's name; no default port that holds for their frameworks
        foreach (var (names, language) in Others)
        {
            if (parts.Any(p => names.Contains(p)))
            {
                return new Runtime(language, null, null);
            }
        }

        if (parts[^1] is "nginx" or "httpd" or "caddy" or "nginx-unprivileged")
        {
            return new Runtime("static files", 80, $"{parts[^1]} listens on 80 by default");
        }

        return null;
    }

    private static readonly (string[] Names, string Language)[] Others =
    [
        (["rust", "rustlang"], "Rust"),
        (["ruby", "jruby"], "Ruby"),
        (["elixir"], "Elixir"),
        (["erlang"], "Erlang"),
        (["swift", "swiftlang"], "Swift"),
        (["dart"], "Dart"),
        (["sbt", "scala", "scala-sbt"], "Scala"),
        (["clojure"], "Clojure"),
        (["gcc", "clang", "cmake", "conanio"], "C/C++"),
        (["haskell"], "Haskell"),
        (["crystal"], "Crystal"),
        (["kotlin"], "Kotlin"),
        (["perl"], "Perl"),
        (["julia"], "Julia"),
        (["zig"], "Zig"),
        (["nim", "nimlang"], "Nim"),
        (["ocaml", "opam"], "OCaml"),
    ];

    /// <summary>The port a command line names, and the words that name it ("--port 8000").</summary>
    private static (int Port, string Text)? FromFlags(string text)
    {
        foreach (var flag in PortFlags)
        {
            if (flag.Match(text) is { Success: true } m && int.TryParse(m.Groups[1].Value, out var port))
            {
                return (port, m.Value.Trim());
            }
        }

        return null;
    }

    private static (int Port, string Key)? FromVariables(IReadOnlyDictionary<string, string> env)
    {
        foreach (var key in PortVariables)
        {
            if (env.TryGetValue(key, out var value)
                && Regex.Match(value, @"(?:^|:)(\d{2,5})(?:$|[;,/\s])") is { Success: true } m)
            {
                return (int.Parse(m.Groups[1].Value), key);
            }
        }

        return null;
    }
}
