namespace MicroserviceScripts.Ui.Infrastructure;

public sealed record PythonCommand(string FileName, IReadOnlyList<string> PrefixArguments, Version Version)
{
    public IReadOnlyList<string> Arguments(params string[] rest) => [.. PrefixArguments, .. rest];
}

/// <summary>
/// Finds a Python to run the analyzer with: <c>MS_PYTHON</c> if set (used as it
/// is), else the first of <c>python3</c>, <c>python</c>, <c>python3.14</c> down to
/// <c>python3.11</c> and the <c>py -3</c> launcher on Windows that is 3.11 or newer.
/// A system whose <c>python3</c> is older (Ubuntu 22.04's 3.10, macOS's 3.9) can
/// have a newer one installed beside it under its versioned name.
/// </summary>
public sealed class PythonLocator(IProcessRunner runner)
{
    private readonly SemaphoreSlim _lock = new(1, 1);
    private PythonCommand? _found;

    public async Task<PythonCommand?> FindAsync(CancellationToken cancellationToken = default)
    {
        if (_found is not null)
        {
            return _found;
        }

        await _lock.WaitAsync(cancellationToken);
        try
        {
            // too old to use, but worth naming when nothing newer is found
            PythonCommand? tooOld = null;
            var configured = !string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MS_PYTHON"));
            foreach (var (file, prefix) in Candidates())
            {
                var result = await runner.RunAsync(
                    file,
                    [.. prefix, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                    timeout: TimeSpan.FromSeconds(15),
                    cancellationToken: cancellationToken);
                if (!result.Ok || !Version.TryParse(result.StdOut.Trim(), out var version))
                {
                    continue;
                }

                var command = new PythonCommand(file, prefix, version);
                if (version >= Minimum)
                {
                    // only a usable hit is remembered, so installing Python and checking
                    // again works without a restart
                    return _found = command;
                }

                if (configured)
                {
                    // the one asked for, even too old: the check says so
                    return command;
                }

                tooOld ??= command;
            }

            return tooOld;
        }
        finally
        {
            _lock.Release();
        }
    }

    /// <summary>The oldest Python the analyzer runs on (it reads specs with tomllib).</summary>
    public static readonly Version Minimum = new(3, 11);

    private static IEnumerable<(string File, string[] Prefix)> Candidates()
    {
        var configured = Environment.GetEnvironmentVariable("MS_PYTHON");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            yield return (configured, []);
            yield break;
        }

        yield return ("python3", []);
        yield return ("python", []);
        foreach (var minor in new[] { 14, 13, 12, 11 })
        {
            yield return ($"python3.{minor}", []);
        }

        if (OperatingSystem.IsWindows())
        {
            yield return ("py", ["-3"]);
        }
    }
}
