namespace MicroserviceScripts.Ui.Infrastructure;

public sealed record PythonCommand(string FileName, IReadOnlyList<string> PrefixArguments, Version Version)
{
    public IReadOnlyList<string> Arguments(params string[] rest) => [.. PrefixArguments, .. rest];
}

/// <summary>
/// Finds a Python to run the analyzer with: <c>MS_PYTHON</c> if set, then
/// <c>python3</c>, <c>python</c>, and the <c>py -3</c> launcher on Windows.
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
            foreach (var (file, prefix) in Candidates())
            {
                var result = await runner.RunAsync(
                    file,
                    [.. prefix, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                    timeout: TimeSpan.FromSeconds(15),
                    cancellationToken: cancellationToken);
                if (result.Ok && Version.TryParse(result.StdOut.Trim(), out var version))
                {
                    // only a hit is remembered, so installing Python and checking
                    // again works without a restart
                    return _found = new PythonCommand(file, prefix, version);
                }
            }

            return null;
        }
        finally
        {
            _lock.Release();
        }
    }

    private static IEnumerable<(string File, string[] Prefix)> Candidates()
    {
        var configured = Environment.GetEnvironmentVariable("MS_PYTHON");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            yield return (configured, []);
        }

        yield return ("python3", []);
        yield return ("python", []);
        if (OperatingSystem.IsWindows())
        {
            yield return ("py", ["-3"]);
        }
    }
}
