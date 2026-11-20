using MicroserviceScripts.Ui.Compose;
using MicroserviceScripts.Ui.Drafting;

namespace MicroserviceScripts.Ui.Tests;

public class PortAndDockerfileTests
{
    private static ComposeService Service(
        IReadOnlyList<int>? ports = null, IReadOnlyDictionary<string, string>? env = null,
        IReadOnlyList<string>? command = null) =>
        new("svc", null, "/ctx", null, [], false, ports ?? [], [], null, null,
            env ?? new Dictionary<string, string>(), command ?? [], [], []);

    [Fact]
    public void Reads_the_final_stage_through_its_aliases()
    {
        var facts = DockerfileFacts.Parse("""
            # build
            FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
            ENV IGNORED=1
            FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS base
            ENV ASPNETCORE_HTTP_PORTS=5000 \
                OTHER="two words"
            EXPOSE 5000/tcp
            FROM base AS final
            ENTRYPOINT ["dotnet", "Api.dll"]
            """);
        Assert.Equal("mcr.microsoft.com/dotnet/aspnet:8.0", facts.BaseImage);
        Assert.Equal("dotnet Api.dll", facts.Entrypoint);
        Assert.Equal("5000", facts.Environment["ASPNETCORE_HTTP_PORTS"]);
        Assert.Equal("two words", facts.Environment["OTHER"]);
        Assert.False(facts.Environment.ContainsKey("IGNORED"));
        Assert.Equal([5000], facts.Exposed);
    }

    [Fact]
    public void Explicit_evidence_beats_defaults()
    {
        var uvicorn = DockerfileFacts.Parse("FROM python:3.12\nCMD [\"uvicorn\", \"app:app\", \"--port\", \"8000\"]");
        Assert.Equal(new PortGuess(8000, "Dockerfile CMD: --port 8000"),
            PortGuesser.Guess(Service(), uvicorn, null, PortGuesser.RuntimeOf(uvicorn.BaseImage)));

        // what the compose file says wins over the Dockerfile
        Assert.Equal(8081, PortGuesser.Guess(
            Service(env: new Dictionary<string, string> { ["ASPNETCORE_URLS"] = "http://+:8081" }),
            uvicorn, null, null)!.Port);
        Assert.Equal(3000, PortGuesser.Guess(
            Service(command: ["node", "server.js", "--port", "3000"]), uvicorn, null, null)!.Port);
        Assert.Equal(9090, PortGuesser.Guess(Service(ports: [9090]), uvicorn, null, null)!.Port);
    }

    [Fact]
    public void Falls_back_to_what_the_runtime_listens_on()
    {
        var dotnet8 = PortGuesser.RuntimeOf("mcr.microsoft.com/dotnet/aspnet:8.0");
        Assert.Equal(8080, PortGuesser.Guess(Service(), null, null, dotnet8)!.Port);
        Assert.Equal(80, PortGuesser.RuntimeOf("mcr.microsoft.com/dotnet/aspnet:6.0")!.DefaultPort);
        Assert.Equal("Python", PortGuesser.RuntimeOf("tiangolo/uvicorn-gunicorn-fastapi:python3.10")!.Language);
        Assert.Equal("Java", PortGuesser.RuntimeOf("eclipse-temurin:21-jre")!.Language);

        // a Python service with nothing to go on gets no port: the user is asked
        Assert.Null(PortGuesser.Guess(Service(), null, null, PortGuesser.RuntimeOf("python:3.12")));
    }
}
